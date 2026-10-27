"""知识库检索工具：把现有的 Retriever 暴露成一个可被模型调用的函数。

为什么要包一层，而不是直接让 Agent 调 retriever：

模型有了工具之后，"什么时候查、查几次、换什么说法查"由模型决定。
Researcher 原本是**固定查 planner 给的 queries**，一次查完就写素材；
有了这个工具，模型可以：先查一次 → 发现覆盖不够 → 换个词再查 → 补齐再交。

但溯源约束不变：返回的每个片段都是 `Snippet.text`（原文逐字切片），
模型引用的 quote 仍然要能在这些片段里做子串匹配，否则照样被丢弃。

注意这里**不做**段落摘要或"帮我总结一下"这类加工——
一旦加工，`quote` 就不可能逐字命中原文，整个溯源链就断了。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..contracts.retrieval import Retriever
from ..contracts.tools import ToolResult, ToolSpec, now_ms, truncate

#: 单次回给模型的最大片段数。太多会让模型把整段语料复述进正文。
DEFAULT_TOP_K = 4
#: 工具单次调用超时。检索是纯内存计算（BM25）或一次 embedding，
#: 给 15s 足够；hybrid 档首次建索引可能更慢，由上层节点超时兜底。
_TOOL_TIMEOUT_S = 15.0


class KBSearchTool:
    """查本地知识库。与 Researcher 内部的检索引擎是同一份实现。"""

    spec = ToolSpec(
        name="kb_search",
        description=(
            "检索本地知识库（已灌入的研报/政策/统计文档），返回若干条原文片段。"
            "这是首选资料来源：数据可溯源、内容稳定。"
            "返回的片段是原文逐字切片，引用时必须从中逐字复制连续文字。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词。用具体名词与指标名，例如「工商业储能 装机规模 2025」",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回片段数，1~8，默认 4",
                    "minimum": 1,
                    "maximum": 8,
                },
            },
            "required": ["query"],
        },
        timeout_s=_TOOL_TIMEOUT_S,
    )

    def __init__(self, *, retriever: Retriever, top_k: int = DEFAULT_TOP_K) -> None:
        self._retriever = retriever
        self._default_top_k = max(1, min(8, top_k))

    async def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        started = now_ms()
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult.failure("query 不能为空", elapsed_ms=now_ms() - started)

        try:
            top_k = int(arguments.get("top_k") or self._default_top_k)
        except (TypeError, ValueError):
            top_k = self._default_top_k
        top_k = max(1, min(8, top_k))

        try:
            hits = await self._retriever.search(query, top_k=top_k)
        except Exception as exc:  # noqa: BLE001 - 检索失败不该让节点崩掉
            return ToolResult.failure(
                f"知识库检索失败：{type(exc).__name__}: {exc}",
                elapsed_ms=now_ms() - started,
            )

        if not hits:
            # 查到空是"正常结果"，不是失败：模型据此知道该换词或承认没有资料。
            # 若报成失败，模型容易反复重试同一个词，白烧轮次。
            return ToolResult(
                ok=True,
                content="知识库中未检索到相关片段。可以换用更具体的指标名或同义词重试，"
                "或如实说明该点无资料。",
                items=(),
                elapsed_ms=now_ms() - started,
                total_items=0,
            )

        items = tuple(
            {
                "doc_id": sn.doc_id,
                "title": sn.title,
                "page": sn.page,
                "text": sn.text,
                "score": sn.score,
            }
            for sn in hits
        )
        return ToolResult(
            ok=True,
            content=self._render(items),
            items=items,
            elapsed_ms=now_ms() - started,
            total_items=len(items),
        )

    @staticmethod
    def _render(items: tuple[Mapping[str, Any], ...]) -> str:
        blocks: list[str] = []
        for i, it in enumerate(items, 1):
            page = f" 页码={it['page']}" if it.get("page") else ""
            text, _ = truncate(str(it["text"]), limit=1200)
            blocks.append(
                f"[片段{i}] doc_id={it['doc_id']} 标题={it['title']}{page}\n原文：{text}"
            )
        blocks.append(
            "引用要求：source_id 必须照抄上面的 doc_id；"
            "quote 必须从「原文」中逐字复制连续文字。"
        )
        return "\n\n".join(blocks)


def build_kb_search_tool(*, retriever: Retriever, top_k: int = DEFAULT_TOP_K) -> KBSearchTool:
    return KBSearchTool(retriever=retriever, top_k=top_k)


__all__ = ["DEFAULT_TOP_K", "KBSearchTool", "build_kb_search_tool"]
