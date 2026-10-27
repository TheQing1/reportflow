"""混合检索：BM25 + 向量，用 RRF 融合。

为什么是 RRF 而不是"归一化后加权求和"：

- BM25 的分数是无界的词频×IDF，余弦相似度在 [0,1]，两者量纲根本不可比。
  要加权求和就必须先归一化，而归一化的方法（min-max / z-score / 除以 top1）
  会随查询变化，等于每次查询都换一把尺子；
- RRF 只看**名次**不看分数：`score = Σ 1/(k + rank)`，rank 从 1 开始。
  它天然免疫两路分数量纲不一致，也不需要调权重——这正是它成为工业默认的原因；
- k 越大越"平权"，k=60 是原论文（Cormack et al., 2009）与工业界的默认值。

关键约束：**最终返回的 Snippet.text 必须来自原文 chunk**。
所以融合只在"片段身份"上做，返回时统一从 BM25 的 chunk 表取文本——
向量索引里存的那份文本只用于建向量，不参与输出。
即便索引文件被人手改过，返回的文本仍然是当前语料的逐字切片。

降级策略分两种，语义不同，不能混为一谈：

| 情况 | 行为 | 为什么 |
|---|---|---|
| 没配 EMBEDDING_API_KEY | 全程按 BM25 跑 | 知识库功能不该因为没配 embedding 就起不来 |
| 回放模式缓存缺失 | 抛 `EmbeddingReplayMiss` | 静默降级会让"回放出来的结果"≠"录制的混合检索结果"，基线就假了 |
| 网络抖动 / 5xx / 超时 | 本次降级为 BM25 并告警 | 检索只是素材入口，一次网络抖动不该让整份报告失败 |

`reload()` 必须**同步**：api 层在 async 上下文里直接调它（上传文档后立即重新索引），
里面 `asyncio.run` 会抛 "cannot be called from a running event loop"。
所以 reload 只重建 BM25（纯本地、毫秒级），向量索引标记为失效，
等下一次 `search()` 时再异步重建——那条路径本来就在事件循环里。
"""

from __future__ import annotations

import asyncio
from collections.abc import Hashable, Iterable, Sequence
from pathlib import Path
from typing import Literal, TypeVar

from ..contracts.retrieval import Document, Snippet
from ..core.logging import get_logger
from .corpus import LocalCorpusRetriever
from .embedding import Embedder, EmbeddingError, EmbeddingReplayMiss
from .vector_index import IndexedChunk, VectorIndex

#: 三档检索模式。默认 bm25 —— 历史 40 主题评测基线就是在这档上跑的
RetrievalMode = Literal["bm25", "vector", "hybrid"]
VALID_MODES: tuple[str, ...] = ("bm25", "vector", "hybrid")

#: RRF 默认平滑常数（原论文取值）。k=60 时第 1 名 ≈ 0.0164，第 2 名 ≈ 0.0161，
#: 名次之间的差距很小，所以"在另一路也排得靠前"比"单路第一名"更能胜出。
DEFAULT_RRF_K = 60

T = TypeVar("T", bound=Hashable)


def rrf_fuse(
    rankings: Sequence[Sequence[T]], *, k: int = DEFAULT_RRF_K
) -> list[tuple[T, float]]:
    """Reciprocal Rank Fusion。

    `rankings` 是若干条已排好序的列表（每条内部名次从 1 开始）。
    返回 (key, 融合分) 按融合分降序；同分时按**首次出现顺序**，
    即先看哪一路更早提到它——这样结果是确定性的，不会因 dict 顺序抖动。

    单独抽成纯函数是为了可测：融合的错法（rank 从 0 开始、k 用 1/k+rank、
    同一条目在多路被重复计入）都不会报错，只会让排序悄悄变差。
    """
    if k <= 0:
        raise ValueError(f"RRF 的 k 必须为正数，收到 {k}")

    scores: dict[T, float] = {}
    first_seen: dict[T, int] = {}
    for ranking in rankings:
        seen: set[T] = set()
        for rank, key in enumerate(ranking, start=1):
            # 同一路里重复出现只按最好名次计一次：上游理论上已去重，
            # 但真重复时若照单累加，一条重复项会凭空翻倍，压过本该胜出的候选。
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            if key not in first_seen:
                first_seen[key] = len(first_seen)

    return sorted(scores.items(), key=lambda pair: (-pair[1], first_seen[pair[0]]))


