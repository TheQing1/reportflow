"""装配层把检索模式接进 runtime 的验证。

存在的理由：rag 层把三档检索做好了，但**装配路径**忘了接线，
是这类改动最常见的失败方式——工厂函数单测全绿，生产却永远只跑 bm25。
所以这里从 `build_runtime` 一路验到 `rt.retriever` 的实际类型。

全程不联网：hybrid/vector 档不配 key 时会自动降级为 bm25（这本身也是一条要测的行为）。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from app.assembler import build_runtime
from app.core.config import Settings
from app.rag import HybridRetriever, LocalCorpusRetriever

#: 一份小语料，够检索出结果即可
DOCS = {
    "doc_a.md": "# 装机规模\n\n2025 年工商业储能新增装机约 8.4GWh，同比增长 62%。\n",
    "doc_b.md": "# 区域分布\n\n浙江、广东、江苏三省合计占比 55% 以上。\n",
}


class AssemblyRetrievalModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rfasm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.docs = self.tmp / "docs"
        self.docs.mkdir(parents=True)
        for name, body in DOCS.items():
            (self.docs / name).write_text(body, encoding="utf-8")

    def _settings(self, **over) -> Settings:
        base = Settings(
            database_url=f"sqlite:///{(self.tmp / 'a.db').as_posix()}",
            docs_dir=str(self.docs),
            llm_api_key="",
        )
        return base.with_overrides(**over)

    def test_default_mode_is_bm25(self) -> None:
        """默认档必须是 LocalCorpusRetriever：历史评测基线就是它。"""
        rt = build_runtime(self._settings(), profile="real", announce=False)
        self.assertIsInstance(rt.retriever, LocalCorpusRetriever)

    def test_hybrid_mode_is_wired_through(self) -> None:
        """retrieval_mode=hybrid 必须真的到 rt.retriever，而不是被静默忽略。"""
        rt = build_runtime(self._settings(retrieval_mode="hybrid"), profile="real", announce=False)
        self.assertIsInstance(rt.retriever, HybridRetriever)
        # 没配 embedding key → 优雅降级，但对象本身仍是 hybrid（可观测）
        self.assertEqual(rt.retriever.effective_mode, "bm25")
        self.assertTrue(rt.retriever.degraded_reason)

    def test_vector_mode_is_wired_through(self) -> None:
        rt = build_runtime(self._settings(retrieval_mode="vector"), profile="real", announce=False)
        self.assertIsInstance(rt.retriever, HybridRetriever)
        self.assertEqual(rt.retriever.effective_mode, "bm25")

    def test_degraded_hybrid_behaves_like_bm25(self) -> None:
        """降级档的检索结果必须与纯 bm25 完全一致（text 与顺序都比）。"""
        import asyncio

        rt_bm25 = build_runtime(
            self._settings(retrieval_mode="bm25"), profile="real", announce=False
        )
        rt_hy = build_runtime(
            self._settings(retrieval_mode="hybrid"), profile="real", announce=False
        )
        a = asyncio.run(rt_bm25.retriever.search("储能 装机规模", top_k=2))
        b = asyncio.run(rt_hy.retriever.search("储能 装机规模", top_k=2))
        self.assertEqual([x.doc_id for x in a], [x.doc_id for x in b])
        self.assertEqual([x.text for x in a], [x.text for x in b])

    def test_unknown_mode_fails_fast(self) -> None:
        """配错的模式必须在启动时炸，而不是悄悄跑一份质量下降的报告。"""
        with self.assertRaises(ValueError):
            build_runtime(self._settings(retrieval_mode="bogus"), profile="real", announce=False)

    def test_reload_works_on_new_retriever(self) -> None:
        """上传文档后 api 会调 reload()；hybrid 档也必须支持，且不联网。"""
        rt = build_runtime(self._settings(retrieval_mode="hybrid"), profile="real", announce=False)
        before = rt.retriever.corpus_size()
        (self.docs / "doc_c.md").write_text(
            "# 新增\n\n2026 年预计新增 13GWh，增速回落至 55%。\n", encoding="utf-8"
        )
        after = rt.retriever.reload()
        self.assertGreater(after, before)
        self.assertEqual(after, rt.retriever.corpus_size())


if __name__ == "__main__":
    unittest.main()
