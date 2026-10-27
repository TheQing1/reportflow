"""混合检索的测试。

三条主线，都**绝不联网**：
1. RRF 融合的数学与顺序 —— 融合算错不会报错，只会排序悄悄变差，所以逐条锁死；
2. 三档模式切换 + 无 key 降级 —— 降级必须是"行为等价于 BM25"，不是"看起来没崩"；
3. 溯源不破坏 —— 返回的每个 text 必须是某个原文 chunk 的**逐字**子串。

embedding 用 `_FakeEmbeddingTransport`（按文本哈希造确定性向量）替换真实 HTTP，
并接上真实的 `Embedder` 缓存逻辑，这样缓存命中/缺失也能一起验。
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from app.contracts.retrieval import Document, Retriever, Snippet
from app.core.config import Settings
from app.rag import (
    Embedder,
    EmbeddingCacheMode,
    EmbeddingReplayMiss,
    HybridRetriever,
    LocalCorpusRetriever,
    build_hybrid_from_documents,
    build_retriever,
    embed_key,
    rrf_fuse,
)
from app.rag.embedding import EmbeddingTransient
from app.rag.hybrid import chunk_key
from app.rag.vector_index import IndexedChunk, VectorIndex, cosine

DIM = 8

DOCS: list[Document] = [
    Document(
        doc_id="policy",
        title="政策与规划",
        text=(
            "国家层面的顶层设计已经明确。到2027年全国新型储能装机规模达到1.8亿千瓦以上。\n\n"
            "省级补贴政策以容量补偿为主，部分地区按放电量给予度电补贴。"
        ),
    ),
    Document(
        doc_id="scale",
        title="装机规模",
        text=(
            "2025年新增装机容量约8.4GWh，同比增长62%。\n\n"
            "户用储能受限于居民电价峰谷差较小，商业化模式尚未跑通。"
        ),
    ),
    Document(
        doc_id="cost",
        title="成本与经济性",
        text=(
            "电芯成本占系统成本的比重超过一半，碳酸锂价格波动直接决定项目收益率。\n\n"
            "工商业储能通过峰谷套利回收期普遍在五到七年。"
        ),
    ),
]


def _vec_of(text: str, dim: int = DIM) -> list[float]:
    """确定性伪向量：只依赖文本内容，同一文本永远同一向量。

    不是随机数——随机的话"断言排序结果"就没法写，而排序正是这里最该测的东西。
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [b / 255.0 - 0.5 for b in digest[:dim]]


class _FakeEmbeddingTransport:
    """按文本内容返回确定性向量。绝不联网。"""

    def __init__(self, *, fails: int = 0) -> None:
        self.calls = 0
        self.batches: list[list[str]] = []
        self._fails = fails  # 前 N 次抛可重试错误，用于测重试

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        self.calls += 1
        if self._fails > 0:
            self._fails -= 1
            raise EmbeddingTransient("测试：模拟网络抖动")
        self.batches.append(list(texts))
        return [_vec_of(t) for t in texts]

    async def aclose(self) -> None:
        pass


class _ClusteredTransport:
    """把语义相近的文本映射到相近向量，用来验证"向量真的影响了排序"。

    规则：向量第一维由命中关键词决定，其余维度来自文本哈希。
    这样"含某个词的片段"会聚成一簇，纯向量检索的排序就是可预测的。
    """

    #: 词 -> 簇方向
    CLUSTERS = {
        "政策": [1.0, 0.0, 0.0, 0.0],
        "装机": [0.0, 1.0, 0.0, 0.0],
        "成本": [0.0, 0.0, 1.0, 0.0],
    }

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        self.calls += 1
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0, 0.0, 0.0, 0.0]
            for word, direction in self.CLUSTERS.items():
                if word in text:
                    vec = [a + b for a, b in zip(vec, direction, strict=True)]
            if not any(vec):
                vec = [0.0, 0.0, 0.0, 0.0]
            # 加一点文本自身的噪声，保证同簇内也有稳定次序
            out.append([*vec, *(_vec_of(text, 4))])
        return out

    async def aclose(self) -> None:
        pass


def _embedder(
    transport: object,
    *,
    cache_dir: Path,
    mode: EmbeddingCacheMode = EmbeddingCacheMode.LIVE,
    model: str = "fake-embed",
    batch_size: int = 2,
) -> Embedder:
    return Embedder(
        transport,  # type: ignore[arg-type]
        mode=mode,
        cache_dir=cache_dir,
        model=model,
        batch_size=batch_size,
        backoff_s=0.0,  # 测试里不要真的等
    )


def _run(coro):
    return asyncio.run(coro)


