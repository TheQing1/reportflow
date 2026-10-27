"""阶段三：评测器自身的正确性。

评测代码错了比被评测系统错了更糟——一个算错的指标会被当成结论写进报告。
所以这里重点测"口径"：重复率怎么算、角标有效率怎么算、数据集校验是否会拦住脏数据。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.contracts.eval import TopicCase
from app.contracts.nodes import NodeType
from app.evaluation import load_dataset
from app.evaluation.dataset import DatasetError, load_case
from app.evaluation.judge import Judge
from app.evaluation.report import (
    citation_validity,
    collect_from_text,
    must_cite_hit,
)
from app.llm import ScriptedLLM, json_handler
from app.orchestrator.graph_langgraph import build_report_graph


class TestReportMetrics(unittest.TestCase):
    def test_repeat_rate_counts_sources_shared_across_chapters(self) -> None:
        text = "## 第一章\n\n甲说[^doc_01]，乙说[^doc_02]。\n\n## 第二章\n\n又说了一遍[^doc_01]。\n"
        artifact = collect_from_text(text)
        # doc_01 被两章引用，doc_02 只被一章引用 → 1/2
        self.assertAlmostEqual(artifact.repeat_rate, 0.5)
        self.assertEqual(artifact.distinct_sources, 2)

    def test_repeat_rate_is_zero_for_single_chapter(self) -> None:
        artifact = collect_from_text("## 一\n\n[^doc_01] [^doc_02]")
        self.assertEqual(artifact.repeat_rate, 0.0)

    def test_citation_validity_ignores_duplicates_in_count(self) -> None:
        artifact = collect_from_text("## 一\n\n[^doc_01][^doc_01][^doc_99]")
        valid, total = citation_validity(artifact, {"doc_01"})
        self.assertEqual((valid, total), (2, 3))

    def test_must_cite_hit(self) -> None:
        artifact = collect_from_text("## 一\n\n[^doc_01]")
        self.assertEqual(must_cite_hit(artifact, ("doc_01", "doc_02")), 0.5)
        self.assertEqual(must_cite_hit(artifact, ()), 1.0)

    def test_chapter_split_keeps_heading(self) -> None:
        artifact = collect_from_text("## 一\n\n正文A\n\n## 二\n\n正文B")
        self.assertEqual(len(artifact.chapter_texts), 2)
        self.assertTrue(artifact.chapter_texts[0].startswith("## 一"))


class TestDatasetValidation(unittest.TestCase):
    def _write(self, content: str) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="eval-ds-"))
        path = tmp / "case.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_missing_field_rejected(self) -> None:
        path = self._write("id: x\ntopic: y\n")
        with self.assertRaises(DatasetError):
            load_case(path)

    def test_empty_must_cover_rejected(self) -> None:
        path = self._write("id: x\ntopic: y\nmust_cover: []\n")
        with self.assertRaises(DatasetError):
            load_case(path)

    def test_duplicate_ids_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="eval-ds2-"))
        body = "id: same\ntopic: t\nmust_cover:\n  - 甲\n"
        for name in ("a.yaml", "b.yaml"):
            (tmp / name).write_text(body, encoding="utf-8")
        with self.assertRaises(DatasetError):
            load_dataset(tmp)

    def test_real_dataset_loads(self) -> None:
        root = Path(__file__).resolve().parents[2] / "eval" / "topics"
        if not root.is_dir():
            self.skipTest("评测集目录缺失")
        cases = load_dataset(root)
        self.assertGreaterEqual(len(cases), 8)
        for case in cases:
            self.assertTrue(case.must_cover)
            self.assertTrue(case.topic)


class TestJudgeParsing(unittest.TestCase):
    def _judge(self, payload: dict) -> Judge:
        return Judge(ScriptedLLM({"judge": json_handler(payload)}))

    def test_matches_items_back_from_prefix(self) -> None:
        case = TopicCase(
            id="t",
            topic="x",
            must_cover=("2025年新增装机约8.4GWh，同比增长62%",),
            forbidden=("装机同比下降",),
        )
        judge = self._judge(
            {
                "coverage": [
                    {"item": "2025年新增装机约8.4GWh，同比", "covered": True, "evidence": "原文"}
                ],
                "forbidden": [{"item": "装机同比下降", "asserted": False}],
            }
        )
        result = asyncio.run(judge.assess(case, "正文"))
        self.assertEqual(len(result.coverage), 1)
        self.assertTrue(result.coverage[0].covered)
        self.assertEqual(result.forbidden_hits, [])

    def test_missing_items_default_to_not_covered(self) -> None:
        case = TopicCase(id="t", topic="x", must_cover=("甲要点", "乙要点"))
        judge = self._judge({"coverage": [{"item": "甲要点", "covered": True}], "forbidden": []})
        result = asyncio.run(judge.assess(case, "正文"))
        self.assertEqual([c.covered for c in result.coverage], [True, False])

    def test_forbidden_asserted_is_reported(self) -> None:
        case = TopicCase(id="t", topic="x", must_cover=("甲",), forbidden=("行业CR5超过80%",))
        judge = self._judge(
            {
                "coverage": [{"item": "甲", "covered": True}],
                "forbidden": [{"item": "行业CR5超过80%", "asserted": True}],
            }
        )
        result = asyncio.run(judge.assess(case, "正文"))
        self.assertEqual(result.forbidden_hits, ["行业CR5超过80%"])


class TestArmGraphs(unittest.TestCase):
    def test_b_arm_has_no_review_nodes(self) -> None:
        graph = build_report_graph(topic="x", sections=3, with_review=False)
        types = {n.type for n in graph}
        self.assertNotIn(NodeType.REVIEW, types)
        # 串行链仍存在，只是挂在 write 自己身上
        self.assertEqual(graph.get("write_ch2").deps, ("research_ch2", "write_ch1"))

    def test_c_arm_keeps_review_chain(self) -> None:
        graph = build_report_graph(topic="x", sections=3, with_review=True)
        self.assertEqual(len(graph), 1 + 3 + 3 + 3)
        self.assertEqual(graph.get("write_ch2").deps, ("research_ch2", "review_ch1"))


class TestStats(unittest.TestCase):
    def test_t_critical_lookup(self) -> None:
        from app.evaluation.stats import t_critical

        self.assertAlmostEqual(t_critical(19), 2.093, places=3)
        self.assertAlmostEqual(t_critical(1), 2.571, places=3)
        self.assertAlmostEqual(t_critical(500), 1.96, places=3)

    def test_paired_identical_is_not_significant(self) -> None:
        from app.evaluation.stats import paired

        a = {"x": [0.5], "y": [0.7], "z": [0.6]}
        r = paired(a, dict(a), label="A→B")
        self.assertEqual(r.n, 3)
        self.assertAlmostEqual(r.mean_diff, 0.0)
        self.assertFalse(r.significant)

    def test_paired_consistent_gain_is_significant(self) -> None:
        from app.evaluation.stats import paired

        a = {f"c{i}": [0.5] for i in range(20)}
        b = {f"c{i}": [0.6] for i in range(20)}
        r = paired(a, b, label="A→B")
        self.assertAlmostEqual(r.mean_diff, 0.1, places=6)
        self.assertTrue(r.significant, "所有主题同向提升 10pp，应判为显著")

    def test_paired_noisy_gain_is_not_significant(self) -> None:
        from app.evaluation.stats import paired

        # 有升有降，均值略正 → CI 应跨 0
        a = {f"c{i}": [0.5] for i in range(20)}
        b = {f"c{i}": [0.9 if i % 2 else 0.2] for i in range(20)}
        r = paired(a, b, label="A→B")
        self.assertFalse(r.significant, "波动巨大时不应判为显著")

    def test_paired_drops_cases_missing_on_either_side(self) -> None:
        from app.evaluation.stats import paired

        r = paired({"x": [0.5], "y": [0.5]}, {"x": [0.6]}, label="A→B")
        self.assertEqual(r.n, 1)
        self.assertFalse(r.significant, "样本不足时不应给结论")

    def test_ci95_narrows_with_more_samples(self) -> None:
        from app.evaluation.stats import ci95

        small = ci95([0.4, 0.6])
        large = ci95([0.4, 0.6] * 20)
        self.assertGreater(small[1] - small[0], large[1] - large[0])


class TestSemanticSupport(unittest.TestCase):
    def test_sampling_is_per_chapter_and_skips_short(self) -> None:
        from app.evaluation.semantic import sample_assertions

        chapters = [
            "## 一\n\n第一个有效断言内容[^doc_01]。第二个有效断言内容[^doc_02]。第三个也要抽到吗[^doc_03]。短[^doc_04]。",
            "## 二\n\n第二章的断言内容在这里[^doc_05]。",
        ]
        picked = sample_assertions(chapters, per_chapter=2)
        self.assertEqual(len(picked), 3, "每章最多 2 条，第二章只有 1 条")
        self.assertEqual([a.chapter for a in picked], [1, 1, 2])
        self.assertEqual(picked[0].source_id, "doc_01")
        self.assertTrue(all(len(a.sentence) >= 4 for a in picked))

    def test_audit_scores_supported_as_one_and_weak_as_half(self) -> None:
        from app.evaluation.semantic import Assertion, SupportAuditor

        llm = ScriptedLLM(
            {
                "audit": json_handler(
                    {
                        "items": [
                            {"idx": 1, "verdict": "supported", "reason": "一致"},
                            {"idx": 2, "verdict": "weak", "reason": "口径不同"},
                            {"idx": 3, "verdict": "unsupported", "reason": "矛盾"},
                        ]
                    }
                )
            }
        )
        auditor = SupportAuditor(llm)
        report = asyncio.run(
            auditor.audit(
                [
                    Assertion(1, "断言一内容", "doc_01"),
                    Assertion(1, "断言二内容", "doc_01"),
                    Assertion(2, "断言三内容", "doc_02"),
                ],
                lambda doc_id: "原文内容",
            )
        )
        self.assertEqual(report.total, 3)
        self.assertEqual((report.supported, report.weak, report.unsupported), (1, 1, 1))
        self.assertAlmostEqual(report.support_rate, 0.5)
        self.assertEqual(len([i for i in report.items if i.verdict != "supported"]), 2)

    def test_missing_items_counted_as_skipped_not_unsupported(self) -> None:
        """judge 漏写一条，不能当成"不支持"——否则模型少写一行就凭空拉低分数。"""
        from app.evaluation.semantic import Assertion, SupportAuditor

        llm = ScriptedLLM({"audit": json_handler({"items": [{"idx": 1, "verdict": "supported"}]})})
        report = asyncio.run(
            SupportAuditor(llm).audit(
                [Assertion(1, "断言一内容", "doc_01"), Assertion(1, "断言二内容", "doc_01")],
                lambda doc_id: "原文",
            )
        )
        self.assertEqual(report.total, 1)
        self.assertEqual(report.skipped, 1)
        self.assertAlmostEqual(report.support_rate, 1.0)

    def test_audit_failure_does_not_crash(self) -> None:
        from app.evaluation.semantic import Assertion, SupportAuditor

        class Boom:
            async def complete(self, req):
                raise RuntimeError("judge down")

        report = asyncio.run(
            SupportAuditor(Boom()).audit([Assertion(1, "断言一内容", "doc_01")], lambda d: "x")
        )
        self.assertEqual(report.total, 0)
        self.assertIn("judge down", report.error or "")

    def test_empty_assertions_returns_empty_report(self) -> None:
        from app.evaluation.semantic import SupportAuditor

        report = asyncio.run(SupportAuditor(ScriptedLLM({})).audit([], lambda d: ""))
        self.assertEqual(report.total, 0)
        self.assertIsNone(report.error)


class TestRunKeyUniqueness(unittest.TestCase):
    """批次指纹必须能区分前缀相同、后缀相同的 run_id。"""

    def test_run_key_differs_for_similar_ids(self) -> None:
        from app.evaluation.runner import EvalRunner

        def key(run_id: str) -> str:
            stub = SimpleNamespace(run_id=run_id, _run_key=None)
            return EvalRunner._run_key(stub)  # type: ignore[arg-type]

        # 这两个 id 截尾 6 位都是 "-smoke"，旧实现会让新批次复用旧任务
        self.assertNotEqual(key("audit-smoke"), key("judge-smoke"))
        self.assertEqual(key("v4"), key("v4"))
        self.assertEqual(len(key("v4")), 8)


if __name__ == "__main__":
    unittest.main()


class ClaimRepeatTest(unittest.TestCase):
    """论述重复率：必须区分「共享引用」和「复读」。

    锁住它的理由：前两轮去重实验以来源复用率为目标全部失败，
    而来源复用率比论述重复率高约 4 倍——目标定错了。
    """

    def _artifact(self, chapters: list[str]):
        from app.evaluation.report import ReportArtifact

        return ReportArtifact(text="\n## ".join(chapters), chapter_texts=list(chapters))

    def test_shared_source_is_not_counted(self) -> None:
        # 两章引用同一篇文档，但讲的是完全不同的数据 → 不算复读
        art = self._artifact(
            [
                "2025 年新增装机达到 8.4GWh，同比增长 62% 规模持续扩张[^doc_a]。",
                "系统集成环节 CR5 约为 38%，较上年提升 4 个百分点[^doc_a]。",
            ]
        )
        from app.evaluation.report import _CITE

        for idx, ch in enumerate(art.chapter_texts, 1):
            for t in _CITE.findall(ch):
                art.source_chapters.setdefault(t.strip(), set()).add(idx)
        self.assertEqual(art.repeat_rate, 1.0)  # 来源复用 100%
        self.assertLess(art.claim_repeat, 0.05)  # 但论述几乎不重复

    def test_verbatim_repetition_is_caught(self) -> None:
        sentence = "2025年储能系统集成环节的市场集中度仍然偏低，CR5约为38%，较上一年提升4个百分点"
        art = self._artifact([f"{sentence}[^doc_a]。", f"{sentence}[^doc_b]。"])
        self.assertGreater(art.claim_repeat, 0.8)

    def test_single_chapter_is_zero(self) -> None:
        art = self._artifact(["只有一章的内容，没有任何跨章可比对对象[^doc_a]。"])
        self.assertEqual(art.claim_repeat, 0.0)


class RelevantWindowTest(unittest.TestCase):
    """审计取原文时必须切到「与断言相关」的那一段。

    曾经直接取 source[:600]，在长文档上系统性误判：
    一篇 16947 字的发布会实录，断言数字在第 3761 字之后，
    前 600 字只有开场白，judge 一律判「原文未提及」——
    模型明明引对了却报编造，这类误报比漏报更伤。
    """

    def _long_doc(self) -> str:
        filler = "会议开始，主持人介绍出席领导和会议议程。" * 120
        return filler + "截至2025年底4小时及以上储能装机占比达27.6%，同比提高12个百分点。" + filler

    def test_window_contains_the_number(self) -> None:
        from app.evaluation.semantic import _relevant_window

        doc = self._long_doc()
        window = _relevant_window("4小时及以上储能装机占比达27.6%", doc)
        self.assertIn("27.6%", window)
        self.assertLess(len(window), len(doc))

    def test_short_doc_is_returned_whole(self) -> None:
        from app.evaluation.semantic import _relevant_window

        doc = "2025年新增装机8.4GWh，同比增长62%。"
        self.assertEqual(_relevant_window("新增装机8.4GWh", doc), doc)

    def test_falls_back_when_no_number(self) -> None:
        from app.evaluation.semantic import _relevant_window

        doc = "甲" * 3000
        # 断言里没有数字 → 定位不到，退回开头且不超过上限的两倍
        self.assertLessEqual(len(_relevant_window("这段没有任何数字", doc)), 1200)
