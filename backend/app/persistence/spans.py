"""Span 存储与成本归因。

放在 persistence 而不是单开一个 tracing 层：span 本质是"只追加的明细表"，
没有独立业务语义。网关负责产生，这里负责落库与聚合，避免为了一个概念多加一层依赖。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..contracts.tracing import Span
from ..core.timeutil import utcnow
from .models import SpanRow


class SqlSpanRecorder:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def record(self, span: Span) -> None:
        # 记账失败绝不能拖垮业务：这里吞掉异常只打日志，
        # 因为"少一条 span"比"整章生成失败"轻得多
        try:
            with self._sf() as s:
                s.add(
                    SpanRow(
                        task_id=span.task_id or "",
                        node_id=span.node_id or "",
                        span_type=span.span_type,
                        model=span.model,
                        prompt_tokens=span.prompt_tokens,
                        completion_tokens=span.completion_tokens,
                        latency_ms=span.latency_ms,
                        cost=span.cost,
                        attempt=span.attempt,
                        degraded=span.degraded,
                        error=span.error,
                        created_at=span.created_at or utcnow(),
                    )
                )
                s.commit()
        except Exception:  # noqa: BLE001
            from ..core.logging import get_logger

            get_logger("spans").warning("span 落库失败，已忽略", exc_info=True)


@dataclass(frozen=True)
class CostReport:
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cost: float
    degraded_calls: int
    failed_calls: int
    latency_ms_avg: int


@dataclass(frozen=True)
class NodeCost:
    node_id: str
    calls: int
    cost: float
    total_tokens: int


class SpanQuery:
    """只读聚合，给 CLI / 评测用。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def summary(self, task_id: str) -> CostReport:
        with self._sf() as s:
            row = s.execute(
                select(
                    func.count(SpanRow.id),
                    func.coalesce(func.sum(SpanRow.prompt_tokens), 0),
                    func.coalesce(func.sum(SpanRow.completion_tokens), 0),
                    func.coalesce(func.sum(SpanRow.cost), 0.0),
                    func.coalesce(func.avg(SpanRow.latency_ms), 0),
                ).where(SpanRow.task_id == task_id)
            ).one()
            degraded = s.scalar(
                select(func.count(SpanRow.id)).where(
                    SpanRow.task_id == task_id, SpanRow.degraded.is_(True)
                )
            )
            failed = s.scalar(
                select(func.count(SpanRow.id)).where(
                    SpanRow.task_id == task_id, SpanRow.error.is_not(None)
                )
            )
        return CostReport(
            calls=int(row[0] or 0),
            prompt_tokens=int(row[1] or 0),
            completion_tokens=int(row[2] or 0),
            cost=float(row[3] or 0.0),
            degraded_calls=int(degraded or 0),
            failed_calls=int(failed or 0),
            latency_ms_avg=int(row[4] or 0),
        )

    def cached_calls(self, task_id: str) -> int:
        """回放命中的调用次数。

        网关内存里的计数只对"当前进程跑的任务"有效，换一次进程就归零；
        而成本表要在任何时刻都能回答"这份报告是不是白跑的"。
        回放命中的特征很硬：延迟 0、但有真实的 token 用量（usage 从录制里带出来的）。
        """
        with self._sf() as s:
            row = s.execute(
                select(func.count(SpanRow.id)).where(
                    SpanRow.task_id == task_id,
                    SpanRow.latency_ms == 0,
                    SpanRow.prompt_tokens > 0,
                )
            ).one()
        return int(row[0] or 0)

    def by_node(self, task_id: str) -> list[NodeCost]:
        with self._sf() as s:
            rows = s.execute(
                select(
                    SpanRow.node_id,
                    func.count(SpanRow.id),
                    func.coalesce(func.sum(SpanRow.cost), 0.0),
                    func.coalesce(func.sum(SpanRow.prompt_tokens + SpanRow.completion_tokens), 0),
                )
                .where(SpanRow.task_id == task_id)
                .group_by(SpanRow.node_id)
                .order_by(func.sum(SpanRow.cost).desc())
            ).all()
        return [
            NodeCost(
                node_id=r[0],
                calls=int(r[1]),
                cost=float(r[2] or 0.0),
                total_tokens=int(r[3] or 0),
            )
            for r in rows
        ]
