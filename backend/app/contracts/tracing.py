"""Span 记录契约。

网关每次调用 LLM 都写一条 span：模型、token、延迟、成本、是否降级。
有了它，"这份研报的钱花在哪个环节"才是可回答的问题，而不是一句宣传。
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class SpanContext:
    """当前正在执行的节点。用 contextvar 传，Agent 完全不需要知道追踪的存在。"""

    task_id: str
    node_id: str
    attempt: int = 1


_current: contextvars.ContextVar[SpanContext | None] = contextvars.ContextVar(
    "reportflow_span_context", default=None
)


def current_span_context() -> SpanContext | None:
    return _current.get()


@contextmanager
def span_context(task_id: str, node_id: str, attempt: int = 1) -> Iterator[SpanContext]:
    ctx = SpanContext(task_id=task_id, node_id=node_id, attempt=attempt)
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


@dataclass(frozen=True)
class Span:
    task_id: str
    node_id: str
    span_type: str  # llm_call / retrieval / validate
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    cost: float
    attempt: int = 1
    degraded: bool = False
    error: str | None = None
    created_at: datetime | None = None


@runtime_checkable
class SpanRecorder(Protocol):
    def record(self, span: Span) -> None: ...


class NullRecorder:
    """空实现。测试里用它，避免为了记录器而建库。"""

    def record(self, span: Span) -> None:  # pragma: no cover - 无副作用
        return None
