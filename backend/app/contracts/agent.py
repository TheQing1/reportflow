"""Agent 契约：编排层只认识这个 Protocol，不认识任何具体实现。

这是阶段一能"Mock 与真实 Agent 可切换"的前提——调度器拿到的始终是
`dict[NodeType, Agent]`，换成 MockAgent 还是 LLM Agent 都无需改调度代码。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .nodes import NodeSpec, NodeType


@dataclass(frozen=True)
class AgentContext:
    """一次节点执行的完整输入。

    只给"依赖节点的结构化产出"，不给任何对话历史——这是交接协议的落点。
    `hint` 是上一轮 Review 给出的修复要求（打回重写时才有），
    它是**唯一**允许跨越轮次传递的自然语言，且必须由 Review 结构化产出后转写，
    不允许 Agent 自己攒一段对话历史塞进来。
    """

    task_id: str
    node: NodeSpec
    attempt: int
    upstream: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    hint: str | None = None

    def upstream_of(self, node_id: str) -> Mapping[str, Any]:
        return self.upstream.get(node_id, {})


@runtime_checkable
class Agent(Protocol):
    """Agent 是纯函数：(AgentContext) -> JSON 可序列化的 dict。

    失败通过抛异常表达，由调度器统一做重试/终态判定，
    Agent 自己不关心重试次数与状态流转。
    """

    node_type: NodeType

    async def execute(self, ctx: AgentContext) -> dict[str, Any]: ...


class AgentRegistry:
    """NodeType -> Agent 的映射表，缺失即报错（不静默兜底）。"""

    def __init__(self, agents: Mapping[NodeType, Agent]) -> None:
        self._agents = dict(agents)

    def get(self, node_type: NodeType) -> Agent:
        try:
            return self._agents[node_type]
        except KeyError:
            raise LookupError(f"没有为节点类型 {node_type.value} 注册 Agent") from None

    def registered_types(self) -> set[NodeType]:
        return set(self._agents)

    def describe(self) -> str:
        parts = []
        for t in NodeType:
            if t in self._agents:
                parts.append(f"{t.value}={type(self._agents[t]).__name__}")
            else:
                parts.append(f"{t.value}=<none>")
        return ", ".join(parts)
