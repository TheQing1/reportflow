"""TaskStore 的 SQLAlchemy 实现（SQLite / MySQL 通用）。

设计要点：
1. 每次操作一个短事务。断点续跑要求"状态变更"与"产出写入"原子提交，
   否则崩溃会留下"有产出但状态还是 RUNNING"的中间态。
2. 状态迁移一律用带条件的 UPDATE（WHERE status=...），靠 rowcount 判断
   是否抢到。这样将来多副本部署时不需要额外加锁。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from ..contracts.nodes import Graph, NodeStatus, NodeType
from ..contracts.task import NodeState, TaskState, TaskStatus
from ..core.timeutil import utcnow
from .models import NodeRow, TaskRow


class TaskExistsError(RuntimeError):
    pass


class SqlTaskStore:
    """实现 contracts.task.TaskStore 协议。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session = self._sf()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ---------------- 任务与图 ----------------

    def create_task(
        self,
        *,
        task_id: str,
        topic: str,
        sections: int,
        graph: Graph,
        max_review_rounds: int = 2,
        profile: str = "mock",
    ) -> None:
        graph.validate()
        now = utcnow()
        with self._session() as s:
            if s.get(TaskRow, task_id) is not None:
                raise TaskExistsError(f"任务已存在: {task_id}")
            s.add(
                TaskRow(
                    id=task_id,
                    topic=topic,
                    sections=sections,
                    status=TaskStatus.PENDING.value,
                    created_at=now,
                    updated_at=now,
                    max_review_rounds=max_review_rounds,
                    profile=profile,
                )
            )
            s.add_all(
                [
                    NodeRow(
                        task_id=task_id,
                        node_id=n.id,
                        type=n.type.value,
                        status=NodeStatus.PENDING.value,
                        attempt=1,
                        deps=list(n.deps),
                        payload=dict(n.payload),
                        max_retries=n.max_retries,
                        timeout_s=n.timeout_s,
                        updated_at=now,
                    )
                    for n in graph
                ]
            )

    def get_task(self, task_id: str) -> TaskState | None:
        with self._session() as s:
            row = s.get(TaskRow, task_id)
            if row is None:
                return None
            return TaskState(
                id=row.id,
                topic=row.topic,
                sections=row.sections,
                status=TaskStatus(row.status),
                created_at=row.created_at,
                finished_at=row.finished_at,
                max_review_rounds=row.max_review_rounds,
                profile=row.profile,
            )

    def list_task_ids(self, *, limit: int = 10) -> list[str]:
        with self._session() as s:
            return list(
                s.scalars(select(TaskRow.id).order_by(TaskRow.created_at.desc()).limit(limit)).all()
            )

    def list_nodes(self, task_id: str) -> list[NodeState]:
        with self._session() as s:
            rows = s.scalars(
                select(NodeRow).where(NodeRow.task_id == task_id).order_by(NodeRow.node_id)
            ).all()
            return [self._to_state(r) for r in rows]

    def load_graph(self, task_id: str) -> Graph | None:
        """从节点行重建 DAG。执行策略（重试次数/超时）也一并还原。"""
        states = self.list_nodes(task_id)
        if not states:
            return None
        return Graph.of(st.to_spec() for st in states)

    @staticmethod
    def _to_state(row: NodeRow) -> NodeState:
        return NodeState(
            task_id=row.task_id,
            node_id=row.node_id,
            type=NodeType(row.type),
            status=NodeStatus(row.status),
            attempt=row.attempt,
            deps=tuple(row.deps or ()),
            payload=dict(row.payload or {}),
            max_retries=row.max_retries,
            timeout_s=row.timeout_s,
            output=dict(row.output) if row.output is not None else None,
            error=row.error,
            revision_hint=row.revision_hint,
            review_round=row.review_round,
            owner=row.owner,
            retry_at=row.retry_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )

    # ---------------- 节点状态迁移 ----------------

    def start_node(self, *, task_id: str, node_id: str, attempt: int, owner: str) -> bool:
        """PENDING -> RUNNING。条件更新保证同一 (node, attempt) 只被抢一次。"""
        now = utcnow()
        with self._session() as s:
            res = s.execute(
                update(NodeRow)
                .where(
                    NodeRow.task_id == task_id,
                    NodeRow.node_id == node_id,
                    NodeRow.status == NodeStatus.PENDING.value,
                    NodeRow.attempt == attempt,
                )
                .values(
                    status=NodeStatus.RUNNING.value,
                    owner=owner,
                    started_at=now,
                    updated_at=now,
                    error=None,
                )
            )
            return res.rowcount == 1

    def finish_node(
        self, *, task_id: str, node_id: str, attempt: int, output: Mapping[str, Any]
    ) -> bool:
        now = utcnow()
        with self._session() as s:
            res = s.execute(
                update(NodeRow)
                .where(
                    NodeRow.task_id == task_id,
                    NodeRow.node_id == node_id,
                    NodeRow.status == NodeStatus.RUNNING.value,
                    NodeRow.attempt == attempt,
                )
                .values(
                    status=NodeStatus.DONE.value,
                    output=dict(output),
                    error=None,
                    finished_at=now,
                    updated_at=now,
                    retry_at=None,
                )
            )
            return res.rowcount == 1

    def fail_node(
        self,
        *,
        task_id: str,
        node_id: str,
        attempt: int,
        error: str,
        retry_at: datetime | None,
    ) -> bool:
        now = utcnow()
        if retry_at is not None:
            values: dict[str, Any] = dict(
                status=NodeStatus.RETRYING.value,
                attempt=attempt + 1,
                error=error,
                retry_at=retry_at,
                finished_at=now,
                updated_at=now,
            )
        else:
            values = dict(
                status=NodeStatus.FAILED.value,
                error=error,
                retry_at=None,
                finished_at=now,
                updated_at=now,
            )
        with self._session() as s:
            res = s.execute(
                update(NodeRow)
                .where(
                    NodeRow.task_id == task_id,
                    NodeRow.node_id == node_id,
                    NodeRow.status == NodeStatus.RUNNING.value,
                    NodeRow.attempt == attempt,
                )
                .values(**values)
            )
            return res.rowcount == 1

    def block_node(self, *, task_id: str, node_id: str, reason: str) -> bool:
        """只对"还没跑成"的节点生效：PENDING / RETRYING / FAILED。

        DONE 的节点永远不该被阻塞——它是下游判定的依据，不能被覆盖。
        """
        now = utcnow()
        with self._session() as s:
            res = s.execute(
                update(NodeRow)
                .where(
                    NodeRow.task_id == task_id,
                    NodeRow.node_id == node_id,
                    NodeRow.status.in_(
                        [
                            NodeStatus.PENDING.value,
                            NodeStatus.RETRYING.value,
                            NodeStatus.FAILED.value,
                        ]
                    ),
                )
                .values(
                    status=NodeStatus.BLOCKED.value,
                    error=reason,
                    retry_at=None,
                    updated_at=now,
                )
            )
            return res.rowcount == 1

    def claim_orphans(self, *, task_id: str, owner: str) -> int:
        """RUNNING 且 owner != 当前进程 → 视为孤儿（上一个进程已死）重置。

        attempt 保持不变：崩溃重跑沿用同一个幂等键，
        已经成功写库的节点不会重跑（状态已是 DONE，不在此集合内）。
        """
        now = utcnow()
        with self._session() as s:
            res = s.execute(
                update(NodeRow)
                .where(
                    NodeRow.task_id == task_id,
                    NodeRow.status == NodeStatus.RUNNING.value,
                    NodeRow.owner != owner,
                )
                .values(
                    status=NodeStatus.PENDING.value,
                    owner=None,
                    started_at=None,
                    updated_at=now,
                )
            )
            return res.rowcount or 0

    def promote_due_retries(self, *, task_id: str) -> int:
        now = utcnow()
        with self._session() as s:
            res = s.execute(
                update(NodeRow)
                .where(
                    NodeRow.task_id == task_id,
                    NodeRow.status == NodeStatus.RETRYING.value,
                    NodeRow.retry_at <= now,
                )
                .values(status=NodeStatus.PENDING.value, retry_at=None, updated_at=now)
            )
            return res.rowcount or 0

    def reset_failed(self, *, task_id: str) -> int:
        now = utcnow()
        with self._session() as s:
            res = s.execute(
                update(NodeRow)
                .where(
                    NodeRow.task_id == task_id,
                    NodeRow.status.in_([NodeStatus.FAILED.value, NodeStatus.BLOCKED.value]),
                )
                .values(
                    status=NodeStatus.PENDING.value,
                    attempt=1,
                    error=None,
                    retry_at=None,
                    owner=None,
                    started_at=None,
                    finished_at=None,
                    updated_at=now,
                )
            )
            s.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id)
                .values(status=TaskStatus.PENDING.value, finished_at=None, updated_at=now)
            )
            return res.rowcount or 0

    def reopen_for_revision(
        self,
        *,
        task_id: str,
        review_node_id: str,
        targets: Sequence[str],
        hint: str,
    ) -> int:
        """Reviewer 打回：目标节点回到 PENDING 且 attempt+1。

        三件事必须在一个事务里完成，否则崩溃后可能只剩半套（写了 PENDING 没加 attempt
        就会撞上幂等检查，重写永远不生效）：
        1. targets 全部重置为 PENDING、attempt+1、清 owner/时间戳、写入修复要求；
        2. review 节点的 review_round +1（打回轮数，用于判断上限）；
        3. 任务状态回到 RUNNING（可能已因失败被标 PARTIAL）。
        """
        now = utcnow()
        with self._session() as s:
            reset = s.execute(
                update(NodeRow)
                .where(
                    NodeRow.task_id == task_id,
                    NodeRow.node_id.in_(list(targets)),
                    NodeRow.status.in_([NodeStatus.DONE.value, NodeStatus.PENDING.value]),
                )
                .values(
                    status=NodeStatus.PENDING.value,
                    attempt=NodeRow.attempt + 1,
                    error=None,
                    revision_hint=hint,
                    retry_at=None,
                    owner=None,
                    started_at=None,
                    finished_at=None,
                    updated_at=now,
                )
            )
            bumped = s.execute(
                update(NodeRow)
                .where(
                    NodeRow.task_id == task_id,
                    NodeRow.node_id == review_node_id,
                )
                .values(review_round=NodeRow.review_round + 1, updated_at=now)
            )
            s.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id)
                .values(status=TaskStatus.RUNNING.value, finished_at=None, updated_at=now)
            )
            if reset.rowcount == 0 or bumped.rowcount == 0:
                s.rollback()
                return 0
            return reset.rowcount

    def set_task_status(self, task_id: str, status: TaskStatus) -> None:
        now = utcnow()
        values: dict[str, Any] = {"status": status.value, "updated_at": now}
        if status.is_final:
            values["finished_at"] = now
        with self._session() as s:
            s.execute(update(TaskRow).where(TaskRow.id == task_id).values(**values))

    def count_nodes_by_status(self, task_id: str) -> dict[NodeStatus, int]:
        with self._session() as s:
            rows = s.execute(
                select(NodeRow.status, func.count())
                .where(NodeRow.task_id == task_id)
                .group_by(NodeRow.status)
            ).all()
            return {NodeStatus(status): count for status, count in rows}
