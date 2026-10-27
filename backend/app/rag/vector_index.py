"""向量索引：构建、落盘、余弦查询。

为什么自己写而不引 faiss / 向量数据库：

- 语料只有 22 篇 / 79 片段。79 × 1024 维的暴力点积在纯 Python 里是毫秒级，
  引一个几十 MB 的原生依赖换不来任何可感知收益；
- CI 装包受限，且项目从一开始就明确"零重量级依赖"（见 pyproject 与 ARCHITECTURE §9）；
- 自己写才能把"每条向量对应哪段原文"这件事**显式**存下来——
  这正是引用可溯源的前提。向量库只保证"能查到某个 id"，不保证 id 背后的文本没被改过。

因此索引里存的是 `IndexedChunk`（doc_id/title/page/**原文**）+ 向量，
检索只做排序，返回的文本永远是建索引时那段原文，不经过任何模型处理。

失效策略：索引带一个指纹（模型名 + 全部 chunk 文本）。
语料一变、模型一换，指纹就对不上，索引自动作废重建——
绝不能出现"新语料配旧向量"这种静默错误结果。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts.retrieval import Document
from ..core.logging import get_logger
from ..core.timeutil import iso, utcnow
from .embedding import Embedder

#: 索引文件格式版本。布局变化时 +1，旧文件直接判废
INDEX_VERSION = 1
#: 落盘文件名（单文件：79 条向量没必要拆成多文件再自己维护一致性）
INDEX_FILE = "index.json"


@dataclass(frozen=True)
class IndexedChunk:
    """索引里的最小单元。文本是原文的逐字切片，检索时原样返回。"""

    doc_id: str
    title: str
    page: int | None
    text: str

    @classmethod
    def from_document(cls, doc: Document) -> IndexedChunk:
        return cls(doc_id=doc.doc_id, title=doc.title, page=doc.page, text=doc.text)

    def to_json(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "page": self.page,
            "text": self.text,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> IndexedChunk:
        page = raw.get("page")
        return cls(
            doc_id=str(raw["doc_id"]),
            title=str(raw.get("title") or raw["doc_id"]),
            page=int(page) if page is not None else None,
            text=str(raw["text"]),
        )


def corpus_fingerprint(model: str, chunks: Sequence[IndexedChunk]) -> str:
    """模型名 + 全部片段文本的指纹。

    用 \\x00 分隔而不是直接拼接：否则 "ab"+"c" 与 "a"+"bc" 会算出同一个指纹，
    语料真的换了却判断成没换——这类碰撞不会报错，只会让检索结果莫名其妙。
    """
    digest = hashlib.sha256()
    digest.update(f"v{INDEX_VERSION}\x00{model}\x00".encode())
    for chunk in chunks:
        digest.update(f"{chunk.doc_id}\x00{chunk.page}\x00{chunk.text}\x00".encode())
    return digest.hexdigest()


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """纯 Python 余弦相似度。

    调用方已经预归一化时不会走这里（见 VectorIndex.search），
    这个函数留给测试与少量零散比较用。
    """
    if len(a) != len(b) or not a:
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na**0.5) * (nb**0.5))


def _normalize(vector: Sequence[float]) -> list[float]:
    """入库即归一化：检索时只剩点积，79 次点积不用每次再算一遍模长。"""
    norm = sum(x * x for x in vector) ** 0.5
    if norm <= 0.0:
        return [0.0] * len(vector)
    return [x / norm for x in vector]


class VectorIndex:
    """一块向量 + 对应原文片段。构建是异步的（要调 embedding），查询是同步的。"""

    def __init__(
        self,
        embedder: Embedder,
        *,
        index_dir: str | Path | None = None,
    ) -> None:
        self._embedder = embedder
        self.dir = Path(index_dir) if index_dir else None
        self._chunks: list[IndexedChunk] = []
        self._vectors: list[list[float]] = []  # 已 L2 归一化
        self._fingerprint: str = ""
        self._log = get_logger("rag.vector_index")

    # ------------------------------------------------------------ 状态

    def size(self) -> int:
        return len(self._chunks)

    def chunks(self) -> list[IndexedChunk]:
        return list(self._chunks)

    def fingerprint(self) -> str:
        return self._fingerprint

    def matches(self, model: str, chunks: Sequence[IndexedChunk]) -> bool:
        """索引是否恰好对应这批片段 + 这个模型。"""
        if not self._chunks or len(self._chunks) != len(chunks):
            return False
        return self._fingerprint == corpus_fingerprint(model, chunks)

    def clear(self) -> None:
        self._chunks = []
        self._vectors = []
        self._fingerprint = ""

    # ------------------------------------------------------------ 构建

    async def build(self, chunks: Sequence[IndexedChunk], *, force: bool = False) -> int:
        """为给定片段建索引。指纹一致且没 force 时直接复用（不调 embedding）。

        复用是**省钱**的关键：同一语料重复启动服务不该重复计费，
        而"指纹一致"已经包含了模型名与全部原文，复用是安全的。
        """
        items = list(chunks)
        fingerprint = corpus_fingerprint(self._embedder.model, items)
        if not force and self.matches(self._embedder.model, items):
            return len(self._chunks)

        if not items:
            self.clear()
            return 0

        texts = [c.text for c in items]
        vectors = await self._embedder.embed_texts(texts)
        if len(vectors) != len(items):
            raise ValueError(f"向量条数 {len(vectors)} 与片段数 {len(items)} 不一致")

        # 维度不一致时直接报错：混进另一个模型的向量只会得到无意义的相似度
        dims = {len(v) for v in vectors}
        if len(dims) != 1:
            raise ValueError(f"同一批向量维度不一致: {sorted(dims)}")

        self._chunks = items
        self._vectors = [_normalize(v) for v in vectors]
        self._fingerprint = fingerprint
        self.save()
        return len(self._chunks)

    # ------------------------------------------------------------ 查询

    def search(
        self, query_vector: Sequence[float], *, top_k: int = 4
    ) -> list[tuple[str, int | None, str, float]]:
        """返回 (doc_id, page, text, score)，按相似度降序。

        text 取自 `IndexedChunk.text`，即建索引时的原文，逐字未改。
        """
        if not self._chunks or not query_vector or top_k <= 0:
            return []
        q = _normalize(query_vector)
        if len(q) != len(self._vectors[0]):
            raise ValueError(
                f"查询向量维度 {len(q)} 与索引维度 {len(self._vectors[0])} 不一致"
                f"（模型 {self._embedder.model}）"
            )
        scored: list[tuple[float, int]] = []
        for idx, vec in enumerate(self._vectors):
            # 双方都已归一化，余弦 = 点积
            score = sum(x * y for x, y in zip(q, vec, strict=True))
            scored.append((score, idx))
        # 同分按索引顺序（即语料顺序）排，保证结果可复现
        scored.sort(key=lambda pair: (-pair[0], pair[1]))

        out: list[tuple[str, int | None, str, float]] = []
        for score, idx in scored[:top_k]:
            chunk = self._chunks[idx]
            out.append((chunk.doc_id, chunk.page, chunk.text, score))
        return out

    # ------------------------------------------------------------ 持久化

    def save(self) -> None:
        """原子落盘。没有配置目录时静默跳过（纯内存模式，测试用）。"""
        if self.dir is None or not self._chunks:
            return
        payload = {
            "v": INDEX_VERSION,
            "model": self._embedder.model,
            "dim": len(self._vectors[0]) if self._vectors else 0,
            "fingerprint": self._fingerprint,
            "count": len(self._chunks),
            "built_at": iso(utcnow()),
            "chunks": [c.to_json() for c in self._chunks],
            # 不四舍五入：与 embedding 缓存同理，落盘再读回来的向量必须逐位相同，
            # 否则"用磁盘索引"与"重建索引"两种走法的排序会有 1e-8 级差异。
            "vectors": [[float(x) for x in v] for v in self._vectors],
        }
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            target = self.dir / INDEX_FILE
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, target)  # 原子替换：被杀不会留下半个索引
        except OSError as exc:
            # 落盘失败只影响下次启动要重建（多花一次 embedding 钱），不该让检索挂掉
            self._log.warning("向量索引落盘失败 %s: %s", self.dir, exc)

    def load(self) -> bool:
        """尝试从磁盘恢复。文件不存在/版本不符/损坏/指纹对不上都返回 False。"""
        if self.dir is None:
            return False
        path = self.dir / INDEX_FILE
        if not path.is_file():
            return False
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._log.warning("向量索引文件损坏，将重建 %s: %s", path, exc)
            return False
        if not isinstance(raw, dict) or raw.get("v") != INDEX_VERSION:
            return False
        if raw.get("model") != self._embedder.model:
            return False

        raw_chunks = raw.get("chunks")
        raw_vectors = raw.get("vectors")
        if not isinstance(raw_chunks, list) or not isinstance(raw_vectors, list):
            return False
        if len(raw_chunks) != len(raw_vectors) or not raw_chunks:
            return False
        try:
            chunks = [IndexedChunk.from_json(item) for item in raw_chunks]
            vectors = [[float(x) for x in vec] for vec in raw_vectors]
        except (KeyError, TypeError, ValueError) as exc:
            self._log.warning("向量索引内容不合法，将重建 %s: %s", path, exc)
            return False
        if len({len(v) for v in vectors}) != 1:
            return False
        fingerprint = str(raw.get("fingerprint") or "")
        if fingerprint != corpus_fingerprint(self._embedder.model, chunks):
            return False

        self._chunks = chunks
        self._vectors = vectors
        self._fingerprint = fingerprint
        return True

    def clear_disk(self) -> None:
        if self.dir is not None and self.dir.is_dir():
            shutil.rmtree(self.dir, ignore_errors=True)


__all__ = [
    "INDEX_FILE",
    "INDEX_VERSION",
    "IndexedChunk",
    "VectorIndex",
    "corpus_fingerprint",
    "cosine",
]
