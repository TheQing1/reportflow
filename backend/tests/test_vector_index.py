"""向量索引的持久化与查询测试。

单独成文件是因为这层的失败模式与检索逻辑无关：
- 落盘再读回来向量必须**逐位相同**（差 1e-8 就能让排序翻转）；
- 指纹不一致必须判废，否则会拿旧向量配新语料，静默给出错误结果；
- 索引文件损坏/版本不符必须能自愈（重建），而不是让服务起不来。

全部用假 transport，绝不联网。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from app.rag.embedding import Embedder, EmbeddingCacheMode, EmbeddingTransient
from app.rag.vector_index import (
    INDEX_FILE,
    INDEX_VERSION,
    IndexedChunk,
    VectorIndex,
    corpus_fingerprint,
    cosine,
)


def _vec(text: str, dim: int = 6) -> list[float]:
    """确定性向量：只由文本内容决定，便于断言排序。"""
    total = sum(ord(ch) for ch in text)
    return [
        ((total * (i + 1)) % 97) / 97.0 - 0.5
        for i in range(dim)
    ]


class _FakeTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.texts: list[str] = []
        self.fail = fail

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise EmbeddingTransient("测试：索引构建期间网络抖动")
        self.texts.extend(texts)
        return [_vec(t) for t in texts]

    async def aclose(self) -> None:
        pass


CHUNKS = [
    IndexedChunk("d1", "文档一", None, "储能政策的第一段原文内容。"),
    IndexedChunk("d1", "文档一", None, "储能政策的第二段原文内容。"),
    IndexedChunk("d2", "文档二", 3, "成本与收益率分析段落。"),
]


def _run(coro):
    return asyncio.run(coro)


class VectorIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rfvidx-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _index(self, *, index_dir: Path | None = None, transport=None) -> VectorIndex:
        embedder = Embedder(
            transport or _FakeTransport(),
            mode=EmbeddingCacheMode.LIVE,
            cache_dir=self.tmp / "cache",
            model="fake-embed",
        )
        return VectorIndex(embedder, index_dir=index_dir or (self.tmp / "idx"))

    # -------------------------------------------------------- 构建

    def test_build_embeds_each_chunk_once(self) -> None:
        transport = _FakeTransport()
        index = self._index(transport=transport)
        self.assertEqual(_run(index.build(CHUNKS)), 3)
        self.assertEqual(index.size(), 3)
        self.assertEqual(len(transport.texts), 3)

    def test_build_same_corpus_twice_skips_embedding(self) -> None:
        # 指纹一致就不该再花一次 embedding 的钱
        transport = _FakeTransport()
        index = self._index(transport=transport)
        _run(index.build(CHUNKS))
        calls_after_first = transport.calls
        _run(index.build(CHUNKS))
        self.assertEqual(transport.calls, calls_after_first)

    def test_build_force_reembeds(self) -> None:
        transport = _FakeTransport()
        index = self._index(transport=transport)
        _run(index.build(CHUNKS))
        before = transport.calls
        _run(index.build(CHUNKS, force=True))
        self.assertGreater(transport.calls, before)

    def test_build_empty_clears(self) -> None:
        index = self._index()
        _run(index.build(CHUNKS))
        self.assertEqual(_run(index.build([])), 0)
        self.assertEqual(index.size(), 0)

    # -------------------------------------------------------- 查询

    def test_search_returns_verbatim_text_with_page(self) -> None:
        index = self._index()
        _run(index.build(CHUNKS))
        rows = index.search(_vec(CHUNKS[2].text), top_k=3)
        self.assertEqual(len(rows), 3)
        doc_id, page, text, score = rows[0]
        self.assertEqual(doc_id, "d2")
        self.assertEqual(page, 3)  # page 必须原样带回来，它是引用角标的一部分
        self.assertEqual(text, CHUNKS[2].text)  # 逐字，不是重建的
        self.assertAlmostEqual(score, 1.0, places=6)  # 自己查自己是 1

    def test_search_scores_are_descending(self) -> None:
        index = self._index()
        _run(index.build(CHUNKS))
        scores = [score for *_rest, score in index.search(_vec(CHUNKS[0].text), top_k=3)]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_search_respects_top_k(self) -> None:
        index = self._index()
        _run(index.build(CHUNKS))
        self.assertEqual(len(index.search(_vec("任意"), top_k=1)), 1)
        self.assertEqual(len(index.search(_vec("任意"), top_k=2)), 2)

    def test_search_on_empty_index_is_empty(self) -> None:
        index = self._index()
        self.assertEqual(index.search(_vec("任意"), top_k=3), [])

    def test_search_rejects_dimension_mismatch(self) -> None:
        # 混用两个模型的向量只会得到无意义相似度，必须显式报错
        index = self._index()
        _run(index.build(CHUNKS))
        with self.assertRaises(ValueError) as ctx:
            index.search([0.1, 0.2], top_k=1)
        self.assertIn("维度", str(ctx.exception))

    def test_search_zero_vector_scores_zero(self) -> None:
        index = self._index()
        _run(index.build(CHUNKS))
        rows = index.search([0.0] * 6, top_k=3)
        self.assertEqual(len(rows), 3)
        for *_, score in rows:
            self.assertEqual(score, 0.0)

    # -------------------------------------------------------- 持久化

    def test_save_and_load_roundtrip(self) -> None:
        idx_dir = self.tmp / "idx"
        index = self._index(index_dir=idx_dir)
        _run(index.build(CHUNKS))
        self.assertTrue((idx_dir / INDEX_FILE).is_file())

        restored = self._index(index_dir=idx_dir)
        self.assertTrue(restored.load())
        self.assertEqual(restored.size(), 3)
        self.assertEqual(restored.chunks(), CHUNKS)
        # 落盘再读回来的排序必须与原索引完全一致（含分数）
        before = index.search(_vec(CHUNKS[1].text), top_k=3)
        after = restored.search(_vec(CHUNKS[1].text), top_k=3)
        self.assertEqual([r[:3] for r in before], [r[:3] for r in after])
        for a, b in zip(before, after, strict=True):
            self.assertAlmostEqual(a[3], b[3], places=12)

    def test_load_without_file_returns_false(self) -> None:
        self.assertFalse(self._index(index_dir=self.tmp / "nope").load())

    def test_load_rejects_version_mismatch(self) -> None:
        idx_dir = self.tmp / "idx"
        index = self._index(index_dir=idx_dir)
        _run(index.build(CHUNKS))
        path = idx_dir / INDEX_FILE
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["v"] = INDEX_VERSION - 1
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        self.assertFalse(self._index(index_dir=idx_dir).load())

    def test_load_rejects_model_change(self) -> None:
        idx_dir = self.tmp / "idx"
        index = self._index(index_dir=idx_dir)
        _run(index.build(CHUNKS))

        other = VectorIndex(
            Embedder(
                _FakeTransport(),
                mode=EmbeddingCacheMode.LIVE,
                cache_dir=self.tmp / "cache",
                model="another-model",
            ),
            index_dir=idx_dir,
        )
        self.assertFalse(other.load())

    def test_load_rejects_corrupted_file(self) -> None:
        idx_dir = self.tmp / "idx"
        idx_dir.mkdir(parents=True, exist_ok=True)
        (idx_dir / INDEX_FILE).write_text("{ 半个 JSON", encoding="utf-8")
        self.assertFalse(self._index(index_dir=idx_dir).load())

    def test_load_rejects_tampered_text(self) -> None:
        """有人手改了索引里的原文：指纹对不上，必须判废重建。

        这是"引用可溯源"的最后一道闸——如果照用，返回的就是原文里没有的句子。
        """
        idx_dir = self.tmp / "idx"
        index = self._index(index_dir=idx_dir)
        _run(index.build(CHUNKS))
        path = idx_dir / INDEX_FILE
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["chunks"][0]["text"] = "被改写的句子，原文里不存在。"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        self.assertFalse(self._index(index_dir=idx_dir).load())

    def test_load_rejects_length_mismatch(self) -> None:
        idx_dir = self.tmp / "idx"
        index = self._index(index_dir=idx_dir)
        _run(index.build(CHUNKS))
        path = idx_dir / INDEX_FILE
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["vectors"].pop()
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        self.assertFalse(self._index(index_dir=idx_dir).load())

    def test_save_without_dir_is_noop(self) -> None:
        # 纯内存模式（测试里常见）：没有目录就静默跳过，不该抛
        index = self._index()
        index.dir = None
        _run(index.build(CHUNKS))
        self.assertEqual(index.size(), 3)

    def test_no_tmp_files_left(self) -> None:
        idx_dir = self.tmp / "idx"
        _run(self._index(index_dir=idx_dir).build(CHUNKS))
        self.assertEqual(list(idx_dir.rglob("*.tmp")), [])

    def test_clear_then_disk_persists_old_state(self) -> None:
        idx_dir = self.tmp / "idx"
        index = self._index(index_dir=idx_dir)
        _run(index.build(CHUNKS))
        index.clear()
        self.assertEqual(index.size(), 0)
        # 磁盘上还是旧索引，load 仍可用（clear 只清内存，不删文件）
        self.assertTrue(self._index(index_dir=idx_dir).load())

    def test_clear_disk_removes_index(self) -> None:
        idx_dir = self.tmp / "idx"
        index = self._index(index_dir=idx_dir)
        _run(index.build(CHUNKS))
        index.clear_disk()
        self.assertFalse(idx_dir.exists())

    # -------------------------------------------------------- 构建失败

    def test_build_failure_propagates(self) -> None:
        index = self._index(transport=_FakeTransport(fail=True))
        with self.assertRaises(EmbeddingTransient):
            _run(index.build(CHUNKS))
        self.assertEqual(index.size(), 0)  # 失败不留半成品


class FingerprintTest(unittest.TestCase):
    def test_same_corpus_same_fingerprint(self) -> None:
        self.assertEqual(
            corpus_fingerprint("m", CHUNKS), corpus_fingerprint("m", list(CHUNKS))
        )

    def test_model_changes_fingerprint(self) -> None:
        self.assertNotEqual(
            corpus_fingerprint("m1", CHUNKS), corpus_fingerprint("m2", CHUNKS)
        )

    def test_text_change_changes_fingerprint(self) -> None:
        changed = [CHUNKS[0], IndexedChunk("d2", "文档二", 3, "改过的段落。")]
        self.assertNotEqual(
            corpus_fingerprint("m", CHUNKS), corpus_fingerprint("m", changed)
        )

    def test_page_change_changes_fingerprint(self) -> None:
        changed = [CHUNKS[0], CHUNKS[1], IndexedChunk("d2", "文档二", 4, CHUNKS[2].text)]
        self.assertNotEqual(
            corpus_fingerprint("m", CHUNKS), corpus_fingerprint("m", changed)
        )

    def test_no_separator_collision(self) -> None:
        """分隔符必须存在，否则相邻文本拼接后可能算出同一个指纹。"""
        a = [IndexedChunk("d", "T", None, "ab")]
        b = [IndexedChunk("d", "T", None, "a"), IndexedChunk("d", "T", None, "b")]
        self.assertNotEqual(corpus_fingerprint("m", a), corpus_fingerprint("m", b))

    def test_empty_corpus_stable(self) -> None:
        self.assertEqual(corpus_fingerprint("m", []), corpus_fingerprint("m", []))


class CosineTest(unittest.TestCase):
    def test_identical_is_one(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]), 1.0)

    def test_orthogonal_is_zero(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_opposite_is_minus_one(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 0.0], [-1.0, 0.0]), -1.0)

    def test_scale_invariant(self) -> None:
        # 余弦只看方向：整体放大不该改变相似度
        self.assertAlmostEqual(cosine([1.0, 2.0], [1.0, 2.0]), cosine([2.0, 4.0], [5.0, 10.0]))

    def test_zero_and_shape_guards(self) -> None:
        self.assertEqual(cosine([], []), 0.0)
        self.assertEqual(cosine([0.0, 0.0], [1.0, 1.0]), 0.0)
        self.assertEqual(cosine([1.0, 2.0], [1.0]), 0.0)


if __name__ == "__main__":
    unittest.main()