class RrfFuseTest(unittest.TestCase):
    """RRF 公式：score = Σ 1/(k + rank)，rank 从 1 开始。"""

    def test_score_matches_formula(self) -> None:
        fused = dict(rrf_fuse([["a", "b"]], k=60))
        self.assertAlmostEqual(fused["a"], 1 / 61)
        self.assertAlmostEqual(fused["b"], 1 / 62)

    def test_rank_starts_at_one_not_zero(self) -> None:
        # 从 0 开始会得到 1/60 而不是 1/61；这个偏差不会报错，只会让权重整体偏大
        fused = dict(rrf_fuse([["x"]], k=60))
        self.assertAlmostEqual(fused["x"], 1 / 61)
        self.assertNotAlmostEqual(fused["x"], 1 / 60)

    def test_agreement_across_lists_outranks_single_list_winner(self) -> None:
        """两路都排第 2 的条目，应胜过只有一路排第 1 的条目——RRF 的核心主张。"""
        bm25 = ["solo", "both"]
        vector = ["other", "both"]
        order = [key for key, _ in rrf_fuse([bm25, vector], k=60)]
        self.assertEqual(order[0], "both")
        self.assertEqual(
            dict(rrf_fuse([bm25, vector], k=60))["both"],
            2 * (1 / 62),
        )

    def test_predicted_full_order(self) -> None:
        """手工算一遍两路融合的完整顺序，验证实现没有额外魔改。"""
        bm25 = ["a", "b", "c"]
        vector = ["b", "c", "a"]
        got = rrf_fuse([bm25, vector], k=60)
        expected = {
            "b": 1 / 62 + 1 / 61,  # BM25 第 2 + 向量第 1
            "a": 1 / 61 + 1 / 63,  # BM25 第 1 + 向量第 3
            "c": 1 / 63 + 1 / 62,  # BM25 第 3 + 向量第 2
        }
        self.assertEqual([key for key, _ in got], ["b", "a", "c"])
        for key, score in got:
            self.assertAlmostEqual(score, expected[key])

    def test_empty_inputs(self) -> None:
        self.assertEqual(rrf_fuse([]), [])
        self.assertEqual(rrf_fuse([[], []]), [])

    def test_rejects_non_positive_k(self) -> None:
        with self.assertRaises(ValueError):
            rrf_fuse([["a"]], k=0)

    def test_ties_are_stable(self) -> None:
        # 同分时按首次出现顺序，结果必须可复现（不能依赖 dict 内部顺序）
        first = rrf_fuse([["a", "b"]], k=60)
        second = rrf_fuse([["a", "b"]], k=60)
        self.assertEqual(first, second)
        self.assertEqual([k for k, _ in first], ["a", "b"])

    def test_duplicate_within_one_list_counts_once(self) -> None:
        # 同一路里重复出现不该被重复计分（否则同一条目会凭空翻倍）
        only_dup = dict(rrf_fuse([["a", "a"]], k=60))
        self.assertAlmostEqual(only_dup["a"], 1 / 61)  # 只按最好名次计一次


class ModeSwitchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rfvec-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_bm25_mode_returns_plain_bm25(self) -> None:
        settings = Settings(database_url="sqlite://", retrieval_mode="bm25")
        got = build_retriever("bm25", docs_dir=self.tmp, settings=settings)
        self.assertIsInstance(got, LocalCorpusRetriever)

    def test_hybrid_mode_returns_hybrid(self) -> None:
        settings = Settings(
            database_url="sqlite://",
            retrieval_mode="hybrid",
            embedding_api_key="k",
            vector_index_dir=str(self.tmp / "idx"),
        )
        got = build_retriever("hybrid", docs_dir=self.tmp, settings=settings)
        self.assertIsInstance(got, HybridRetriever)
        # 没配 docs 时是空库，但类型与模式必须还是 hybrid（不降级）
        self.assertEqual(got.effective_mode, "hybrid")

    def test_vector_mode_returns_hybrid(self) -> None:
        settings = Settings(
            database_url="sqlite://",
            retrieval_mode="vector",
            embedding_api_key="k",
            vector_index_dir=str(self.tmp / "idx"),
        )
        got = build_retriever("vector", docs_dir=self.tmp, settings=settings)
        self.assertIsInstance(got, HybridRetriever)
        self.assertEqual(got.effective_mode, "vector")

    def test_mode_comes_from_settings_by_default(self) -> None:
        settings = Settings(
            database_url="sqlite://", retrieval_mode="vector", embedding_api_key="k"
        )
        got = build_retriever(None, docs_dir=self.tmp, settings=settings)
        self.assertIsInstance(got, HybridRetriever)
        self.assertEqual(got.mode, "vector")

    def test_unknown_mode_rejected(self) -> None:
        settings = Settings(database_url="sqlite://")
        with self.assertRaises(ValueError) as ctx:
            build_retriever("bm25+", docs_dir=self.tmp, settings=settings)
        self.assertIn("bm25+", str(ctx.exception))

    def test_default_mode_is_bm25_for_backward_compat(self) -> None:
        # 历史 40 主题基线跑在 BM25 上，默认档必须是它
        self.assertEqual(Settings(database_url="sqlite://").retrieval_mode, "bm25")

    def test_all_modes_satisfy_retriever_protocol(self) -> None:
        settings = Settings(
            database_url="sqlite://", embedding_api_key="k", vector_index_dir=str(self.tmp / "i")
        )
        for mode in ("bm25", "vector", "hybrid"):
            got = build_retriever(mode, docs_dir=self.tmp, settings=settings)
            self.assertIsInstance(got, Retriever, f"{mode} 未实现 Retriever 协议")
            # reload 不在协议里但 api 层直接调用，必须存在且返回 int
            self.assertIsInstance(got.reload(), int)


