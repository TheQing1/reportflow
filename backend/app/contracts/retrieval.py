"""检索契约。

Researcher 依赖这个端口而不是 ChromaDB：阶段二用的是基于本地语料的
关键词检索（零依赖、可复现），后续换向量检索只需换实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Document:
    """知识库里的一篇文档。page 为 None 表示无分页概念（如 markdown）。"""

    doc_id: str
    title: str
    text: str
    page: int | None = None


@dataclass(frozen=True)
class Snippet:
    """检索命中的最小单元，是 Fact.quote 的合法来源。"""

    doc_id: str
    title: str
    page: int | None
    text: str
    score: float = 0.0

    def label(self) -> str:
        page = f" P.{self.page}" if self.page else ""
        return f"{self.doc_id}《{self.title}》{page}"


@runtime_checkable
class Retriever(Protocol):
    async def search(
        self, query: str, *, top_k: int = 4, chapters_hint: str = ""
    ) -> list[Snippet]: ...

    def corpus_size(self) -> int: ...

    def full_text(self, doc_id: str) -> str:
        """按 doc_id 取整篇原文。

        评测要用它核对"正文断言是否被所引原文支持"：
        Writer 只拿到片段，但审计时必须回到完整上下文，
        否则会因为片段被切断而误判成"不支持"。
        """

    def doc_ids(self) -> list[str]:
        """知识库里全部文档 id。

        发布前体检用它反查"这条数据其实出自哪篇"——
        只拿到被引用的那几篇是不够的，错误恰恰是挂到了别的文档上。
        """
        ...
