"""录制 / 回放层的测试。

这一层的失败模式很隐蔽：缓存键算错了会静默命中过期结果，
成本没记 0 会让人以为花了钱，回放 miss 被当成"模型坏了"会触发一堆无意义重试。
所以这里逐条锁死。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.contracts.llm import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    ModelPrice,
)
from app.llm.cache import (
    CACHE_VERSION,
    CachedTransport,
    CacheMode,
    ReplayMiss,
    cache_summary,
    count_files,
    prompt_key,
    total_bytes,
)
from app.llm.gateway import LLMGateway


def _req(text: str = "写一段", *, model: str = "m1", json_mode: bool = False) -> LLMRequest:
    return LLMRequest(
        messages=[
            LLMMessage(role="system", content="你是助手"),
            LLMMessage(role="user", content=text),
        ],
        model=model,
        json_mode=json_mode,
        tag="test",
    )


class _CountingTransport:
    """记录被调用次数，返回固定文本。"""

    def __init__(self, text: str = '{"ok": true}') -> None:
        self.text = text
        self.calls = 0
        self.models: list[str] = []

    async def send(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        self.models.append(req.model)
        return LLMResponse(
            text=self.text,
            model=req.model,
            usage=LLMUsage(prompt_tokens=100, completion_tokens=50),
            latency_ms=1200,
        )

    async def aclose(self) -> None:
        pass


class CacheModeTest(unittest.TestCase):
    def test_parse_accepts_all_modes_and_empty(self) -> None:
        self.assertIs(CacheMode.parse(""), CacheMode.LIVE)
        self.assertIs(CacheMode.parse(None), CacheMode.LIVE)
        for mode in CacheMode:
            self.assertIs(CacheMode.parse(mode.value.upper()), mode)

    def test_parse_rejects_typo(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            CacheMode.parse("raplay")
        self.assertIn("raplay", str(ctx.exception))

    def test_mode_capabilities(self) -> None:
        self.assertTrue(CacheMode.AUTO.reads_cache)
        self.assertTrue(CacheMode.AUTO.writes_cache)
        self.assertFalse(CacheMode.REPLAY.writes_cache)
        self.assertFalse(CacheMode.LIVE.reads_cache)
        # replay 不允许联网，这是它唯一能保证"绝不花钱"的前提
        self.assertFalse(CacheMode.REPLAY.allows_live)


class PromptKeyTest(unittest.TestCase):
    def test_same_prompt_same_key(self) -> None:
        self.assertEqual(prompt_key(_req("相同")), prompt_key(_req("相同")))

    def test_changed_prompt_changes_key(self) -> None:
        self.assertNotEqual(prompt_key(_req("A")), prompt_key(_req("B")))

    def test_json_mode_changes_key(self) -> None:
        self.assertNotEqual(
            prompt_key(_req("x", json_mode=False)), prompt_key(_req("x", json_mode=True))
        )

    def test_model_is_not_part_of_key(self) -> None:
        # 换模型不该让开发缓存全部失效——这是刻意的取舍，
        # 代价是"用 A 模型录的响应会被 B 模型回放"，所以录制文件里存了 model 备查
        self.assertEqual(
            prompt_key(_req("x", model="deepseek-chat")),
            prompt_key(_req("x", model="qwen-plus")),
        )


class CachedTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="rfcache-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_record_then_replay_returns_same_text(self) -> None:
        inner = _CountingTransport('{"markdown": "第一次"}')

        rec = CachedTransport(inner, mode=CacheMode.RECORD, cache_dir=self.dir)
        first = _run(rec.send(_req("同一个提示")))

        rep = CachedTransport(inner, mode=CacheMode.REPLAY, cache_dir=self.dir)
        second = _run(rep.send(_req("同一个提示")))

        self.assertEqual(first.text, second.text)
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(inner.calls, 1)  # 回放没有再打网络
        self.assertEqual(rep.hits, 1)
        self.assertEqual(second.latency_ms, 0)
        self.assertEqual(second.usage.prompt_tokens, 100)  # usage 保留，便于看上下文规模

    def test_replay_miss_raises_with_fix_hint(self) -> None:
        inner = _CountingTransport()
        rep = CachedTransport(inner, mode=CacheMode.REPLAY, cache_dir=self.dir)
        with self.assertRaises(ReplayMiss) as ctx:
            _run(rep.send(_req("从未录制过")))
        self.assertIn("record", str(ctx.exception))
        self.assertEqual(inner.calls, 0)  # 绝不偷偷联网

    def test_auto_mode_records_then_serves_from_cache(self) -> None:
        inner = _CountingTransport("auto")
        tr = CachedTransport(inner, mode=CacheMode.AUTO, cache_dir=self.dir)

        first = _run(tr.send(_req("auto 提示")))
        second = _run(tr.send(_req("auto 提示")))

        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(inner.calls, 1)
        self.assertEqual(tr.recorded, 1)
        self.assertEqual(tr.hits, 1)

    def test_record_does_not_overwrite_existing(self) -> None:
        inner = _CountingTransport("旧")
        tr = CachedTransport(inner, mode=CacheMode.RECORD, cache_dir=self.dir)
        _run(tr.send(_req("不改")))

        inner2 = _CountingTransport("新")
        tr2 = CachedTransport(inner2, mode=CacheMode.RECORD, cache_dir=self.dir)
        _run(tr2.send(_req("不改")))

        rep = CachedTransport(None, mode=CacheMode.REPLAY, cache_dir=self.dir)
        self.assertEqual(_run(rep.send(_req("不改"))).text, "旧")

    def test_corrupted_file_is_treated_as_miss(self) -> None:
        req = _req("损坏")
        path = self.dir / prompt_key(req)[:2] / f"{prompt_key(req)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ 不是合法 JSON", encoding="utf-8")

        with self.assertRaises(ReplayMiss):
            _run(CachedTransport(None, mode=CacheMode.REPLAY, cache_dir=self.dir).send(req))

        # auto 模式下损坏缓存应视为未命中并重录，而不是整条流水线崩掉
        inner = _CountingTransport("重录")
        tr = CachedTransport(inner, mode=CacheMode.AUTO, cache_dir=self.dir)
        self.assertEqual(_run(tr.send(req)).text, "重录")
        self.assertEqual(inner.calls, 1)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["text"], "重录")

    def test_version_mismatch_invalidates(self) -> None:
        req = _req("旧版本")
        key = prompt_key(req)
        path = self.dir / key[:2] / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"v": CACHE_VERSION - 1, "text": "过期", "model": "x"}),
            encoding="utf-8",
        )
        with self.assertRaises(ReplayMiss):
            _run(CachedTransport(None, mode=CacheMode.REPLAY, cache_dir=self.dir).send(req))

    def test_no_tmp_files_left_behind(self) -> None:
        tr = CachedTransport(_CountingTransport(), mode=CacheMode.RECORD, cache_dir=self.dir)
        _run(tr.send(_req("原子写")))
        self.assertEqual(list(self.dir.rglob("*.tmp")), [])

    def test_summary_counts_files_and_tags(self) -> None:
        tr = CachedTransport(_CountingTransport(), mode=CacheMode.RECORD, cache_dir=self.dir)
        _run(tr.send(_req("a")))
        _run(tr.send(_req("b")))
        info = cache_summary(self.dir)
        self.assertEqual(info["files"], 2)
        self.assertGreater(info["bytes"], 0)
        self.assertEqual(info["tags"], {"test": 2})
        self.assertEqual(count_files(self.dir), 2)
        self.assertGreater(total_bytes(self.dir), 0)


class GatewayCachedCostTest(unittest.TestCase):
    """命中缓存必须记 ¥0，否则成本报表是假账。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="rfcache-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_cached_call_records_zero_cost(self) -> None:
        spans: list[Any] = []

        class _Recorder:
            def record(self, span: Any) -> None:
                spans.append(span)

        def build(mode: CacheMode) -> LLMGateway:
            transport = CachedTransport(
                _CountingTransport(), mode=mode, cache_dir=self.dir
            )
            return LLMGateway(
                transport,
                primary_model="m1",
                max_retries=1,
                prices={"m1": ModelPrice(10.0, 30.0)},
                recorder=_Recorder(),
            )

        live_gw = build(CacheMode.RECORD)
        _run(live_gw.complete(_req("同一条")))
        self.assertGreater(spans[-1].cost, 0.0)

        replay_gw = build(CacheMode.REPLAY)
        resp = _run(replay_gw.complete(_req("同一条")))
        self.assertTrue(resp.cached)
        self.assertEqual(spans[-1].cost, 0.0)
        self.assertEqual(replay_gw.cached_calls, 1)
        self.assertEqual(replay_gw.degraded_calls, 0)


