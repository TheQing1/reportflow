"""本地语料检索（BM25，零外部依赖）。

为什么阶段二不用向量库：
- 目标是让 Researcher 拿到**真实、可溯源**的原文片段，而不是追求召回率；
- ChromaDB + BGE 要下模型（几百 MB）、要 GPU/CPU 推理，在没有网络的机器上装不起来；
- 关键词检索完全可复现，也不会因为 embedding 模型升级导致评测基线漂移。

代价很明确：同义改写的 query 召回不了。等阶段三有了评测数据，再决定是否值得换向量检索。

分块策略：按空行分段，超长段再按句子窗口切（400 字，重叠 80 字）。
每条片段都保留 doc_id 与 page，这是 Fact.quote 可溯源的根。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path

from ..contracts.retrieval import Document, Snippet

_ASCII_WORD = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._%-]*")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_SENT_END = re.compile(r"(?<=[。！？；])")

CHUNK_SIZE = 400
CHUNK_OVERLAP = 80
SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt"}
#: 短于这个长度的片段不进索引：`## 装机规模` 这类纯标题能被关键词检索命中，
#: 但它没有任何可引用内容，会白白占掉 top_k 名额（实际踩到过）
MIN_CHUNK_CHARS = 12
_HEADING_ONLY = re.compile(r"^#{1,6}\s")


def tokenize(text: str) -> list[str]:
    """中文用字二元组 + 英文/数字按词。不引分词库，避免一个依赖拖垮整个安装。"""
    tokens: list[str] = [w.lower() for w in _ASCII_WORD.findall(text)]
    cjk = "".join(_CJK.findall(text))
    tokens.extend(cjk[i : i + 2] for i in range(len(cjk) - 1))
    return tokens


def _split_paragraphs(text: str) -> list[str]:
    raw = re.split(r"\n\s*\n", text)
    return [p.strip() for p in raw if p.strip()]


def _window(chunk: str) -> list[str]:
    """超长段落按句子边界滑窗，避免把一句话切成两半。"""
    if len(chunk) <= CHUNK_SIZE:
        return [chunk]
    sentences = [s for s in _SENT_END.split(chunk) if s]
    out: list[str] = []
    buf = ""
    for sent in sentences:
        if len(buf) + len(sent) > CHUNK_SIZE and buf:
            out.append(buf)
            buf = buf[-CHUNK_OVERLAP:] if CHUNK_OVERLAP < len(buf) else ""
        buf += sent
    if buf:
        out.append(buf)
    return out


def chunk_document(doc: Document) -> list[Document]:
    pieces: list[Document] = []
    for para in _split_paragraphs(doc.text):
        if _HEADING_ONLY.match(para):
            continue  # 标题只作为文档标题，不作为可引用片段
        for piece in _window(para):
            if len(piece.strip()) < MIN_CHUNK_CHARS:
                continue
            pieces.append(
                Document(
                    doc_id=doc.doc_id,
                    title=doc.title,
                    text=piece,
                    page=doc.page,
                )
            )
    return pieces


class LocalCorpusRetriever:
    """BM25 检索器。corpus 在构造时一次性建好倒排，检索是纯内存计算。"""

    #: 语料根目录。reload() 依赖它，from_directory 会设置。
    root: Path

    def __init__(self, documents: Iterable[Document], *, k1: float = 1.5, b: float = 0.75):
        self._chunks: list[Document] = []
        for doc in documents:
            self._chunks.extend(chunk_document(doc))
        self._k1 = k1
        self._b = b

        self._tokens: list[Counter[str]] = [Counter(tokenize(c.text)) for c in self._chunks]
        self._lengths = [sum(t.values()) for t in self._tokens]
        self._avg_len = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0

        df: Counter[str] = Counter()
        for toks in self._tokens:
            df.update(toks.keys())
        n = len(self._chunks)
        self._idf = {
            term: math.log(1 + (n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    @classmethod
    def from_directory(cls, path: str | Path) -> LocalCorpusRetriever:
        root = Path(path)
        docs: list[Document] = []
        if root.is_dir():
            for file in sorted(root.rglob("*")):
                if file.suffix.lower() not in SUPPORTED_SUFFIXES or not file.is_file():
                    continue
                text = file.read_text(encoding="utf-8", errors="replace")
                docs.append(
                    Document(doc_id=file.stem, title=_first_heading(text) or file.stem, text=text)
                )
        retriever = cls(docs)
        retriever.root = root
        return retriever

    def reload(self) -> int:
        """重新读取目录（上传/删除文档后必须调用）。返回片段数。

        没有它的话，上传完还得重启服务才能检索到——那会让人以为上传失败了。
        """
        rebuilt = type(self).from_directory(self.root)
        self._chunks = rebuilt._chunks
        self._tokens = rebuilt._tokens
        self._idf = rebuilt._idf
        self._lengths = rebuilt._lengths
        self._avg_len = rebuilt._avg_len
        return len(self._chunks)

    def corpus_size(self) -> int:
        return len(self._chunks)

    def full_text(self, doc_id: str) -> str:
        """按 doc_id 拼回整篇原文（chunk 顺序即原文顺序）。"""
        parts = [c.text for c in self._chunks if c.doc_id == doc_id]
        return "\n".join(parts)

    def doc_ids(self) -> list[str]:
        return sorted({c.doc_id for c in self._chunks})

    async def search(
        self, query: str, *, top_k: int = 4, chapters_hint: str = ""
    ) -> list[Snippet]:
        if not self._chunks:
            return []
        q_tokens = tokenize(f"{query} {chapters_hint}")
        if not q_tokens:
            return []
        scored: list[tuple[float, int]] = []
        for idx, toks in enumerate(self._tokens):
            score = 0.0
            for term in set(q_tokens):
                tf = toks.get(term)
                if not tf:
                    continue
                idf = self._idf.get(term, 0.0)
                denom = tf + self._k1 * (
                    1 - self._b + self._b * (self._lengths[idx] / (self._avg_len or 1))
                )
                score += idf * tf * (self._k1 + 1) / (denom or 1)
            if score > 0:
                scored.append((score, idx))
        scored.sort(reverse=True)

        results: list[Snippet] = []
        for score, idx in scored[:top_k]:
            chunk = self._chunks[idx]
            results.append(
                Snippet(
                    doc_id=chunk.doc_id,
                    title=chunk.title,
                    page=chunk.page,
                    text=chunk.text,
                    score=round(score, 4),
                )
            )
        return results


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        if stripped:
            return stripped[:40]
    return None


def load_documents(paths: Sequence[str | Path]) -> list[Document]:
    docs: list[Document] = []
    for p in paths:
        root = Path(p)
        if not root.is_dir():
            continue
        for file in sorted(root.rglob("*")):
            if file.suffix.lower() in SUPPORTED_SUFFIXES and file.is_file():
                text = file.read_text(encoding="utf-8", errors="replace")
                docs.append(
                    Document(doc_id=file.stem, title=_first_heading(text) or file.stem, text=text)
                )
    return docs
