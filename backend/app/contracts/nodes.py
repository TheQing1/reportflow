"""节点与图：DAG 编排的领域模型。

本模块是叶子依赖：只依赖标准库，不 import 任何 app 内部模块。
编排层、持久化层、Agent 层都可以依赖它，它不反过来依赖任何人。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


class NodeType(str, Enum):
    """节点类型。每个类型对应一个 Agent 实现，由 registry 绑定。"""

    PLANNER = "planner"
    RESEARCH = "research"
    WRITE = "write"
    REVIEW = "review"


class NodeStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    RETRYING = "RETRYING"  # 等待退避后重试，带 retry_at，不是终态
    BLOCKED = "BLOCKED"  # 上游失败导致永久不执行，是终态
    CANCELLED = "CANCELLED"


_TERMINAL = frozenset(
    {NodeStatus.DONE, NodeStatus.FAILED, NodeStatus.BLOCKED, NodeStatus.CANCELLED}
)
#: 上游处于这些状态时，下游必须被判为 BLOCKED
FAILURE_STATES = frozenset(
    {NodeStatus.FAILED, NodeStatus.BLOCKED, NodeStatus.CANCELLED}
)
#: 会向下游传播"不可执行"的状态（含 RETRYING 之外的全部失败态）
PROPAGATING_STATES = FAILURE_STATES


def is_terminal(status: NodeStatus) -> bool:
    return status in _TERMINAL


@dataclass(frozen=True)
class NodeSpec:
    """节点的静态定义（不可变）。运行时状态另存于 NodeState。"""

    id: str
    type: NodeType
    deps: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)
    max_retries: int = 1
    timeout_s: float = 120.0

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("node id 不能为空")
        if self.max_retries < 0:
            raise ValueError(f"{self.id}: max_retries 不能为负")
        if self.timeout_s <= 0:
            raise ValueError(f"{self.id}: timeout_s 必须为正")
        if self.id in self.deps:
            raise ValueError(f"{self.id}: 不允许自依赖")
        if len(set(self.deps)) != len(self.deps):
            raise ValueError(f"{self.id}: deps 存在重复项")

    @property
    def max_attempts(self) -> int:
        """总尝试次数 = 首次 + 重试次数。attempt 从 1 开始计数。"""
        return self.max_retries + 1


class GraphError(ValueError):
    """DAG 结构非法。"""


@dataclass(frozen=True)
class Graph:
    """一张待执行的 DAG。构造后即可用 validate() 做静态校验。"""

    nodes: tuple[NodeSpec, ...]

    @classmethod
    def of(cls, nodes: Iterable[NodeSpec]) -> "Graph":
        return cls(tuple(nodes))

    @property
    def by_id(self) -> dict[str, NodeSpec]:
        return {n.id: n for n in self.nodes}

    def get(self, node_id: str) -> NodeSpec:
        try:
            return self.by_id[node_id]
        except KeyError:
            raise KeyError(f"节点不存在: {node_id}") from None

    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self):
        return iter(self.nodes)

    def validate(self) -> None:
        """校验：id 唯一、依赖存在、无环。任何一条不满足都直接抛错。

        这些错误如果在调度期才暴露，代价是任务跑到一半卡死，
        所以必须在建图阶段一次性拦截。
        """
        seen: set[str] = set()
        for n in self.nodes:
            if n.id in seen:
                raise GraphError(f"节点 id 重复: {n.id}")
            seen.add(n.id)

        for n in self.nodes:
            for dep in n.deps:
                if dep not in seen:
                    raise GraphError(f"{n.id}: 依赖的节点不存在: {dep}")

        # Kahn 拓扑排序，剩余节点即环的一部分
        indeg = {n.id: len(n.deps) for n in self.nodes}
        children: dict[str, list[str]] = {n.id: [] for n in self.nodes}
        for n in self.nodes:
            for dep in n.deps:
                children[dep].append(n.id)

        queue = [nid for nid, d in indeg.items() if d == 0]
        visited = 0
        while queue:
            cur = queue.pop()
            visited += 1
            for child in children[cur]:
                indeg[child] -= 1
                if indeg[child] == 0:
                    queue.append(child)

        if visited != len(self.nodes):
            cyclic = sorted(nid for nid, d in indeg.items() if d > 0)
            raise GraphError(f"DAG 存在环，涉及节点: {cyclic}")

    def topological_order(self) -> list[NodeSpec]:
        self.validate()
        indeg = {n.id: len(n.deps) for n in self.nodes}
        children: dict[str, list[str]] = {n.id: [] for n in self.nodes}
        for n in self.nodes:
            for dep in n.deps:
                children[dep].append(n.id)
        queue = sorted(nid for nid, d in indeg.items() if d == 0)
        order: list[NodeSpec] = []
        by_id = self.by_id
        while queue:
            cur = queue.pop(0)
            order.append(by_id[cur])
            for child in sorted(children[cur]):
                indeg[child] -= 1
                if indeg[child] == 0:
                    queue.append(child)
        return order

    def describe(self) -> str:
        """人读的 DAG 摘要，用于 CLI 启动时打印，便于核对拓扑。"""
        lines = []
        for n in self.topological_order():
            deps = ", ".join(n.deps) or "-"
            lines.append(f"  {n.id:<16} type={n.type.value:<9} deps=[{deps}]")
        return "\n".join(lines)