class NoKeyDegradeTest(unittest.TestCase):
    """没配 EMBEDDING_API_KEY 时：不抛异常、不联网、行为与 BM25 一致。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rfvec-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.docs_dir = self.tmp / "docs"
        self.docs_dir.mkdir()
        for doc in DOCS:
            (self.docs_dir / f"{doc.doc_id}.md").write_text(
                f"# {doc.title}\n\n{doc.text}", encoding="utf-8"
            )

    def test_hybrid_without_key_degrades_to_bm25(self) -> None:
        settings = Settings(database_url="sqlite://", embedding_api_key="")
        got = build_retriever("hybrid", docs_dir=self.docs_dir, settings=settings)
        self.assertIsInstance(got, HybridRetriever)
        self.assertEqual(got.effective_mode, "bm25")
        self.assertIn("EMBEDDING_API_KEY", got.degraded_reason)

    def test_degraded_results_equal_bm25_exactly(self) -> None:
        settings = Settings(database_url="sqlite://", embedding_api_key="")
        hybrid = build_retriever("hybrid", docs_dir=self.docs_dir, settings=settings)
        plain = build_retriever("bm25", docs_dir=self.docs_dir, settings=settings)

        query = "储能装机规模"
        got = _run(hybrid.search(query, top_k=3))
        want = _run(plain.search(query, top_k=3))
        self.assertEqual([s.text for s in got], [s.text for s in want])
        self.assertEqual([s.score for s in got], [s.score for s in want])

    def test_vector_without_key_also_degrades(self) -> None:
        settings = Settings(database_url="sqlite://", embedding_api_key="")
        got = build_retriever("vector", docs_dir=self.docs_dir, settings=settings)
        self.assertEqual(got.effective_mode, "bm25")
        self.assertTrue(_run(got.search("成本", top_k=2)))

    def test_no_network_attempted(self) -> None:
        """降级路径连 embedder 都不该建出来——建了就意味着可能在别处偷偷发请求。"""
        settings = Settings(database_url="sqlite://", embedding_api_key="")
        got = build_retriever("hybrid", docs_dir=self.docs_dir, settings=settings)
        self.assertIsNone(got._embedder)  # noqa: SLF001
        self.assertIsNone(got._index)  # noqa: SLF001


class EmptyCorpusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rfvec-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_all_modes_return_empty_on_empty_corpus(self) -> None:
        empty = build_hybrid_from_documents(
            [], mode="hybrid", embedder=_embedder(_FakeEmbeddingTransport(), cache_dir=self.tmp)
        )
        for mode in ("bm25", "vector", "hybrid"):
            retriever = build_hybrid_from_documents(
                [],
                mode=mode,  # type: ignore[arg-type]
                embedder=_embedder(_FakeEmbeddingTransport(), cache_dir=self.tmp),
            )
            self.assertEqual(retriever.corpus_size(), 0)
            self.assertEqual(_run(retriever.search("任意查询", top_k=4)), [])
        self.assertEqual(empty.corpus_size(), 0)

    def test_empty_corpus_never_calls_embedding(self) -> None:
        transport = _FakeEmbeddingTransport()
        retriever = build_hybrid_from_documents(
            [], mode="hybrid", embedder=_embedder(transport, cache_dir=self.tmp)
        )
        _run(retriever.search("x"))
        self.assertEqual(transport.calls, 0)

    def test_empty_corpus_full_text_and_ids(self) -> None:
        retriever = build_hybrid_from_documents([], mode="hybrid")
        self.assertEqual(retriever.doc_ids(), [])
        self.assertEqual(retriever.full_text("nope"), "")
        self.assertEqual(retriever.reload(), 0)


class TraceabilityTest(unittest.TestCase):
    """核心不变量：返回的 text 必须是某个原文 chunk 的逐字切片。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rfvec-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _retriever(self, transport=None) -> HybridRetriever:
        return build_hybrid_from_documents(
            DOCS,
            mode="hybrid",
            embedder=_embedder(transport or _ClusteredTransport(), cache_dir=self.tmp),
            index_dir=self.tmp / "idx",
        )

    def test_every_snippet_text_is_verbatim_chunk(self) -> None:
        retriever = self._retriever()
        for query in ("储能政策", "装机规模", "成本与收益率", "碳酸锂"):
            for snippet in _run(retriever.search(query, top_k=4)):
                self.assertIn(
                    snippet.text,
                    retriever.full_text(snippet.doc_id),
                    f"query={query} 的片段不在原文中（被改写过？）: {snippet.text!r}",
                )

    def test_snippet_matches_a_real_chunk_exactly(self) -> None:
        """不只是"子串"，必须是某个 chunk 的完整文本——防止把两段拼起来。"""
        retriever = self._retriever()
        chunk_texts = {c.text for c in LocalCorpusRetriever(DOCS).chunks()}
        for snippet in _run(retriever.search("储能", top_k=5)):
            self.assertIn(snippet.text, chunk_texts)

    def test_hybrid_returns_source_chunks_not_fused_text(self) -> None:
        """融合只影响排序，不影响文本：不得出现"两段拼接"的产物。"""
        retriever = self._retriever()
        results = _run(retriever.search("政策与成本", top_k=5))
        self.assertTrue(results)
        chunk_texts = {c.text for c in LocalCorpusRetriever(DOCS).chunks()}
        for snippet in results:
            self.assertNotIn("\x00", snippet.text)
            self.assertIn(snippet.text, chunk_texts)

    def test_score_is_rrf_score_not_cosine(self) -> None:
        retriever = self._retriever()
        results = _run(retriever.search("政策", top_k=3))
        self.assertTrue(results)
        # RRF 分数量级：1/(60+1) ≈ 0.0164，不会超过 1
        for snippet in results:
            self.assertLessEqual(snippet.score, 1.0)
            self.assertGreater(snippet.score, 0.0)

    def test_vector_only_also_returns_verbatim(self) -> None:
        retriever = build_hybrid_from_documents(
            DOCS,
            mode="vector",
            embedder=_embedder(_ClusteredTransport(), cache_dir=self.tmp),
        )
        chunk_texts = {c.text for c in LocalCorpusRetriever(DOCS).chunks()}
        for snippet in _run(retriever.search("政策", top_k=3)):
            self.assertIn(snippet.text, chunk_texts)


class HybridBehaviourTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rfvec-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_hybrid_pulls_in_vector_only_hits(self) -> None:
        """构造一个 BM25 关键词匹配不上的查询，向量路应当把相关片段捞回来。

        "装机规模" 这类词在语料里是逐字出现的，BM25 能中；这里换成改写过的
        同义表达，验证向量路真的参与了融合，而不是只有 BM25 在干活。
        """
        docs = [
            Document(doc_id="a", title="A", text="国家层面明确新型储能的发展目标与路线图安排。"),
            Document(doc_id="b", title="B", text="碳酸锂价格回落带动电芯成本下降。"),
        ]

        class _Fixed:
            """把查询映射到"和 a 很像、和 b 不像"的向量。"""

            async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
                out = []
                for text in texts:
                    if "目标" in text or "路线图" in text or "规划" in text:
                        out.append([1.0, 0.0])
                    elif "成本" in text or "锂" in text:
                        out.append([0.0, 1.0])
                    else:
                        out.append([0.9, 0.1])
                return out

            async def aclose(self) -> None:
                pass

        retriever = build_hybrid_from_documents(
            docs, mode="hybrid", embedder=_embedder(_Fixed(), cache_dir=self.tmp)
        )
        # 这个词 BM25 一条都中不了（语料里没有"规划"二字）
        results = _run(retriever.search("整体规划", top_k=2))
        self.assertTrue(results)
        self.assertEqual(results[0].doc_id, "a")
        self.assertIn(results[0].text, {d.text for d in docs})

    def test_top_k_respected(self) -> None:
        retriever = build_hybrid_from_documents(
            DOCS, mode="hybrid", embedder=_embedder(_ClusteredTransport(), cache_dir=self.tmp)
        )
        for k in (1, 2, 5):
            self.assertEqual(len(_run(retriever.search("储能", top_k=k))), k)

    def test_top_k_zero_returns_empty(self) -> None:
        retriever = build_hybrid_from_documents(
            DOCS, mode="hybrid", embedder=_embedder(_ClusteredTransport(), cache_dir=self.tmp)
        )
        self.assertEqual(_run(retriever.search("储能", top_k=0)), [])

    def test_chapters_hint_participates(self) -> None:
        transport = _ClusteredTransport()
        retriever = build_hybrid_from_documents(
            DOCS, mode="hybrid", embedder=_embedder(transport, cache_dir=self.tmp)
        )
        plain = _run(retriever.search("储能", top_k=2))
        hinted = _run(retriever.search("储能", top_k=2, chapters_hint="成本 价格 经济性"))
        self.assertTrue(plain)
        self.assertTrue(hinted)  # hint 至少不能把检索搞崩

    def test_index_built_once_for_many_searches(self) -> None:
        """并发五章 research 时不能重复建索引（重复 = 重复计费）。"""
        transport = _FakeEmbeddingTransport()
        retriever = build_hybrid_from_documents(
            DOCS, mode="hybrid", embedder=_embedder(transport, cache_dir=self.tmp)
        )

        async def many() -> None:
            await asyncio.gather(*(retriever.search("储能", top_k=2) for _ in range(5)))

        _run(many())
        # 3 个 chunk + 5 次查询（查询文本去重后是 1 条）；关键是不该出现
        # 5 份完整索引重建 = 15 条
        self.assertLess(transport.calls, 10)

    def test_full_text_and_doc_ids_delegate(self) -> None:
        retriever = build_hybrid_from_documents(DOCS, mode="hybrid")
        self.assertEqual(retriever.doc_ids(), ["cost", "policy", "scale"])
        self.assertIn("电芯成本", retriever.full_text("cost"))
        self.assertEqual(retriever.corpus_size(), len(LocalCorpusRetriever(DOCS).chunks()))

    def test_reload_after_upload_finds_new_doc(self) -> None:
        """api 层上传文件后直接调 reload()：新文档必须立刻可检索。"""
        docs_dir = self.tmp / "docs"
        docs_dir.mkdir()
        (docs_dir / "old.md").write_text("# 旧文\n\n旧的储能政策说明段落内容。", encoding="utf-8")

        retriever = build_retriever(
            "hybrid",
            docs_dir=docs_dir,
            settings=Settings(database_url="sqlite://", embedding_api_key=""),
        )
        before = retriever.corpus_size()
        self.assertTrue(before > 0)
        self.assertTrue(_run(retriever.search("储能政策", top_k=2)))

        (docs_dir / "new.md").write_text(
            "# 新文\n\n新上传的液流电池技术路线说明，含钒电解液成本分析。", encoding="utf-8"
        )
        chunks = retriever.reload()  # 同步方法，api 层就是这么调的
        self.assertIsInstance(chunks, int)
        self.assertGreater(chunks, before)

        hits = _run(retriever.search("液流电池", top_k=2))
        self.assertTrue(hits)
        self.assertEqual(hits[0].doc_id, "new")
        self.assertIn(hits[0].text, retriever.full_text("new"))

    def test_reload_is_sync_inside_running_loop(self) -> None:
        """reload 必须能在事件循环里同步调用（api 的 async 处理器就这么用）。"""
        retriever = build_hybrid_from_documents(DOCS, mode="hybrid")

        async def handler() -> int:
            return retriever.reload()  # 同步调用；内部若 asyncio.run 会在这里炸

        self.assertIsInstance(_run(handler()), int)

    def test_reload_invalidates_vector_index(self) -> None:
        """语料变了向量索引就必须失效重建，否则会拿旧向量配新片段。"""
        transport = _FakeEmbeddingTransport()
        docs_dir = self.tmp / "docs"
        docs_dir.mkdir()
        (docs_dir / "a.md").write_text("# A\n\n储能政策的第一段说明文字。", encoding="utf-8")
        embedder = _embedder(transport, cache_dir=self.tmp)
        bm25 = LocalCorpusRetriever.from_directory(docs_dir)
        retriever = HybridRetriever(bm25, mode="hybrid", embedder=embedder)

        _run(retriever.search("储能", top_k=2))
        self.assertTrue(retriever._index_ready)  # noqa: SLF001

        (docs_dir / "b.md").write_text("# B\n\n全新加入的成本分析段落内容。", encoding="utf-8")
        retriever.reload()
        self.assertFalse(retriever._index_ready)  # noqa: SLF001

        _run(retriever.search("成本", top_k=3))
        self.assertTrue(retriever._index_ready)  # noqa: SLF001

    def test_persisted_index_is_reused_without_embedding(self) -> None:
        """落盘索引在指纹一致时应直接复用：重复启动服务不该重复计费。"""
        idx_dir = self.tmp / "idx"
        first = _FakeEmbeddingTransport()
        r1 = build_hybrid_from_documents(
            DOCS,
            mode="hybrid",
            embedder=_embedder(first, cache_dir=self.tmp),
            index_dir=idx_dir,
        )
        _run(r1.search("储能", top_k=2))
        self.assertGreater(first.calls, 0)

        second = _FakeEmbeddingTransport()
        r2 = build_hybrid_from_documents(
            DOCS,
            mode="hybrid",
            embedder=_embedder(second, cache_dir=self.tmp),
            index_dir=idx_dir,
        )
        _run(r2.search("储能", top_k=2))
        # 只有查询那一条需要向量，索引本身从磁盘恢复
        self.assertEqual(second.calls, 1)

    def test_degraded_when_index_build_fails(self) -> None:
        """建索引失败（网络抖动）时本次降级为 BM25，而不是让整份报告失败。"""

        class _Boom:
            async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
                raise EmbeddingTransient("测试：连接失败")

            async def aclose(self) -> None:
                pass

        retriever = build_hybrid_from_documents(
            DOCS, mode="hybrid", embedder=_embedder(_Boom(), cache_dir=self.tmp)
        )
        hits = _run(retriever.search("储能", top_k=2))
        self.assertTrue(hits)  # 降级后仍有结果
        chunk_texts = {c.text for c in LocalCorpusRetriever(DOCS).chunks()}
        for snippet in hits:
            self.assertIn(snippet.text, chunk_texts)