def chunk_key(doc_id: str, page: int | None, text: str) -> str:
    """片段身份键。用分隔符而不是简单拼串，避免不同 (doc, page, text) 拼出同一个键。"""
    return f"{doc_id}\x00{page}\x00{text}"


class HybridRetriever:
    """实现 Retriever Protocol 的混合检索器，一份代码覆盖三档模式。

    始终内置一个 `LocalCorpusRetriever`：
    - 它是全文与片段文本的唯一权威（`full_text` / `doc_ids` / 逐字片段都从它来）；
    - bm25 档直接委托给它，行为与改造前完全一致；
    - hybrid 档用它的召回做第一路。

    向量索引是**惰性**的：构造时不联网，第一次 `search()` 才建（或从磁盘恢复）。
    这样 `build_retriever()` 可以保持同步，装配层不需要改 async 签名。
    """

    def __init__(
        self,
        bm25: LocalCorpusRetriever,
        *,
        mode: RetrievalMode = "bm25",
        embedder: Embedder | None = None,
        index_dir: str | Path | None = None,
        rrf_k: int = DEFAULT_RRF_K,
        candidate_pool: int = 20,
        degraded_reason: str = "",
    ) -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"未知的检索模式 {mode!r}（可选 {' / '.join(VALID_MODES)}）")
        self._bm25 = bm25
        self.mode: RetrievalMode = mode
        self.rrf_k = max(1, int(rrf_k))
        #: 每一路召回多少条再融合。少于 top_k 时融合没意义，见 search()
        self.candidate_pool = max(1, int(candidate_pool))
        self._log = get_logger("rag.hybrid")

        #: 没配 key 时 embedder 为空 → 明确降级，且把原因记下来给 health 日志看
        self._embedder = embedder
        self._degraded = embedder is None and mode != "bm25"
        self.degraded_reason = degraded_reason or (
            "EMBEDDING_API_KEY 为空" if self._degraded else ""
        )

        self._index = VectorIndex(embedder, index_dir=index_dir) if embedder else None
        #: 索引是否已就绪。reload() 会把它打回 False，下次搜索重建
        self._index_ready = False
        #: 正在进行的重建任务，避免五章并行 research 同时触发五次建索引
        self._build_task: asyncio.Task[None] | None = None
        self._build_loop: object | None = None

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_directory(
        cls,
        path: str | Path,
        *,
        mode: RetrievalMode = "hybrid",
        embedder: Embedder | None = None,
        index_dir: str | Path | None = None,
        rrf_k: int = DEFAULT_RRF_K,
    ) -> HybridRetriever:
        bm25 = LocalCorpusRetriever.from_directory(path)
        return cls(
            bm25,
            mode=mode,
            embedder=embedder,
            index_dir=index_dir,
            rrf_k=rrf_k,
        )

    # ------------------------------------------------------------ 有效模式

    @property
    def effective_mode(self) -> RetrievalMode:
        """实际生效的模式。降级后对外就是 bm25，便于日志与自检如实报告。"""
        return "bm25" if self._degraded else self.mode

    def _uses_vector(self) -> bool:
        return self._embedder is not None and not self._degraded and self.mode != "bm25"

    # ------------------------------------------------------------ Retriever 契约

    def corpus_size(self) -> int:
        return self._bm25.corpus_size()

    def full_text(self, doc_id: str) -> str:
        return self._bm25.full_text(doc_id)

    def doc_ids(self) -> list[str]:
        return self._bm25.doc_ids()

    def reload(self) -> int:
        """重新读目录并重建关键词索引；向量索引标记失效、下次搜索时异步重建。

        必须是同步方法：api 层在 async 上下文里直接调用（上传/删除后立即重索引）。
        这里只做本地重建（毫秒级），不碰网络。
        """
        chunks = self._bm25.reload()
        self._index_ready = False
        # 上一轮的建索引任务可能还挂在一个已经关闭的事件循环上，先丢弃
        self._build_task = None
        self._build_loop = None
        if self._index is not None:
            self._index.clear()
        return chunks

    async def search(
        self, query: str, *, top_k: int = 4, chapters_hint: str = ""
    ) -> list[Snippet]:
        if top_k <= 0 or self.corpus_size() == 0:
            return []

        if not self._uses_vector():
            # bm25 档，以及无 key / 已降级的档：完全按原行为，连打分都不改
            return await self._bm25.search(query, top_k=top_k, chapters_hint=chapters_hint)

        # 纯向量档不需要关键词这一路，别白跑一遍 BM25
        if self.mode == "vector":
            return await self._vector_only(query, top_k=top_k, chapters_hint=chapters_hint)

        # 第一路：关键词。多召回一些，融合才有腾挪空间
        pool = min(max(self.candidate_pool, top_k * 3), self.corpus_size())
        bm25_hits = await self._bm25.search(query, top_k=pool, chapters_hint=chapters_hint)
        vector_hits = await self._vector_search(query, top_k=pool, chapters_hint=chapters_hint)
        if vector_hits is None:
            # 向量这一路本轮不可用（网络抖动等），本次按 BM25 结果交付
            return bm25_hits[:top_k]

        return self._fuse(bm25_hits, vector_hits, top_k=top_k) or bm25_hits[:top_k]

    # ------------------------------------------------------------ 向量路径

    async def _vector_only(
        self, query: str, *, top_k: int, chapters_hint: str
    ) -> list[Snippet]:
        hits = await self._vector_search(query, top_k=top_k, chapters_hint=chapters_hint)
        if hits is None:
            # 纯向量档拿不到向量时退回关键词：有结果总比空手强，
            # 而且返回的仍是原文片段，溯源链没断
            return await self._bm25.search(query, top_k=top_k, chapters_hint=chapters_hint)
        return [Snippet(doc_id=d, title=t, page=p, text=x, score=round(s, 4)) for d, p, x, t, s in hits]

    async def _vector_search(
        self, query: str, *, top_k: int, chapters_hint: str
    ) -> list[tuple[str, int | None, str, str, float]] | None:
        """返回 [(doc_id, page, text, title, score)]；本轮不可用时返回 None。"""
        if self._index is None or self._embedder is None:
            return None
        try:
            await self._ensure_index()
            text = f"{query} {chapters_hint}".strip() or query
            vector = (await self._embedder.embed_texts([text]))[0]
        except EmbeddingReplayMiss:
            # 回放缺失是确定性缺失，必须炸出来：静默降级会让回放结果与录制结果不一致
            raise
        except EmbeddingError as exc:
            self._log.warning("向量检索本轮不可用，本次降级为 BM25：%s", exc)
            return None

        rows = self._index.search(vector, top_k=top_k)
        # 文本权威在 BM25 的 chunk 表：只有当键真的存在于当前语料里才返回，
        # 这样即便索引文件过期/被改，输出也不会出现"原文里没有的片段"
        titles = {chunk_key(c.doc_id, c.page, c.text): c.title for c in self._bm25.chunks()}
        out: list[tuple[str, int | None, str, str, float]] = []
        for doc_id, page, chunk_text, score in rows:
            key = chunk_key(doc_id, page, chunk_text)
            title = titles.get(key)
            if title is None:
                self._log.warning("索引里的片段不在当前语料中，已丢弃 doc=%s", doc_id)
                continue
            out.append((doc_id, page, chunk_text, title, score))
        return out

    async def _ensure_index(self) -> None:
        """惰性建索引：先试磁盘（零成本），不行才调 embedding。

        并发保护用"共享同一个 Task"而不是锁：五章 research 是并行的，
        没有它就会同时发起五份一模一样的建索引请求。
        """
        if self._index is None or self._index_ready:
            return
        loop = asyncio.get_running_loop()
        task = self._build_task
        if task is not None and self._build_loop is loop and not task.done():
            await task
            return
        if task is not None and self._build_loop is not loop:
            # 上一个事件循环（测试里常见）留下的任务不能 await，直接重建
            self._build_task = None
        self._build_loop = loop
        self._build_task = loop.create_task(self._build())
        await self._build_task

    async def _build(self) -> None:
        if self._index is None or self._embedder is None:
            return
        chunks = [IndexedChunk.from_document(doc) for doc in self._bm25.chunks()]
        if not chunks:
            self._index.clear()
            self._index_ready = True
            return
        # 磁盘上的索引若恰好对应这批片段 + 这个模型，直接用：省掉一次全量 embedding
        if self._index.load() and self._index.matches(self._embedder.model, chunks):
            self._index_ready = True
            return
        try:
            await self._index.build(chunks, force=True)
        except EmbeddingError as exc:
            # 建不起来就让下次搜索再试（并保持 not ready），本次由调用方降级
            self._log.warning("向量索引构建失败，本次检索降级为 BM25：%s", exc)
            raise
        self._index_ready = True

    # ------------------------------------------------------------ 融合

    def _fuse(
        self,
        bm25_hits: Sequence[Snippet],
        vector_hits: Sequence[tuple[str, int | None, str, str, float]],
        *,
        top_k: int,
    ) -> list[Snippet]:
        """RRF 融合两路结果，输出统一从 BM25 chunk 表取文本。"""
        by_key: dict[str, Snippet] = {
            chunk_key(s.doc_id, s.page, s.text): s for s in bm25_hits
        }
        for doc_id, page, text, title, _score in vector_hits:
            key = chunk_key(doc_id, page, text)
            if key not in by_key:
                # 只在向量路命中的片段：用索引里的文本建一个，title 来自语料
                by_key[key] = Snippet(
                    doc_id=doc_id, title=title, page=page, text=text, score=0.0
                )

        bm25_ranking = [chunk_key(s.doc_id, s.page, s.text) for s in bm25_hits]
        vector_ranking = [chunk_key(d, p, t) for d, p, t, _title, _s in vector_hits]
        fused = rrf_fuse([bm25_ranking, vector_ranking], k=self.rrf_k)

        out: list[Snippet] = []
        for key, score in fused[:top_k]:
            hit = by_key[key]
            out.append(
                Snippet(
                    doc_id=hit.doc_id,
                    title=hit.title,
                    page=hit.page,
                    text=hit.text,  # 逐字原文，未拼接未改写
                    score=round(score, 6),
                )
            )
        return out

    # ------------------------------------------------------------ 诊断

    def describe(self) -> str:
        if self._degraded:
            return f"{self.mode}→bm25（降级：{self.degraded_reason}）"
        if self._index is None:
            return "bm25（纯关键词）"
        return f"{self.mode}（rrf_k={self.rrf_k}｜向量 {self._index.size()} 条）"


def build_hybrid_from_documents(
    documents: Iterable[Document],
    *,
    mode: RetrievalMode = "hybrid",
    embedder: Embedder | None = None,
    index_dir: str | Path | None = None,
    rrf_k: int = DEFAULT_RRF_K,
) -> HybridRetriever:
    """从内存文档建检索器（测试与脚本用，免去落盘再读）。"""
    return HybridRetriever(
        LocalCorpusRetriever(documents),
        mode=mode,
        embedder=embedder,
        index_dir=index_dir,
        rrf_k=rrf_k,
    )


__all__ = [
    "DEFAULT_RRF_K",
    "VALID_MODES",
    "HybridRetriever",
    "RetrievalMode",
    "build_hybrid_from_documents",
    "chunk_key",
    "rrf_fuse",
]
