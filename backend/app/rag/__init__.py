"""知识库：语料加载与检索。

检索有三档（见 `corpus.build_retriever`）：bm25（默认）/ vector / hybrid。
上层只依赖 `contracts.retrieval.Retriever` 协议，换档不影响 Researcher、checkup 与 api。
"""

from .corpus import (
    LocalCorpusRetriever,
    build_retriever,
    chunk_document,
    load_documents,
    tokenize,
)
from .embedding import (
    Embedder,
    EmbeddingCacheMode,
    EmbeddingError,
    EmbeddingReplayMiss,
    build_embedder,
    embed_key,
)
from .hybrid import (
    DEFAULT_RRF_K,
    HybridRetriever,
    build_hybrid_from_documents,
    rrf_fuse,
)
from .vector_index import IndexedChunk, VectorIndex, cosine

__all__ = [
    "DEFAULT_RRF_K",
    "Embedder",
    "EmbeddingCacheMode",
    "EmbeddingError",
    "EmbeddingReplayMiss",
    "HybridRetriever",
    "IndexedChunk",
    "LocalCorpusRetriever",
    "VectorIndex",
    "build_embedder",
    "build_hybrid_from_documents",
    "build_retriever",
    "chunk_document",
    "cosine",
    "embed_key",
    "load_documents",
    "rrf_fuse",
    "tokenize",
]
