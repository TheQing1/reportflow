"""工具层：Agent 可调用的外部能力。

与 `agents/` 的分工：agents 决定**何时**调用，tools 只负责**怎么**执行。
这样加工具不改 Agent，改 Agent 不改工具，也方便单独测超时/失败/截断这些边界。

当前提供两个工具：

| 工具 | 默认 | 用途 |
|---|---|---|
| `kb_search` | 开 | 查本地知识库（与 Researcher 原有的检索同一份语料） |
| `web_search` | **关** | 查互联网。默认关闭：联网内容会变，一开评测基线就不可复现 |

默认关闭这一点是刻意的。README「已知边界」里写"不具备实时联网检索"，
现在可以按需打开，但**不会**改变默认路径的行为。
"""

from __future__ import annotations

from ..contracts.tools import (
    MAX_TOOL_OUTPUT_CHARS,
    Tool,
    ToolError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    ToolUnavailable,
)
from .kb_search import KBSearchTool, build_kb_search_tool
from .web_search import WebSearchTool, build_web_search_tool

__all__ = [
    "MAX_TOOL_OUTPUT_CHARS",
    "KBSearchTool",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ToolUnavailable",
    "WebSearchTool",
    "build_default_registry",
    "build_kb_search_tool",
    "build_web_search_tool",
]


def build_default_registry(
    *,
    retriever: object | None = None,
    enable_web: bool = False,
    web_top_k: int = 3,
    kb_top_k: int = 4,
) -> ToolRegistry:
    """按配置装配工具集。

    `retriever` 为 None 时只注册联网工具（mock profile 没有知识库）；
    反之亦然。宁可少给工具，也不要给一个"名字在、调用必失败"的空壳——
    那会诱导模型反复尝试一个永远失败的路径。
    """
    tools: list[Tool] = []
    if retriever is not None:
        tools.append(build_kb_search_tool(retriever=retriever, top_k=kb_top_k))
    if enable_web:
        tools.append(build_web_search_tool(enabled=True, top_k=web_top_k))
    return ToolRegistry(tools)
