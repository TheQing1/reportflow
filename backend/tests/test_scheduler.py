"""调度行为验证：并行 / 串行 / 失败阻塞 / 断点续跑。

这里用"记录 Agent 的起止时刻"来计算真实并发重叠度，
而不是相信日志——日志可能骗人，时间区间不会。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from app.agents import MockAgentConfig, build_mock_registry
from app.assembler import build_runtime
from app.contracts.agent import AgentContext
from app.contracts.nodes import NodeStatus, NodeType
from app.contracts.task import TaskStatus
from app.core.config import Settings

FAST_LATENCY = {
    NodeType.PLANNER: 0.01,
    NodeType.RESEARCH: 0.12,
    NodeType.WRITE: 0.06,
    NodeType.REVIEW: 0.04,
}


class RecordingAgent:
    """包一层，记录每个节点的执行区间；不改变被包 Agent 的行为。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.node_type = inner.node_type
        self.intervals: list[tuple[str, float, float]] = []

    async def execute(self, ctx: AgentContext) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            return await self.inner.execute(ctx)
        finally:
            self.intervals.append((ctx.node.id, t0, time.monotonic()))

    def executed(self) -> list[str]:
        return [nid for nid, _, _ in self.intervals]

    def max_overlap(self, prefix: str) -> int:
        spans = sorted(
            [(s, e) for nid, s, e in self.intervals if nid.startswith(prefix)]
        )
        best = cur = 0
        edges = [(s, 1) for s, _ in spans] + [(e, -1) for _, e in spans]
        for _, delta in sorted(edges, key=lambda x: (x[0], x[1])):
            cur += delta
            best = max(best, cur)
        return best


def make_registry(fail_nodes: set[str] | None = None) -> tuple[Any, dict[NodeType, RecordingAgent]]:
    cfg = MockAgentConfig(
        latency_s=FAST_LATENCY, fail_nodes=frozenset(fail_nodes or ()), jitter_s=0.01
    )
    base = build_mock_registry(cfg)
    rec: dict[NodeType, RecordingAgent] = {
        t: RecordingAgent(base.get(t)) for t in NodeType
    }
    from app.contracts.agent import AgentRegistry

    return AgentRegistry(rec), rec


class SchedulerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="reportflow-test-"))
        self.db_url = f"sqlite:///{(self.tmpdir / 'test.db').as_posix()}"
        self.settings = Settings(
            database_url=self.db_url,
            max_concurrency=3,
            node_timeout_s=5,
            max_retries=1,
            retry_backoff_s=0.01,
            poll_interval_s=0.01,
            # 这组用例验证的是带 Reviewer 的完整串行尾部；默认流水线已关掉 review
            review_in_pipeline=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def runtime(self, fail_nodes: set[str] | None = None):
        rt = build_runtime(self.settings, profile="mock")
        registry, rec = make_registry(fail_nodes)
        rt.registry = registry
        rt.runner.registry = registry
        return rt, rec


class TestParallelAndSerial(SchedulerTestBase):
    def test_research_parallel_tail_serial(self) -> None:
        rt, rec = self.runtime()
        task_id, _ = rt.runner.submit(topic="储能", sections=5)
        result = asyncio.run(rt.runner.run(task_id))

        self.assertIs(result.task_status, TaskStatus.DONE)
        all_executed = [nid for r in rec.values() for nid in r.executed()]
        self.assertEqual(len(all_executed), 16)

        research_overlap = max(r.max_overlap("research") for r in rec.values())
        write_overlap = max(r.max_overlap("write") for r in rec.values())
        review_overlap = max(r.max_overlap("review") for r in rec.values())

        # 5 个 research 节点，并发上限 3 → 观察到的最大重叠必须正好是 3
        self.assertEqual(research_overlap, 3)
        # write / review 串行：任何时刻只有一个在跑
        self.assertEqual(write_overlap, 1)
        self.assertEqual(review_overlap, 1)

    def test_concurrency_limit_is_respected(self) -> None:
        settings = Settings(**{**self.settings.__dict__, "max_concurrency": 2})
        rt = build_runtime(settings, profile="mock")
        registry, rec = make_registry()
        rt.runner.registry = registry
        task_id, _ = rt.runner.submit(topic="储能", sections=5)
        asyncio.run(rt.runner.run(task_id))
        self.assertLessEqual(max(r.max_overlap("research") for r in rec.values()), 2)


class TestBlockPropagation(SchedulerTestBase):
    def test_failed_node_blocks_all_downstream(self) -> None:
        rt, rec = self.runtime(fail_nodes={"research_ch2"})
        task_id, _ = rt.runner.submit(topic="储能", sections=5)
        result = asyncio.run(rt.runner.run(task_id))

        nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        self.assertIs(nodes["research_ch2"].status, NodeStatus.FAILED)
        for nid in ["write_ch2", "review_ch2", "write_ch3", "review_ch3", "write_ch5", "review_ch5"]:
            self.assertIs(nodes[nid].status, NodeStatus.BLOCKED, nid)
        # 上游失败不影响同层兄弟：ch1 仍然完成
        self.assertIs(nodes["research_ch1"].status, NodeStatus.DONE)
        self.assertIs(nodes["review_ch1"].status, NodeStatus.DONE)
        # 任务保留已完成章节
        self.assertIs(result.task_status, TaskStatus.PARTIAL)
        # BLOCKED 的节点一次都没被执行
        executed = {nid for r in rec.values() for nid in r.executed()}
        self.assertNotIn("write_ch2", executed)
        self.assertNotIn("review_ch3", executed)

    def test_retry_exhausted_marks_failed(self) -> None:
        rt, _ = self.runtime(fail_nodes={"research_ch1"})
        task_id, _ = rt.runner.submit(topic="储能", sections=2)
        asyncio.run(rt.runner.run(task_id))
        node = {n.node_id: n for n in rt.store.list_nodes(task_id)}["research_ch1"]
        self.assertIs(node.status, NodeStatus.FAILED)
        self.assertEqual(node.attempt, 2)  # max_retries=1 → 共 2 次尝试
        self.assertIn("injected failure", node.error or "")


class TestResume(SchedulerTestBase):
    def test_crash_then_resume_does_not_rerun_done(self) -> None:
        rt, rec1 = self.runtime()
        task_id, _ = rt.runner.submit(topic="储能", sections=5)

        with self.assertRaises(SystemExit):
            asyncio.run(rt.runner.run(task_id, crash_before="write_ch3"))

        nodes_after_crash = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        done_before = {
            nid for nid, n in nodes_after_crash.items() if n.status is NodeStatus.DONE
        }
        self.assertTrue(done_before, "崩溃前应至少完成一些节点")
        self.assertIs(nodes_after_crash["write_ch3"].status, NodeStatus.RUNNING)

        # 新进程：全新 runtime + 全新记录器，指向同一个数据库
        rt2, rec2 = self.runtime()
        result = asyncio.run(rt2.runner.run(task_id))

        executed_again = set(rec2[NodeType.PLANNER].executed())
        for r in rec2.values():
            executed_again |= set(r.executed())

        self.assertFalse(
            done_before & executed_again,
            f"已完成的节点被重跑: {sorted(done_before & executed_again)}",
        )
        self.assertIn("write_ch3", executed_again)  # 孤儿节点被重置并重跑
        self.assertIs(result.task_status, TaskStatus.DONE)

    def test_outputs_survive_restart(self) -> None:
        rt, _ = self.runtime()
        task_id, _ = rt.runner.submit(topic="储能", sections=2)
        asyncio.run(rt.runner.run(task_id))
        before = {n.node_id: dict(n.output or {}) for n in rt.store.list_nodes(task_id)}

        rt2, _ = self.runtime()
        result = asyncio.run(rt2.runner.run(task_id))
        after = {n.node_id: dict(n.output or {}) for n in rt2.store.list_nodes(task_id)}
        self.assertEqual(before, after)
        self.assertEqual(result.note, "already done")

    def test_reset_failed_allows_recovery(self) -> None:
        rt, _ = self.runtime(fail_nodes={"research_ch2"})
        task_id, _ = rt.runner.submit(topic="储能", sections=3)
        asyncio.run(rt.runner.run(task_id))

        rt2, _ = self.runtime()  # 故障已移除
        result = asyncio.run(rt2.runner.run(task_id, reset_failed=True))
        self.assertIs(result.task_status, TaskStatus.DONE)


if __name__ == "__main__":
    unittest.main()
