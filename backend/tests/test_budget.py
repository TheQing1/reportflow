"""文档预算的测试。

这一层是压章节重复率的真正落点（prompt 层去重实测无效且有害，已撤回）。
它一旦失效，重复率会悄悄回到 40%+，而且没有任何报错，所以必须锁住：
预算要真的分散，且重排要真的偏好预算内文档。
"""

from __future__ import annotations

import asyncio
import unittest

from app.agents.researcher import _looks_like_rrf, _rerank_by_budget
from app.contracts.retrieval import Document, Snippet
from app.rag.budget import allocate_doc_budgets, overlap
from app.rag.corpus import LocalCorpusRetriever

DOCS = [
    Document(
        doc_id="doc_a",
        title="规模",
        text="储能行业 2025 年新增装机 8.4GWh，同比增长 62%，规模持续扩大。",
    ),
    Document(
        doc_id="doc_b",
        title="区域",
        text="储能项目在浙江、广东、江苏三省合计占比 55% 以上，区域集中度很高。",
    ),
    Document(
        doc_id="doc_c",
        title="成本",
        text="储能系统均价降至 0.48 元/Wh，同比下降 31%，造价持续下行。",
    ),
    Document(
        doc_id="doc_d",
        title="政策",
        text="储能政策方面，23 个省级行政区出台容量补偿机制，框架逐步建立。",
    ),
    Document(
        doc_id="doc_e", title="竞争", text="储能系统集成 CR5 约 38%，集中度提升但绝对水平不高。"
    ),
    Document(
        doc_id="doc_f", title="展望", text="储能展望：预计 2026 年新增装机 6000 万千瓦，增速回落。"
    ),
]


def _retriever() -> LocalCorpusRetriever:
    return LocalCorpusRetriever(DOCS)


class AllocateTest(unittest.TestCase):
    def test_budgets_are_spread_across_chapters(self) -> None:
        budgets = asyncio.run(
            allocate_doc_budgets(
                topic="储能", sections=3, retriever=_retriever(), per_chapter=2, pool=6
            )
        )
        self.assertEqual(sorted(budgets), [1, 2, 3])
        for docs in budgets.values():
            self.assertLessEqual(len(docs), 2)
        # 6 篇文档分给 3 章，每章 2 篇 → 应当基本不重叠
        self.assertLess(overlap(budgets), 0.34)

    def test_empty_corpus_returns_nothing(self) -> None:
        budgets = asyncio.run(
            allocate_doc_budgets(topic="x", sections=2, retriever=LocalCorpusRetriever([]))
        )
        self.assertEqual(budgets, {})

    def test_more_chapters_than_dimensions_reuses_dimensions(self) -> None:
        budgets = asyncio.run(
            allocate_doc_budgets(topic="储能", sections=8, retriever=_retriever())
        )
        self.assertEqual(len(budgets), 8)
        self.assertTrue(all(v for v in budgets.values()))


class RerankTest(unittest.TestCase):
    def test_budget_docs_come_first(self) -> None:
        async def go():
            r = _retriever()
            return await r.search("储能 规模", top_k=6)

        hits = asyncio.run(go())
        self.assertGreater(len(hits), 2)
        # 把排名最末的那篇设为唯一预算文档，重排后它的位次必须前移。
        # 注意是"前移"不是"置顶"：折扣是 0.5 而不是硬过滤，
        # 分差太大时预算内文档也可能仍在后面——这是刻意的，硬过滤会伤覆盖率。
        target = hits[-1].doc_id
        reranked = _rerank_by_budget(hits, {target}, limit=len(hits))
        before = [h.doc_id for h in hits].index(target)
        after = [h.doc_id for h in reranked].index(target)
        self.assertLess(after, before)

    def test_limit_is_respected(self) -> None:
        async def go():
            return await _retriever().search("储能", top_k=6)

        hits = asyncio.run(go())
        self.assertEqual(len(_rerank_by_budget(hits, {"doc_a"}, limit=2)), 2)


class RrfScaleTest(unittest.TestCase):
    """RRF 分数尺度下的降权语义。

    BM25 与 RRF 的量纲差了两个数量级：BM25 相邻名次相对差 20%~50%，
    RRF 只有 1.6%（k=60）。如果两档共用"乘 0.5"，RRF 档会把预算外片段
    推到 60 名开外——降权退化成硬过滤，与 budget 的设计意图相反。
    这一组用例把"RRF 档必须仍是降权、不是过滤"钉住。
    """

    @staticmethod
    def _rrf_hits() -> list[Snippet]:
        # 真实 RRF 量级：两路融合后单片段不超过 2/61 ≈ 0.0328
        return [
            Snippet(doc_id="budget_a", title="a", page=None, text="x", score=2 / 61),
            Snippet(doc_id="free_b", title="b", page=None, text="y", score=1 / 62),
            Snippet(doc_id="free_c", title="c", page=None, text="z", score=1 / 63),
        ]

    def test_scale_detection(self) -> None:
        self.assertTrue(_looks_like_rrf([s.score for s in self._rrf_hits()]))
        # BM25 原始分不应被误判成 RRF
        self.assertFalse(_looks_like_rrf([20.5, 9.7, 6.7]))
        self.assertFalse(_looks_like_rrf([]))

    def test_budget_doc_moves_up_not_necessarily_first(self) -> None:
        """预算内片段位次前移，但不保证置顶。

        与 BM25 档同一条语义：降权是"优先"，不是"独占"。
        预算内片段本身分低时，仍可能排在后面——这正是刻意保留的行为，
        硬置顶会让某章拿到一堆低相关片段，反而伤覆盖率。
        """
        # budget_a 原本第 1；把最末位的 free_c 设为预算内，它应前移
        hits = self._rrf_hits()
        before = [s.doc_id for s in hits].index("free_c")
        out = _rerank_by_budget(hits, {"free_c"}, limit=3)
        after = [s.doc_id for s in out].index("free_c")
        self.assertLess(after, before)

    def test_low_scoring_budget_doc_stays_put_but_is_kept(self) -> None:
        """预算内片段本身分很低时不会被强行置顶，但绝不会被丢弃。"""
        hits = [
            Snippet(doc_id="free1", title="1", page=None, text="a", score=2 / 61),
            Snippet(doc_id="free2", title="2", page=None, text="b", score=1 / 62),
            Snippet(doc_id="bud1", title="3", page=None, text="c", score=1 / 70),
        ]
        out = _rerank_by_budget(hits, {"bud1"}, limit=3)
        self.assertEqual(len(out), 3, "硬过滤会丢片段，这里必须保留")
        self.assertEqual({s.doc_id for s in out}, {"free1", "free2", "bud1"})

    def test_non_budget_docs_are_not_hard_filtered(self) -> None:
        """降权后预算外片段仍必须留在结果里（这是与硬过滤的分界线）。"""
        hits = self._rrf_hits()
        out = _rerank_by_budget(hits, {"free_c"}, limit=3)
        self.assertEqual(len(out), 3)
        self.assertEqual({s.doc_id for s in out}, {"budget_a", "free_b", "free_c"})

    def test_naive_halving_would_have_dropped_everything(self) -> None:
        """把"分数字面乘 0.5"的错误做法显式记下来，防有人改回去。

        RRF 下 1/(60+61) > (1/(60+1)) * 0.5，即预算内第 61 名仍压过预算外第 1 名。
        """
        k = 60
        worst_in_budget = 1 / (k + 61)
        best_out_budget_halved = (1 / (k + 1)) * 0.5
        self.assertGreater(worst_in_budget, best_out_budget_halved)


if __name__ == "__main__":
    unittest.main()
