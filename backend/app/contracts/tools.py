"""工具层的契约：工具定义、参数校验、执行结果。

为什么单独一层而不是塞进 agents：
1. **分层约束**（tests/test_layering.py）：agents 不能反向依赖具体工具实现；
2. 工具要能被单独测试——超时、参数非法、抓取失败这些边界不该靠跑整条流水线去验；
3. 将来加第二个工具（数据库查询、内部 API）时，Agent 一行不用改。

三条设计约束，都是为"不破坏引用溯源"服务的：

- **工具返回的每个片段必须带可回传的原文**（`text` 原样保存，不做摘要改写）。
  上层要拿 `quote` 去原文里做逐字子串匹配，改写过的内容一定匹配失败。
- **工具执行失败不能抛给编排层**。检索只是素材入口，一次网络抖动不该让整章崩掉；
  失败要以 `ok=False` 的形式回给模型，让它自己决定"换个说法再检"还是"如实说没查到"。
- **工具输出要有硬上限**。网页正文动辄几十 KB，不截断会把上下文撑爆，
  而且模型会开始"自由发挥"——这恰恰是我们要防的。
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: 单次工具结果回给模型的最大字符数。
#:
#: 取 4000：约 2~3 条网页摘要的量。再多的话，模型倾向于把整段网页复述进素材，
#: 而不是提炼——那会让"引用逐字可查"变成"引用一整篇网页"，失去可核验性。
MAX_TOOL_OUTPUT_CHARS = 4000
#: 单次抓取的原始正文上限（截断前）。防止个别超大页面拖垮节点。
MAX_FETCH_BYTES = 2 * 1024 * 1024


class ToolError(RuntimeError):
    """工具执行的基类异常（内部用；对外一律转成 ToolResult.ok=False）。"""


class ToolUnavailable(ToolError):
    """工具当前不可用：未配置、被关闭、依赖缺失。属于"预期内"的状态。"""


@dataclass(frozen=True)
class ToolResult:
    """一次工具调用的结果。

    `ok=False` 是**正常返回值**而不是异常：模型需要看到"这次没查到"这一事实，
    才能决定下一步。抛异常会让整个节点失败，损失掉已经花掉的检索与生成。
    """

    ok: bool
    #: 回给模型的文本（已截断）。ok=False 时是失败原因。
    content: str = ""
    #: 结构化片段，供上层做溯源校验（网页检索时是 [{doc_id,title,url,text}]）
    items: tuple[Mapping[str, Any], ...] = ()
    error: str = ""
    #: 耗时，便于成本/性能归因
    elapsed_ms: int = 0
    #: 命中的条目数（截断前），用于判断"是真没有还是被截断了"
    total_items: int = 0

    @classmethod
    def failure(cls, message: str, *, elapsed_ms: int = 0) -> ToolResult:
        return cls(ok=False, error=message, content=f"工具执行失败：{message}", elapsed_ms=elapsed_ms)

    def to_model_text(self) -> str:
        """回填给模型的文本。成功=内容，失败=原因（让模型知道要换策略）。"""
        return self.content if self.ok else f"工具执行失败：{self.error}"


@dataclass(frozen=True)
class ToolSpec:
    """工具的静态声明。`name` 必须与 LLM 看到的函数名一致。"""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    #: 单次调用超时（秒）。必须小于节点超时，否则节点先被杀、拿不到失败信息。
    timeout_s: float = 20.0

    def to_llm_spec(self):  # -> LLMToolSpec
        # 延迟 import：contracts.llm 与 contracts.tools 同层，但避免顶层互相 import 造成环
        from .llm import LLMToolSpec

        return LLMToolSpec(
            name=self.name,
            description=self.description,
            parameters=dict(self.parameters),
        )


@runtime_checkable
class Tool(Protocol):
    """工具的执行端口。实现方只管"按参数干活、把结果装进 ToolResult"。"""

    spec: ToolSpec

    async def run(self, arguments: Mapping[str, Any]) -> ToolResult: ...


class ToolRegistry:
    """名字 -> 工具。模型给的名字必须精确命中，不做模糊匹配（猜错比查不到更糟）。

    除了分发调用，它还承担一个额外职责：**记住本次 Agent 执行期间所有工具
    返回过的片段**。原因是溯源校验需要"模型可能引用到的全部原文"，
    而工具是模型临时决定调用的，Agent 拿不到这些片段的第二份来源。
    没有这一步，模型通过工具查到的合法 fact 会在校验时被当成编造丢弃。
    """

    def __init__(self, tools: Sequence[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        self._items: list[Mapping[str, Any]] = []
        #: 每次 Agent 执行前应清空，避免跨章串味（第 2 章看到第 1 章的网页）
        self._epoch = 0
        for t in tools:
            self.register(t)

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if not name:
            raise ValueError("工具名不能为空")
        if name in self._tools:
            raise ValueError(f"工具名重复: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return sorted(self._tools)

    # ------------------------------------------------------------ 片段收集

    def begin(self) -> None:
        """开始一次新的 Agent 执行：清空上一轮收集的片段。"""
        self._items = []
        self._epoch += 1

    def record_items(self, items: Sequence[Mapping[str, Any]]) -> None:
        self._items.extend(items)

    def last_items(self) -> tuple[Mapping[str, Any], ...]:
        """本轮已收集的片段（供溯源校验合并证据池）。"""
        return tuple(self._items)

    def llm_specs(self) -> tuple[Any, ...]:
        """转成 LLM 侧的工具声明。空表示不给模型任何工具（退化为纯文本补全）。"""
        return tuple(t.spec.to_llm_spec() for t in self._tools.values())

    def describe(self) -> str:
        return ", ".join(self.names()) or "-"


def truncate(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> tuple[str, bool]:
    """按字符截断，返回 (文本, 是否被截断)。

    在句子边界上截断而不是硬切：硬切会把最后一个词割成半个，
    模型据此引用会得到一段"看起来完整但实际不存在于原文"的片段——
    那是溯源最容易漏掉的一种假阳性。
    """
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    cut = max(head.rfind("。"), head.rfind("\n"), head.rfind("；"), head.rfind(". "))
    if cut > limit // 2:
        head = head[: cut + 1]
    return head, True


def strip_html(raw: str) -> str:
    """极简 HTML 取文本：去 script/style、去标签、压空白。

    刻意不引 bs4/lxml：
    - 我们只需要"能读的正文"，不需要精确 DOM；
    - 少一个解析器就少一处版本漂移，也少一份安装失败的可能。
    代价是表格/结构化内容会丢格式——对"抽一句话做引用"这个用途可以接受。
    """
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    # 块级标签换成换行，保住段落边界（否则正文会粘成一行，截断时切到句子中间）
    text = re.sub(r"(?i)</?(p|div|br|li|h[1-6]|tr|section|article)\b[^>]*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def now_ms() -> int:
    return int(time.monotonic() * 1000)


__all__ = [
    "MAX_FETCH_BYTES",
    "MAX_TOOL_OUTPUT_CHARS",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ToolUnavailable",
    "now_ms",
    "strip_html",
    "truncate",
]
