"""ORM 表定义：tasks / nodes。

放在 persistence 而不是顶层 models/：
表结构是"存储实现细节"，编排层不应该看见 SQLAlchemy 的 declarative 基类。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON as MySQLJSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


# SQLite 用 JSON，MySQL 用原生 JSON（SQLAlchemy 的通用 JSON 在 MySQL 上等价，
# 这里显式声明只为将来需要索引 JSON 字段时不必改表）。
_JSON = JSON().with_variant(MySQLJSON(), "mysql")


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    topic: Mapped[str] = mapped_column(String(512), nullable=False)
    sections: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 每章允许被打回的轮数上限（Reviewer 打回重写）
    max_review_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    #: 生成这份结果用的是哪套 Agent（mock / real）。评测必须能区分，
    #: 否则没法回答"多 Agent 与单 Agent 差多少"
    profile: Mapped[str] = mapped_column(String(16), nullable=False, default="mock")


class NodeRow(Base):
    """一个任务的所有节点。复合主键 (task_id, node_id)。

    attempt 是幂等键的一半：同一 (node_id, attempt) 成功过一次就不再重跑，
    但被打回后的下一次生成会用 attempt+1，因此不会被误判成"已完成"。
    """

    __tablename__ = "nodes"

    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    deps: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False, default=dict)
    # 执行策略必须落库：断点续跑要复原出与首次执行一致的 DAG，
    # 否则重启后重试次数/超时会悄悄退回默认值。
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    timeout_s: Mapped[float] = mapped_column(Float, nullable=False, default=120.0)
    output: Mapped[dict[str, Any] | None] = mapped_column(_JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 上一轮 Review 给出的修复要求。只传"要改什么"，不传整段对话历史
    revision_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 该节点被打回的轮数（记在 review 节点上，用于判断是否到上限）
    review_round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    owner: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SpanRow(Base):
    """每次 LLM 调用一行。成本归因与"重写占比"都从这里算。

    只追加不更新：量大时按 task_id 归档即可，不需要复杂的关系设计。
    """

    __tablename__ = "spans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    span_type: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
