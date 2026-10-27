"""日志。阶段一的验收靠日志观察并行/串行，所以格式里必须带时间戳（毫秒）。"""

from __future__ import annotations

import logging
import time
from typing import TextIO

_FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)-5s %(name)-26s %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"


class _UtcFormatter(logging.Formatter):
    """统一用 UTC，与数据库中落盘的时间戳口径一致，避免日志和 DB 对不上。"""

    def converter(self, timestamp: float | None = None) -> time.struct_time:
        # 基类把 converter 标注为 (float) -> struct_time。直接写 `converter = time.gmtime`
        # 能跑（格式化时基类总会传 timestamp），但签名对不上——mypy strict 会拦，
        # 而它拦得对：那种写法依赖基类实现细节，上游一改就是运行期 TypeError。
        return time.gmtime(timestamp if timestamp is not None else time.time())


def setup_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_UtcFormatter(_FORMAT, datefmt=_DATEFMT))
    root = logging.getLogger("reportflow")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"reportflow.{name}")
