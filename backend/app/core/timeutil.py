"""时间工具。全系统统一 UTC naive datetime，入库/出库口径一致。"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """当前 UTC 时间（naive，保留微秒）。

    用 naive 是为了 SQLite/MySQL 行为一致：带 tzinfo 的 datetime
    在 SQLite 里会被写成带偏移的字符串，读取时类型不稳定。
    保留微秒是必须的——节点耗时普遍在百毫秒级，截断到秒会让
    状态表里的 duration 全是 0 或 1000ms。
    """
    return datetime.now(UTC).replace(tzinfo=None)


def fmt(dt: datetime | None) -> str:
    return dt.strftime("%H:%M:%S") if dt else "-"


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None