class RrfScoreScaleContractTest(unittest.TestCase):
    """RRF 分数量级（~0.016）与下游"按分数打折"机制的兼容性。

    这不是 hybrid 的 bug，而是一个**跨层契约**：
    `budget.py` 的 REUSE_DISCOUNT 与 `researcher._rerank_by_budget` 的
    `_BUDGET_DISCOUNT` 都是把分数乘 0.5。在 BM25 的绝对分数量级下（6.4 vs 5.2，
    相对差 ~20%），乘 0.5 是"降权但仍可能翻盘"；而 RRF 相邻名次只差 ~1.6%，
    乘 0.5 等于把名次直接推到 k 名开外——实际效果是**硬过滤**。

    这里把该性质钉成测试，让它在被切换模式时显式暴露，
    而不是等评测发现"章节重复率没改善 / 覆盖率反而下降"再回头查。
    """

    def test_rrf_neighbour_gap_is_far_smaller_than_bm25(self) -> None:
        k = 60
        rrf_gap = abs(1 / (k + 1) - 1 / (k + 2)) / (1 / (k + 1))
        bm25_gap = abs(6.4292 - 5.1968) / 6.4292  # 真实语料实测相邻两名
        self.assertLess(rrf_gap, 0.05)
        self.assertGreater(bm25_gap, 0.15)
        # 量级差异至少一个数量级：说明"乘 0.5"在两种口径下语义完全不同
        self.assertGreater(bm25_gap / rrf_gap, 10)

    def test_halving_rrf_score_effectively_excludes(self) -> None:
        """RRF 下 0.5 折扣会让预算内文档无条件压过预算外文档。"""
        from app.agents.researcher import _rerank_by_budget

        best_outside = Snippet(doc_id="outside", title="t", page=None, text="x", score=1 / 61)
        worst_budgeted = Snippet(doc_id="budgeted", title="t", page=None, text="x", score=1 / 80)
        order = _rerank_by_budget([best_outside, worst_budgeted], {"budgeted"}, limit=2)
        # 记录当前行为：预算内的永远在前 —— 等价于硬过滤，
        # 与 budget.py 注释里"不做硬过滤"的设计意图相反。切换 hybrid 前必须先解决。
        self.assertEqual(order[0].doc_id, "budgeted")

    def test_halving_bm25_score_keeps_relative_order_meaningful(self) -> None:
        """对照：BM25 量级下同样的 0.5 折扣不会让低分文档无条件胜出。"""
        from app.agents.researcher import _rerank_by_budget

        better = Snippet(doc_id="outside", title="t", page=None, text="x", score=6.4)
        worse = Snippet(doc_id="budgeted", title="t", page=None, text="x", score=2.0)
        order = _rerank_by_budget([better, worse], {"budgeted"}, limit=2)
        self.assertEqual(order[0].doc_id, "outside")  # 打折后 3.2 < 6.4，仍不翻转

    def test_documents_the_migration_requirement(self) -> None:
        """切换到 hybrid 前，下游两处折扣语义必须重新标定（见交接说明）。"""
        self.assertTrue(hasattr(__import__("app.rag.budget", fromlist=["x"]), "REUSE_DISCOUNT"))


