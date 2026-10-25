"""文档预算的测试。

这一层是压章节重复率的真正落点（prompt 层去重实测无效且有害，已撤回）。
它一旦失效，重复率会悄悄回到 40%+，而且没有任何报错，所以必须锁住：
预算要真的分散，且重排要真的偏好预算内文档。
"""

from __future__ import annotations

import asyncio
import unittest

from app.agents.researcher import _rerank_by_budget
from app.contracts.retrieval import Document
from app.rag.budget import allocate_doc_budgets, overlap
from app.rag.corpus import LocalCorpusRetriever

DOCS = [
    Document(doc_id="doc_a", title="规模", text="储能行业 2025 年新增装机 8.4GWh，同比增长 62%，规模持续扩大。"),
    Document(doc_id="doc_b", title="区域", text="储能项目在浙江、广东、江苏三省合计占比 55% 以上，区域集中度很高。"),
    Document(doc_id="doc_c", title="成本", text="储能系统均价降至 0.48 元/Wh，同比下降 31%，造价持续下行。"),
    Document(doc_id="doc_d", title="政策", text="储能政策方面，23 个省级行政区出台容量补偿机制，框架逐步建立。"),
    Document(doc_id="doc_e", title="竞争", text="储能系统集成 CR5 约 38%，集中度提升但绝对水平不高。"),
    Document(doc_id="doc_f", title="展望", text="储能展望：预计 2026 年新增装机 6000 万千瓦，增速回落。"),
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
        for i, docs in budgets.items():
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


if __name__ == "__main__":
    unittest.main()
