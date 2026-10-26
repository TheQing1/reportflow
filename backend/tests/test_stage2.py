"""阶段二：交接协议、四个 Agent、LLM 网关、打回闭环。

这些用例全部不联网：
- Agent 用 ScriptedLLM（返回预置文本）
- 网关用 ScriptedTransport（按脚本抛异常）
真正走 HTTP 的验证由 `scripts/fake_openai_server.py` + CLI 完成（见 STAGE2_ACCEPTANCE.md）。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.agents.base import parse_json_object
from app.agents.planner import PlannerAgent
from app.agents.researcher import ResearcherAgent
from app.agents.reviewer import ReviewerAgent
from app.agents.writer import WriterAgent
from app.assembler import build_runtime
from app.contracts.agent import AgentContext, AgentRegistry
from app.contracts.handoff import (
    Fact,
    HandoffError,
    ResearchPacket,
    ReviewVerdict,
)
from app.contracts.llm import (
    LLMBadResponse,
    LLMPermanent,
    LLMRateLimit,
    LLMRequest,
    LLMResponse,
    LLMTransient,
    LLMUnavailable,
    LLMUsage,
)
from app.contracts.nodes import NodeSpec, NodeType
from app.contracts.retrieval import Document
from app.contracts.task import TaskStatus
from app.core.config import Settings
from app.llm import LLMGateway, ScriptedLLM, json_handler
from app.rag import LocalCorpusRetriever

QUOTE = "2025 年新增装机容量约 8.4GWh，同比增长 62%"


def _node(node_id: str, node_type: NodeType, **payload: Any) -> NodeSpec:
    return NodeSpec(id=node_id, type=node_type, payload=payload)


def _ctx(node: NodeSpec, upstream: dict[str, Any] | None = None, hint: str | None = None) -> AgentContext:
    return AgentContext(task_id="t", node=node, attempt=1, upstream=upstream or {}, hint=hint)


class TestHandoffProtocol(unittest.TestCase):
    def test_quote_is_clipped_not_truncated_blindly(self) -> None:
        fact = Fact.from_dict(
            {"claim": "c", "source_id": "d", "page": 1, "quote": "字" * 500}
        )
        self.assertLessEqual(len(fact.quote), 300)
        self.assertIn("……", fact.quote)

    def test_fact_requires_quote(self) -> None:
        with self.assertRaises(HandoffError):
            Fact.from_dict({"claim": "c", "source_id": "d"})

    def test_summary_is_compressed_on_sentence_boundary(self) -> None:
        packet = ResearchPacket.from_dict(
            {"chapter": 1, "summary": ("这是一句话。" * 80) }
        )
        self.assertLessEqual(len(packet.summary), 200)
        self.assertNotIn("\n", packet.summary)

    def test_revise_without_defects_is_rejected(self) -> None:
        with self.assertRaises(HandoffError):
            ReviewVerdict.from_dict({"chapter": 1, "verdict": "revise", "defects": []})

    def test_defect_fix_target_is_derived_not_trusted(self) -> None:
        verdict = ReviewVerdict.from_dict(
            {
                "chapter": 1,
                "verdict": "revise",
                "defects": [
                    {"type": "missing_evidence", "claim": "x", "reason": "y", "fix_target": "write"}
                ],
            }
        )
        # 模型说 write 不算，missing_evidence 一律指向 research
        self.assertEqual(verdict.fix_target, "research")


class TestJsonParsing(unittest.TestCase):
    def test_fenced(self) -> None:
        self.assertEqual(parse_json_object("```json\n{\"a\":1}\n```"), {"a": 1})

    def test_wrapped_in_prose(self) -> None:
        self.assertEqual(parse_json_object("好的，结果如下：{\"a\":2} 完毕"), {"a": 2})

    def test_garbage_raises(self) -> None:
        with self.assertRaises(LLMBadResponse):
            parse_json_object("抱歉我不能完成")


class TestPlannerAgent(unittest.TestCase):
    def test_rejects_wrong_chapter_count(self) -> None:
        llm = ScriptedLLM(
            {"planner": json_handler({"chapters": [{"chapter": 1, "title": "a", "queries": ["q"]}]})}
        )
        agent = PlannerAgent(llm)
        with self.assertRaises(HandoffError):
            asyncio.run(agent.execute(_ctx(_node("planner", NodeType.PLANNER, topic="x", sections=3))))

    def test_strips_question_marks_from_queries(self) -> None:
        llm = ScriptedLLM(
            {
                "planner": json_handler(
                    {"chapters": [{"chapter": 1, "title": "a", "queries": ["储能增速？"]}]}
                )
            }
        )
        out = asyncio.run(
            PlannerAgent(llm).execute(_ctx(_node("planner", NodeType.PLANNER, topic="x", sections=1)))
        )
        self.assertEqual(out["outline"][0]["queries"], ["储能增速"])


class TestResearcherAgent(unittest.TestCase):
    def setUp(self) -> None:
        self.retriever = LocalCorpusRetriever(
            [Document(doc_id="doc_01", title="白皮书", text=QUOTE + "，为近五年最快增速。")]
        )

    def test_drops_fabricated_quote(self) -> None:
        payload = {
            "chapter": 1,
            "summary": "覆盖 2 条",
            "key_facts": [
                {"claim": "新增 8.4GWh", "source_id": "doc_01", "page": 23, "quote": QUOTE},
                {
                    "claim": "新增 9.1GWh",  # 编造的数字
                    "source_id": "doc_01",
                    "page": 23,
                    "quote": "2025 年新增装机容量约 9.1GWh，同比增长 62%",
                },
            ],
            "open_questions": [],
        }
        agent = ResearcherAgent(ScriptedLLM({"research_ch1": json_handler(payload)}), self.retriever)
        out = asyncio.run(
            agent.execute(
                _ctx(
                    _node("research_ch1", NodeType.RESEARCH, chapter=1),
                    upstream={"planner": {"outline": [{"chapter": 1, "queries": ["新增装机"]}]}},
                )
            )
        )
        self.assertEqual(len(out["key_facts"]), 1, "编造的引用必须被丢弃")
        self.assertEqual(out["dropped_facts"], 1)
        self.assertEqual(out["key_facts"][0]["quote"], QUOTE)

    def test_unknown_source_is_dropped(self) -> None:
        payload = {
            "chapter": 1,
            "summary": "x",
            "key_facts": [
                {"claim": "c", "source_id": "doc_99", "page": 1, "quote": QUOTE}
            ],
        }
        agent = ResearcherAgent(ScriptedLLM({"research_ch1": json_handler(payload)}), self.retriever)
        out = asyncio.run(
            agent.execute(
                _ctx(
                    _node("research_ch1", NodeType.RESEARCH, chapter=1),
                    upstream={"planner": {"outline": [{"chapter": 1, "queries": ["新增装机"]}]}},
                )
            )
        )
        self.assertEqual(out["key_facts"], [])
        self.assertEqual(out["dropped_facts"], 1)

    def test_empty_corpus_returns_open_question(self) -> None:
        agent = ResearcherAgent(
            ScriptedLLM({"research_ch1": "never used"}), LocalCorpusRetriever([])
        )
        out = asyncio.run(
            agent.execute(_ctx(_node("research_ch1", NodeType.RESEARCH, chapter=1)))
        )
        self.assertEqual(out["key_facts"], [])
        self.assertTrue(out["open_questions"])


class TestWriterAgent(unittest.TestCase):
    def _packet(self) -> dict[str, Any]:
        return {
            "chapter": 1,
            "summary": "s",
            "key_facts": [
                {"claim": "新增 8.4GWh", "source_id": "doc_01", "page": 23, "quote": QUOTE},
            ],
            "open_questions": [],
            "dropped_facts": 0,
        }

    def test_strips_unknown_citation_and_rebuilds_map(self) -> None:
        llm = ScriptedLLM(
            {
                "write_ch1": json_handler(
                    {
                        "chapter": 1,
                        "markdown": "## 一\n\n新增 8.4GWh[^doc_01]，另有 9.9GWh[^doc_99]。",
                        "citation_map": {"doc_99": 999},
                    }
                )
            }
        )
        out = asyncio.run(
            WriterAgent(llm).execute(
                _ctx(
                    _node("write_ch1", NodeType.WRITE, chapter=1),
                    upstream={"research_ch1": self._packet()},
                )
            )
        )
        self.assertNotIn("doc_99", out["markdown"])
        self.assertEqual(out["invalid_citations"], 1)
        self.assertEqual(out["citation_map"], {"doc_01": 23})  # 页码取自素材，不采信模型
        self.assertEqual(len(out["used_facts"]), 1)

    def test_keeps_all_facts_of_same_source(self) -> None:
        """同一篇文档的多条 fact 不能被 source_id 去重掉。

        曾经用 `{f.source_id: f}` 建映射，同源只剩最后一条：
        正文引用了第 1 条的数字，Reviewer 拿第 3 条的原文去核对，
        必然判成"编造"，于是每章都被打回（实测打回率 88%、任务全 PARTIAL）。
        """
        packet = {
            "chapter": 1,
            "summary": "s",
            "key_facts": [
                {"claim": "补贴覆盖12个省份", "source_id": "doc_01", "page": 1, "quote": "补贴覆盖12个省份"},
                {"claim": "最高补贴300元/千瓦", "source_id": "doc_01", "page": 2, "quote": "最高补贴300元/千瓦"},
            ],
            "open_questions": [],
        }
        llm = ScriptedLLM(
            {
                "write_ch1": json_handler(
                    {
                        "chapter": 1,
                        "markdown": "## 一\n\n补贴覆盖12个省份[^doc_01]。",
                    }
                )
            }
        )
        out = asyncio.run(
            WriterAgent(llm).execute(
                _ctx(_node("write_ch1", NodeType.WRITE, chapter=1), upstream={"research_ch1": packet})
            )
        )
        self.assertEqual(len(out["used_facts"]), 2, "同源两条 fact 都要保留")
        self.assertEqual({f["claim"] for f in out["used_facts"]},
                         {"补贴覆盖12个省份", "最高补贴300元/千瓦"})
        self.assertEqual(out["citation_map"], {"doc_01": 1})

    def test_empty_packet_skips_llm(self) -> None:
        llm = ScriptedLLM({})
        out = asyncio.run(
            WriterAgent(llm).execute(
                _ctx(
                    _node("write_ch1", NodeType.WRITE, chapter=1),
                    upstream={"research_ch1": {"chapter": 1, "key_facts": [], "open_questions": ["缺"]}},
                )
            )
        )
        self.assertTrue(out["insufficient_evidence"])
        self.assertIn("资料未涉及", out["markdown"])
        self.assertEqual(llm.requests, [], "空素材不应调用模型")


class TestReviewerAgent(unittest.TestCase):
    def _draft(self, claim: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "chapter": 1,
            "markdown": f"## 一\n\n{claim}[^doc_01]",
            "citation_map": {"doc_01": 23},
            "used_facts": [
                {"claim": claim, "source_id": "doc_01", "page": 23, "quote": QUOTE}
            ],
            **kwargs,
        }

    def test_numeric_mismatch_caught_without_llm(self) -> None:
        llm = ScriptedLLM({})
        out = asyncio.run(
            ReviewerAgent(llm).execute(
                _ctx(
                    _node("review_ch1", NodeType.REVIEW, chapter=1),
                    upstream={"write_ch1": self._draft("新增装机 9.1GWh")},
                )
            )
        )
        self.assertEqual(out["verdict"], "revise")
        self.assertEqual(out["reviewed_by"], "program")
        self.assertEqual(out["defects"][0]["fix_target"], "write")
        self.assertEqual(llm.requests, [], "程序能判的问题不该花钱问模型")

    def test_chapter_ordinal_not_treated_as_data(self) -> None:
        """「第 3 章」里的 3 不是数据，不能触发数字不一致。"""
        llm = ScriptedLLM({"review_ch1": json_handler({"chapter": 1, "verdict": "pass", "defects": []})})
        out = asyncio.run(
            ReviewerAgent(llm).execute(
                _ctx(
                    _node("review_ch1", NodeType.REVIEW, chapter=1),
                    upstream={"write_ch1": self._draft("第 3 章要点：新增 8.4GWh")},
                )
            )
        )
        self.assertEqual(out["verdict"], "pass")

    def test_insufficient_evidence_passes_without_llm(self) -> None:
        llm = ScriptedLLM({})
        out = asyncio.run(
            ReviewerAgent(llm).execute(
                _ctx(
                    _node("review_ch1", NodeType.REVIEW, chapter=1),
                    upstream={
                        "write_ch1": {
                            "chapter": 1,
                            "markdown": "## 一\n\n资料未涉及。",
                            "citation_map": {},
                            "used_facts": [],
                            "insufficient_evidence": True,
                        }
                    },
                )
            )
        )
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(llm.requests, [])


# ---------------------------------------------------------------- 网关


class RecordingRecorder:
    def __init__(self) -> None:
        self.spans: list = []

    def record(self, span) -> None:
        self.spans.append(span)


class ScriptedTransport:
    """按模型名给出脚本：字符串=成功返回，异常实例=抛出。"""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []

    async def send(self, req: LLMRequest) -> LLMResponse:
        self.calls.append(req.model)
        queue = self.script.get(req.model) or []
        item = queue.pop(0) if queue else f"ok:{req.model}"
        if isinstance(item, BaseException):
            raise item
        return LLMResponse(
            text=item, model=req.model, usage=LLMUsage(10, 5), latency_ms=7
        )


def _req(tag: str = "n1") -> LLMRequest:
    return LLMRequest(messages=[], model="", tag=tag)


class TestGateway(unittest.TestCase):
    def _gateway(self, script: dict[str, list[Any]], **kwargs: Any):
        recorder = RecordingRecorder()
        gw = LLMGateway(
            ScriptedTransport(script),
            primary_model="primary",
            fallback_model="fallback",
            backoff_s=0.0,
            recorder=recorder,
            **kwargs,
        )
        return gw, recorder

    def test_retries_transient_then_succeeds(self) -> None:
        gw, rec = self._gateway({"primary": [LLMRateLimit("429"), "ok"]})
        resp = asyncio.run(gw.complete(_req()))
        self.assertEqual(resp.text, "ok")
        self.assertEqual(resp.model, "primary")
        self.assertFalse(resp.degraded)
        self.assertEqual(len(rec.spans), 2)  # 两次尝试都记了 span

    def test_falls_back_when_primary_exhausted(self) -> None:
        gw, rec = self._gateway(
            {"primary": [LLMTransient("500"), LLMTransient("500")], "fallback": ["ok"]},
            max_retries=2,
        )
        resp = asyncio.run(gw.complete(_req()))
        self.assertTrue(resp.degraded)
        self.assertEqual(resp.model, "fallback")
        self.assertEqual(gw.degraded_calls, 1)
        last = rec.spans[-1]
        self.assertTrue(last.degraded)

    def test_permanent_error_also_tries_fallback(self) -> None:
        gw, _ = self._gateway(
            {"primary": [LLMPermanent("401")], "fallback": ["ok"]}, max_retries=2
        )
        resp = asyncio.run(gw.complete(_req()))
        self.assertTrue(resp.degraded)

    def test_raises_when_all_models_fail(self) -> None:
        gw, _ = self._gateway(
            {"primary": [LLMTransient("500")] * 2, "fallback": [LLMTransient("500")] * 2},
            max_retries=2,
        )
        with self.assertRaises(LLMUnavailable):
            asyncio.run(gw.complete(_req()))


# ---------------------------------------------------------------- 打回闭环


def _fact(chapter: int) -> dict[str, Any]:
    return {
        "claim": f"第{chapter}章要点",
        "source_id": f"doc_{chapter:02d}",
        "page": 10,
        "quote": f"第{chapter}章原文出处",
    }


class FixableAgents:
    """review 前 N 次判 revise，之后 pass。用来验证打回闭环能否收敛。"""

    def __init__(self, *, revise_times: int) -> None:
        self.revise_times = revise_times
        self.review_calls: dict[int, int] = {}
        self.write_calls: dict[int, int] = {}

    def registry(self) -> AgentRegistry:
        def planner(ctx: AgentContext) -> dict[str, Any]:
            return {
                "outline": [
                    {"chapter": i, "title": f"第{i}章", "queries": [f"q{i}"]}
                    for i in (1, 2)
                ]
            }

        def research(ctx: AgentContext) -> dict[str, Any]:
            ch = int(ctx.node.payload["chapter"])
            return {
                "chapter": ch,
                "summary": "s",
                "key_facts": [_fact(ch)],
                "open_questions": [],
                "dropped_facts": 0,
            }

        def write(ctx: AgentContext) -> dict[str, Any]:
            ch = int(ctx.node.payload["chapter"])
            self.write_calls[ch] = self.write_calls.get(ch, 0) + 1
            return {
                "chapter": ch,
                "markdown": f"## 第{ch}章\n\n正文[^doc_{ch:02d}]",
                "citation_map": {f"doc_{ch:02d}": 10},
                "used_facts": [_fact(ch)],
            }

        def review(ctx: AgentContext) -> dict[str, Any]:
            ch = int(ctx.node.payload["chapter"])
            n = self.review_calls.get(ch, 0) + 1
            self.review_calls[ch] = n
            if n <= self.revise_times:
                return {
                    "chapter": ch,
                    "verdict": "revise",
                    "defects": [
                        {
                            "type": "assertion_mismatch",
                            "claim": f"第{ch}章要点",
                            "reason": "与原文不一致（模拟）",
                            "fix_target": "write",
                        }
                    ],
                    "fix_target": "write",
                    "hint": "请按原文修正数字",
                }
            return {"chapter": ch, "verdict": "pass", "defects": []}

        class _Node:
            def __init__(self, fn, node_type):
                self._fn = fn
                self.node_type = node_type

            async def execute(self, ctx):
                return self._fn(ctx)

        return AgentRegistry(
            {
                NodeType.PLANNER: _Node(planner, NodeType.PLANNER),
                NodeType.RESEARCH: _Node(research, NodeType.RESEARCH),
                NodeType.WRITE: _Node(write, NodeType.WRITE),
                NodeType.REVIEW: _Node(review, NodeType.REVIEW),
            }
        )


class TestRevisionLoop(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="reportflow-rev-"))
        self.settings = Settings(
            database_url=f"sqlite:///{(self.tmpdir / 'r.db').as_posix()}",
            max_concurrency=2,
            node_timeout_s=5,
            max_retries=0,
            retry_backoff_s=0.01,
            poll_interval_s=0.01,
            max_review_rounds=2,
            review_in_pipeline=True,  # 本组用例专测打回闭环
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, agents: FixableAgents):
        rt = build_runtime(self.settings, profile="mock")
        rt.runner.registry = agents.registry()
        task_id, _ = rt.runner.submit(topic="x", sections=2)
        result = asyncio.run(rt.runner.run(task_id))
        return rt, task_id, result

    def test_revise_triggers_rewrite_and_converges(self) -> None:
        agents = FixableAgents(revise_times=1)
        rt, task_id, result = self._run(agents)
        nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        self.assertEqual(nodes["write_ch1"].attempt, 2, "被打回后应重写一次")
        self.assertEqual(nodes["review_ch1"].review_round, 1)
        self.assertIn("按原文修正", nodes["write_ch1"].revision_hint or "")
        self.assertIs(result.task_status, TaskStatus.DONE)
        self.assertEqual(agents.write_calls[1], 2)

    def test_gives_up_at_cap_and_marks_partial(self) -> None:
        agents = FixableAgents(revise_times=99)
        rt, task_id, result = self._run(agents)
        nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        self.assertEqual(nodes["review_ch1"].review_round, 2, "打回轮数应停在 max_review_rounds")
        self.assertIs(result.task_status, TaskStatus.PARTIAL)
        # 收敛性：每章最多 max_review_rounds+1 次 review
        self.assertLessEqual(agents.review_calls[1], 3)


if __name__ == "__main__":
    unittest.main()
