"""服务启动入口：`python -m app.api`。"""

from __future__ import annotations

import argparse

import uvicorn

from .app import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ReportFlow HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    if args.reload:
        # 热重载模式下必须给字符串路径，uvicorn 自己 import 并调 factory
        uvicorn.run("app.api.app:create_app", host=args.host, port=args.port, factory=True)
    else:
        uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
