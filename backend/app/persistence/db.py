"""数据库引擎与会话工厂。

阶段一默认 SQLite（零运维即可跑通验收），生产切 MySQL 只改 DATABASE_URL。
WAL 模式让"边写状态边读状态"的调度循环不会互相阻塞。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def _ensure_sqlite_dir(url: str) -> None:
    if not url.startswith("sqlite:///"):
        return
    raw = url.removeprefix("sqlite:///")
    if raw.startswith(":memory:") or not raw:
        return
    Path(raw).parent.mkdir(parents=True, exist_ok=True)


def build_engine(url: str, *, echo: bool = False) -> Engine:
    _ensure_sqlite_dir(url)
    if url.startswith("sqlite"):
        engine = create_engine(
            url,
            echo=echo,
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30.0},
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        return engine

    return create_engine(url, echo=echo, future=True, pool_pre_ping=True, pool_recycle=3600)


def create_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
