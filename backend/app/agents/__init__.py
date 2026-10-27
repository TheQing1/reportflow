"""Agent 实现包。

两套实现共用同一份契约：

| profile | 用途 | 是否联网 |
|---|---|---|
| `mock` | 阶段一回归、编排行为验证 | 否 |
| `real` | 真实生成（LLM 网关 + 本地语料检索） | 是 |

`build_registry` 是唯一的切换点，调度器不认识任何具体 Agent。
"""

from __future__ import annotations

from ..contracts.agent import AgentRegistry
from ..contracts.llm import LLMClient
from ..contracts.nodes import NodeType
from ..contracts.retrieval import Retriever
from .base import JsonAgent, parse_json_object
from .mock import MockAgent, MockAgentConfig
from .planner import PlannerAgent
from .researcher import ResearcherAgent
from .reviewer import ReviewerAgent
from .writer import WriterAgent

__all__ = [
    "JsonAgent",
    "MockAgent",
    "MockAgentConfig",
    "PlannerAgent",
    "ResearcherAgent",
    "ReviewerAgent",
    "WriterAgent",
    "build_mock_registry",
    "build_real_registry",
    "parse_json_object",
]


def build_mock_registry(config: MockAgentConfig | None = None) -> AgentRegistry:
    """为四种节点类型各装配一个 MockAgent 实例。"""
    cfg = config or MockAgentConfig()
    return AgentRegistry({t: MockAgent(t, cfg) for t in NodeType})


def build_real_registry(
    llm: LLMClient, retriever: Retriever, *, tools: object | None = None
) -> AgentRegistry:
    """装配真实 Agent。

    `tools` 为 None 时只有 Researcher 保持原样（无工具）；
    传了才给 Researcher 开启工具调用循环——其余三个 Agent 不需要工具，
    给它们加只会让每次请求都带上一堆用不上的函数声明。
    """
    return AgentRegistry(
        {
            NodeType.PLANNER: PlannerAgent(llm),
            NodeType.RESEARCH: ResearcherAgent(llm, retriever, tools=tools),
            NodeType.WRITE: WriterAgent(llm),
            NodeType.REVIEW: ReviewerAgent(llm),
        }
    )