class EmbeddingCacheTest(unittest.TestCase):
    """embedding 的录制回放：与 llm/cache.py 语义等价，但键含模型名。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rfemb-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_key_includes_model_and_text(self) -> None:
        self.assertEqual(embed_key("m1", "文本"), embed_key("m1", "文本"))
        self.assertNotEqual(embed_key("m1", "文本"), embed_key("m2", "文本"))
        self.assertNotEqual(embed_key("m1", "文本"), embed_key("m1", "文本2"))

    def test_record_then_replay_returns_same_vector(self) -> None:
        inner = _FakeEmbeddingTransport()
        rec = _embedder(inner, cache_dir=self.tmp, mode=EmbeddingCacheMode.RECORD)
        first = _run(rec.embed_texts(["甲", "乙"]))

        replay_inner = _FakeEmbeddingTransport()
        rep = _embedder(replay_inner, cache_dir=self.tmp, mode=EmbeddingCacheMode.REPLAY)
        second = _run(rep.embed_texts(["甲", "乙"]))

        self.assertEqual(first, second)
        self.assertEqual(replay_inner.calls, 0)  # 回放绝不联网
        self.assertEqual(rep.hits, 2)
        self.assertEqual(rep.misses, 0)

    def test_replay_miss_raises_and_never_networks(self) -> None:
        inner = _FakeEmbeddingTransport()
        rep = _embedder(inner, cache_dir=self.tmp, mode=EmbeddingCacheMode.REPLAY)
        with self.assertRaises(EmbeddingReplayMiss) as ctx:
            _run(rep.embed_texts(["从未录制过的文本"]))
        self.assertIn("record", str(ctx.exception))
        self.assertEqual(inner.calls, 0)
        self.assertEqual(rep.misses, 1)

    def test_replay_miss_has_no_retry_marker(self) -> None:
        # 调度层用 getattr 检查 no_retry，与 llm/cache.ReplayMiss 保持同一约定
        self.assertTrue(getattr(EmbeddingReplayMiss, "no_retry", False))

    def test_auto_records_then_serves_from_cache(self) -> None:
        inner = _FakeEmbeddingTransport()
        auto = _embedder(inner, cache_dir=self.tmp, mode=EmbeddingCacheMode.AUTO)
        _run(auto.embed_texts(["丙"]))
        _run(auto.embed_texts(["丙"]))
        self.assertEqual(inner.calls, 1)
        self.assertEqual(auto.recorded, 1)
        self.assertEqual(auto.hits, 1)

    def test_live_never_touches_disk(self) -> None:
        inner = _FakeEmbeddingTransport()
        live = _embedder(inner, cache_dir=self.tmp, mode=EmbeddingCacheMode.LIVE)
        _run(live.embed_texts(["丁"]))
        _run(live.embed_texts(["丁"]))
        self.assertEqual(inner.calls, 2)  # 每次都真打
        self.assertEqual(list((self.tmp / "embeddings").rglob("*.json")), [])

    def test_record_does_not_overwrite_existing(self) -> None:
        first = _embedder(
            _FakeEmbeddingTransport(), cache_dir=self.tmp, mode=EmbeddingCacheMode.RECORD
        )
        _run(first.embed_texts(["戊"]))

        class _Different:
            async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
                return [[9.0] * DIM for _ in texts]

            async def aclose(self) -> None:
                pass

        second = _embedder(_Different(), cache_dir=self.tmp, mode=EmbeddingCacheMode.RECORD)
        _run(second.embed_texts(["戊"]))

        rep = _embedder(
            _FakeEmbeddingTransport(), cache_dir=self.tmp, mode=EmbeddingCacheMode.REPLAY
        )
        self.assertEqual(_run(rep.embed_texts(["戊"]))[0], _vec_of("戊"))

    def test_cache_lives_under_embeddings_subdir(self) -> None:
        # 与 LLM 的 prompt 缓存分开，否则 22 篇语料的向量会和 prompt 混在一个目录里
        emb = _embedder(_FakeEmbeddingTransport(), cache_dir=self.tmp, mode=EmbeddingCacheMode.AUTO)
        _run(emb.embed_texts(["己"]))
        self.assertEqual(emb.dir, self.tmp / "embeddings")
        self.assertEqual(len(list((self.tmp / "embeddings").rglob("*.json"))), 1)

    def test_corrupted_cache_is_a_miss(self) -> None:
        key = embed_key("fake-embed", "庚")
        path = self.tmp / "embeddings" / key[:2] / f"{key}.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ 半个 JSON", encoding="utf-8")

        with self.assertRaises(EmbeddingReplayMiss):
            _run(
                _embedder(
                    _FakeEmbeddingTransport(),
                    cache_dir=self.tmp,
                    mode=EmbeddingCacheMode.REPLAY,
                ).embed_texts(["庚"])
            )

        inner = _FakeEmbeddingTransport()
        auto = _embedder(inner, cache_dir=self.tmp, mode=EmbeddingCacheMode.AUTO)
        self.assertEqual(_run(auto.embed_texts(["庚"]))[0], _vec_of("庚"))
        self.assertEqual(inner.calls, 1)

    def test_model_change_invalidates_cache(self) -> None:
        _run(
            _embedder(
                _FakeEmbeddingTransport(), cache_dir=self.tmp, mode=EmbeddingCacheMode.RECORD
            ).embed_texts(["辛"])
        )
        other = _embedder(
            _FakeEmbeddingTransport(),
            cache_dir=self.tmp,
            mode=EmbeddingCacheMode.REPLAY,
            model="another-model",
        )
        with self.assertRaises(EmbeddingReplayMiss):
            _run(other.embed_texts(["辛"]))

    def test_batching_splits_requests(self) -> None:
        transport = _FakeEmbeddingTransport()
        emb = _embedder(transport, cache_dir=self.tmp, mode=EmbeddingCacheMode.LIVE, batch_size=2)
        _run(emb.embed_texts(["a", "b", "c", "d", "e"]))
        self.assertEqual(transport.calls, 3)  # 2 + 2 + 1
        self.assertEqual([len(b) for b in transport.batches], [2, 2, 1])

    def test_duplicate_texts_embedded_once(self) -> None:
        transport = _FakeEmbeddingTransport()
        emb = _embedder(transport, cache_dir=self.tmp, mode=EmbeddingCacheMode.LIVE)
        got = _run(emb.embed_texts(["同样", "同样", "不同"]))
        self.assertEqual(len(got), 3)
        self.assertEqual(got[0], got[1])
        self.assertEqual(transport.batches[0].count("同样"), 1)

    def test_transient_error_is_retried(self) -> None:
        transport = _FakeEmbeddingTransport(fails=2)
        emb = _embedder(transport, cache_dir=self.tmp, mode=EmbeddingCacheMode.LIVE)
        got = _run(emb.embed_texts(["重试"]))
        self.assertEqual(len(got), 1)
        self.assertEqual(transport.calls, 3)  # 失败 2 次 + 成功 1 次

    def test_retry_gives_up_eventually(self) -> None:
        transport = _FakeEmbeddingTransport(fails=99)
        emb = _embedder(transport, cache_dir=self.tmp, mode=EmbeddingCacheMode.LIVE)
        with self.assertRaises(EmbeddingTransient):
            _run(emb.embed_texts(["永远失败"]))
        self.assertEqual(transport.calls, 3)  # 首次 + 2 次重试

    def test_no_tmp_files_left(self) -> None:
        emb = _embedder(
            _FakeEmbeddingTransport(), cache_dir=self.tmp, mode=EmbeddingCacheMode.RECORD
        )
        _run(emb.embed_texts(["原子写"]))
        self.assertEqual(list((self.tmp / "embeddings").rglob("*.tmp")), [])

    def test_replay_mode_in_hybrid_search(self) -> None:
        """混合检索走 replay：缓存齐了就完全离线，缺了就抛（不静默降级）。"""
        idx = self.tmp / "idx"
        rec = build_hybrid_from_documents(
            DOCS,
            mode="hybrid",
            embedder=_embedder(
                _FakeEmbeddingTransport(), cache_dir=self.tmp, mode=EmbeddingCacheMode.RECORD
            ),
            index_dir=idx,
        )
        expected = _run(rec.search("储能政策", top_k=3))
        self.assertTrue(expected)

        replay = build_hybrid_from_documents(
            DOCS,
            mode="hybrid",
            embedder=_embedder(
                _FakeEmbeddingTransport(), cache_dir=self.tmp, mode=EmbeddingCacheMode.REPLAY
            ),
            index_dir=idx,
        )
        got = _run(replay.search("储能政策", top_k=3))
        self.assertEqual([s.text for s in got], [s.text for s in expected])

    def test_replay_miss_in_hybrid_search_raises(self) -> None:
        replay = build_hybrid_from_documents(
            DOCS,
            mode="hybrid",
            embedder=_embedder(
                _FakeEmbeddingTransport(), cache_dir=self.tmp, mode=EmbeddingCacheMode.REPLAY
            ),
        )
        with self.assertRaises(EmbeddingReplayMiss):
            _run(replay.search("任何查询", top_k=2))


class VectorIndexUnitTest(unittest.TestCase):
    """索引本身的单元测试（持久化与相似度）。完整场景见 test_vector_index.py。"""

    def test_cosine_basics(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertAlmostEqual(cosine([1.0, 0.0], [-1.0, 0.0]), -1.0)
        self.assertEqual(cosine([0.0, 0.0], [1.0, 0.0]), 0.0)  # 零向量不能除零
        self.assertEqual(cosine([1.0], [1.0, 0.0]), 0.0)  # 维度不同

    def test_chunk_key_distinguishes_documents(self) -> None:
        self.assertNotEqual(chunk_key("a", None, "x"), chunk_key("b", None, "x"))
        self.assertNotEqual(chunk_key("a", 1, "x"), chunk_key("a", 2, "x"))

    def test_indexed_chunk_json_roundtrip(self) -> None:
        chunk = IndexedChunk(doc_id="d", title="T", page=3, text="原文")
        self.assertEqual(IndexedChunk.from_json(chunk.to_json()), chunk)

    def test_index_matches_requires_same_text(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="rfidx-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        index = VectorIndex(
            _embedder(_FakeEmbeddingTransport(), cache_dir=tmp), index_dir=tmp / "i"
        )
        chunks = [IndexedChunk("d", "T", None, "第一段"), IndexedChunk("d", "T", None, "第二段")]
        _run(index.build(chunks))
        self.assertTrue(index.matches("fake-embed", chunks))
        changed = [chunks[0], IndexedChunk("d", "T", None, "改过的第二段")]
        self.assertFalse(index.matches("fake-embed", changed))
        self.assertFalse(index.matches("another-model", chunks))


if __name__ == "__main__":
    unittest.main()
