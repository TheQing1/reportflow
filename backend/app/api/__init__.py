"""HTTP 接口层（阶段四）。

    python -m app.api          # 起服务，默认 http://127.0.0.1:8000
"""

from .app import create_app

__all__ = ["create_app"]