class EvalGuardTest(unittest.TestCase):
    """评测器必须拒绝回放——否则「重复 N 次」是假的。"""

    def test_eval_refuses_non_live_mode(self) -> None:
        from app.evaluation.runner import EvalRunner

        stub = SimpleNamespace(llm_mode="auto", gateway=object(), settings=None)
        with self.assertRaises(RuntimeError) as ctx:
            EvalRunner(runtime=stub)  # type: ignore[arg-type]
        self.assertIn("live", str(ctx.exception))


class ReplayMissNoRetryTest(unittest.TestCase):
    """缓存缺失是确定性缺失：重试只会白等退避时间。"""

    def test_replay_miss_fails_without_retry(self) -> None:
        from app.assembler import build_runtime
        from app.contracts.agent import AgentRegistry
        from app.contracts.nodes import NodeType
        from app.core.config import Settings
        from app.orchestrator.runner import TaskRunner

        tmp = Path(tempfile.mkdtemp(prefix="rfretry-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        settings = Settings(
            database_url=f"sqlite:///{(tmp / 't.db').as_posix()}",
            max_retries=3,  # 故意给足重试次数
            retry_backoff_s=0.0,
        )
        rt = build_runtime(settings, profile="mock", announce=False)
        inner = {t: rt.registry.get(t) for t in NodeType}

        class _Boom:
            node_type = NodeType.WRITE
            calls = 0

            async def execute(self, ctx):  # noqa: ANN001, ANN202
                _Boom.calls += 1
                raise ReplayMiss("测试：缺缓存")

        registry = AgentRegistry({**inner, NodeType.WRITE: _Boom()})
        runner = TaskRunner(
            store=rt.store, registry=registry, settings=settings, profile="mock"
        )
        task_id, _ = runner.submit(topic="不可重试", sections=1)
        asyncio.run(runner.run(task_id))

        node = {n.node_id: n for n in rt.store.list_nodes(task_id)}["write_ch1"]
        self.assertIs(node.status.value, "FAILED")
        self.assertEqual(node.attempt, 1)  # 只跑了一次，没走退避重试
        self.assertEqual(_Boom.calls, 1)
        self.assertIn("ReplayMiss", node.error or "")


def _run(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
