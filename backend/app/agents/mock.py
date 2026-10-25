"""MockAgent：阶段一唯一可用的 Agent 实现。

职责只有两个：
1. 返回结构合法的**假**输出，让编排层能跑通依赖传递；
2. 提供可复现的故障注入（fail_nodes）与耗时模拟（latency），
   用来验证"并行/串行/失败阻塞/断点续跑"这四件事。

它不产生任何真实内容，输出里统一带 MOCK 标记，避免被误当成真实结果。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..contracts.agent import AgentContext
from ..contracts.nodes import NodeType


class MockInjectedFailure(RuntimeError):
    """人为注入的节点失败（验收用）。"""


DEFAULT_LATENCY: dict[NodeType, float] = {
    NodeType.PLANNER: 0.15,
    NodeType.RESEARCH: 0.60,
    NodeType.WRITE: 0.45,
    NodeType.REVIEW: 0.30,
}


@dataclass(frozen=True)
class MockAgentConfig:
    #: 每种节点类型的模拟耗时（秒）
    latency_s: Mapping[NodeType, float] = field(
        default_factory=lambda: dict(DEFAULT_LATENCY)
    )
    #: 永远失败的节点 id 集合（重试也会失败，用于触发 BLOCKED 传播）
    fail_nodes: frozenset[str] = frozenset()
    #: 随机抖动上限，让并行节点的完成时刻错开，日志里看得更清楚
    jitter_s: float = 0.15
    #: 固定随机种子，保证验收输出可复现
    seed: int = 20261024


def _chapter_of(payload: Mapping[str, Any]) -> int:
    value = payload.get("chapter", 0)
    return int(value) if isinstance(value, (int, float, str)) and str(value).isdigit() else 0


class MockAgent:
    """一个实例只负责一种节点类型，由 AgentRegistry 按类型装配。"""

    def __init__(self, node_type: NodeType, config: MockAgentConfig | None = None) -> None:
        self.node_type = node_type
        self.config = config or MockAgentConfig()

    async def execute(self, ctx: AgentContext) -> dict[str, Any]:
        node = ctx.node
        if node.id in self.config.fail_nodes:
            # 先睡一下再失败：让日志里的失败时机看起来像真实超时/报错
            await asyncio.sleep(self.config.latency_s.get(self.node_type, 0.1) / 2)
            raise MockInjectedFailure(
                f"injected failure on {node.id} (attempt={ctx.attempt})"
            )

        base = self.config.latency_s.get(self.node_type, 0.2)
        rng = random.Random(f"{self.config.seed}:{node.id}:{ctx.attempt}")
        await asyncio.sleep(base + rng.uniform(0, self.config.jitter_s))

        handler = {
            NodeType.PLANNER: self._plan,
            NodeType.RESEARCH: self._research,
            NodeType.WRITE: self._write,
            NodeType.REVIEW: self._review,
        }[self.node_type]
        return handler(ctx)

    # ---------------- 假产出 ----------------

    def _plan(self, ctx: AgentContext) -> dict[str, Any]:
        sections = int(ctx.node.payload.get("sections", 3))
        topic = str(ctx.node.payload.get("topic", "未命名主题"))
        return {
            "mock": True,
            "topic": topic,
            "outline": [
                {"chapter": i, "title": f"{topic} · 第 {i} 部分"}
                for i in range(1, sections + 1)
            ],
        }

    def _research(self, ctx: AgentContext) -> dict[str, Any]:
        ch = _chapter_of(ctx.node.payload)
        return {
            "mock": True,
            "chapter": ch,
            "summary": f"[MOCK] 第 {ch} 章素材摘要，覆盖 3 个数据点。",
            "key_facts": [
                {
                    "claim": f"[MOCK] 第 {ch} 章事实 {k}",
                    "source_id": f"doc_{ch:02d}",
                    "page": 10 + k,
                    "quote": f"[MOCK] 第 {ch} 章引用原文片段 {k}",
                }
                for k in (1, 2, 3)
            ],
            "open_questions": [],
        }

    def _write(self, ctx: AgentContext) -> dict[str, Any]:
        ch = _chapter_of(ctx.node.payload)
        research = ctx.upstream_of(f"research_ch{ch}")
        facts = research.get("key_facts", [])
        return {
            "mock": True,
            "chapter": ch,
            "markdown": (
                f"## {ch}. [MOCK] 第 {ch} 章正文\n\n"
                + "\n".join(
                    f"- {f['claim']} [^{f['source_id']}]" for f in facts  # type: ignore[index]
                )
            ),
            "citation_map": {f["source_id"]: f["page"] for f in facts},  # type: ignore[index]
            "attempt": ctx.attempt,
        }

    def _review(self, ctx: AgentContext) -> dict[str, Any]:
        ch = _chapter_of(ctx.node.payload)
        return {
            "mock": True,
            "chapter": ch,
            "verdict": "pass",  # 阶段一不做真实校验，恒通过
            "defects": [],
        }
