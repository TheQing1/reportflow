"""任务与状态存储契约。

编排层依赖的是这里的 `TaskStore` 协议，而不是 SQLAlchemy 或 MySQL。
这样做的好处是调度逻辑可以在纯内存存储上跑单测，也可以日后换 MySQL
而不动编排代码；坏处是多了一层接口——考虑到断点续跑是核心卖点，
这层抽象值得。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .nodes import Graph, NodeSpec, NodeStatus, NodeType


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"  # 全部节点成功
    PARTIAL = "PARTIAL"  # 有节点完成、也有节点失败/阻塞，交付已完成部分
    FAILED = "FAILED"  # 一个节点都没成
    CANCELLED = "CANCELLED"

    @property
    def is_final(self) -> bool:
        return self in _TASK_FINAL


_TASK_FINAL = frozenset(
    {TaskStatus.DONE, TaskStatus.PARTIAL, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


@dataclass(frozen=True)
class TaskState:
    id: str
    topic: str
    sections: int
    status: TaskStatus
    created_at: datetime
    finished_at: datetime | None = None
    max_review_rounds: int = 2
    profile: str = "mock"


@dataclass(frozen=True)
class NodeState:
    """节点的运行时快照。DB 是唯一事实来源，调度器每轮读它决策。"""

    task_id: str
    node_id: str
    type: NodeType
    status: NodeStatus
    attempt: int
    deps: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)
    max_retries: int = 1
    timeout_s: float = 120.0
    output: Mapping[str, Any] | None = None
    error: str | None = None
    revision_hint: str | None = None
    review_round: int = 0
    owner: str | None = None
    retry_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def to_spec(self) -> NodeSpec:
        """从持久化状态重建节点定义 —— 断点续跑靠这个复原整张 DAG。"""
        return NodeSpec(
            id=self.node_id,
            type=self.type,
            deps=tuple(self.deps),
            payload=dict(self.payload),
            max_retries=self.max_retries,
            timeout_s=self.timeout_s,
        )


@runtime_checkable
class TaskStore(Protocol):
    """状态存储端口。所有方法都必须是"单次事务内完成"的原子操作。"""

    def create_task(
        self,
        *,
        task_id: str,
        topic: str,
        sections: int,
        graph: Graph,
        max_review_rounds: int = 2,
        profile: str = "mock",
    ) -> None: ...

    def get_task(self, task_id: str) -> TaskState | None: ...

    def list_task_ids(self, *, limit: int = 10) -> list[str]: ...

    def list_nodes(self, task_id: str) -> list[NodeState]: ...

    def load_graph(self, task_id: str) -> Graph | None:
        """从已落库的节点行重建 DAG，用于进程重启后续跑。"""
        ...

    def start_node(
        self, *, task_id: str, node_id: str, attempt: int, owner: str
    ) -> bool:
        """PENDING -> RUNNING 的原子抢占。返回 False 表示已被别人抢先。"""
        ...

    def finish_node(
        self, *, task_id: str, node_id: str, attempt: int, output: Mapping[str, Any]
    ) -> bool:
        """RUNNING -> DONE 并写入产出（同一事务，避免"有产出但状态没更新"）。"""
        ...

    def fail_node(
        self,
        *,
        task_id: str,
        node_id: str,
        attempt: int,
        error: str,
        retry_at: datetime | None,
    ) -> bool:
        """RUNNING -> RETRYING（retry_at 非空）或 FAILED（retry_at 为空）。"""
        ...

    def block_node(self, *, task_id: str, node_id: str, reason: str) -> bool: ...

    def claim_orphans(self, *, task_id: str, owner: str) -> int:
        """把属于已死进程的 RUNNING 节点重置为 PENDING（重跑本次尝试）。"""
        ...

    def promote_due_retries(self, *, task_id: str) -> int: ...

    def reset_failed(self, *, task_id: str) -> int:
        """把 FAILED / BLOCKED 节点重置为 PENDING（attempt 归 1）。

        用于"修完根因后重跑"。DONE 节点绝不受影响。
        """
        ...

    def reopen_for_revision(
        self,
        *,
        task_id: str,
        review_node_id: str,
        targets: Sequence[str],
        hint: str,
    ) -> int:
        """Reviewer 打回：把 targets 重置为 PENDING 且 attempt+1，并给 review 节点 +1 轮。

        `attempt+1` 是关键：幂等键 (node_id, attempt) 变了，上游"已完成就不再跑"
        的保护才不会把重写挡住（文档 §3.4 提到的那个坑）。
        """
        ...

    def set_task_status(self, task_id: str, status: TaskStatus) -> None: ...

    def count_nodes_by_status(self, task_id: str) -> dict[NodeStatus, int]: ...
