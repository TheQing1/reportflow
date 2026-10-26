# 更新日志

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

---

## [0.5.0] - 2026-10-25 — 工程化闭环

让项目从「能跑的代码」变成「别人能复现的工程」。

### Added

- **GitHub Actions CI**：后端 lint + 139 个测试（Python 3.11 / 3.12 双版本）、前端构建、Docker 镜像构建验证
- **Docker 化**：`backend/Dockerfile`、`frontend/Dockerfile`（nginx 托管 + `/api` 反代）、`docker-compose.yml` 一条命令起全栈
  - nginx 反代里为 SSE 显式关闭响应缓冲。不做这件事，进度条会一直不动、直到任务结束才一次性跳到 100%
- **依赖锁定**：`backend/requirements.txt` 与 `requirements-dev.txt`，与 `pyproject.toml` 双向一致
- `Makefile` 统一命令入口（`make help` 查看全部）
- `.editorconfig` 与 `.pre-commit-config.yaml`
- ruff / mypy / pytest 配置固化进 `pyproject.toml`
- `CONTRIBUTING.md`：分层约束、测试要求、commit 规范、新增 Agent 的步骤

### Fixed

- **补上两个此前完全没声明的运行时依赖**：
  - `PyYAML` —— 评测数据集（`eval/topics/*.yaml`）读写需要，缺失时 import 阶段直接失败
  - `python-multipart` —— 文件上传接口需要。它更隐蔽：服务能正常启动，
    直到第一次调用 `POST /api/corpus/upload` 才抛 `RuntimeError`，因此一直没被发现
- 清理 79 处静态检查问题（未使用导入、过时 `typing` 写法、import 排序、盲异常断言等）
- 统一三处版本号至 `0.5.0`（此前 pyproject 0.2.0 / package.json 0.4.0 / FastAPI 0.4.0 互不一致）

### Changed

- 测试断言收紧：`assertRaises(Exception)` → `assertRaises(TaskExistsError)`，
  前者在异常类型发生变化时依然会通过，等于没测

---

## [0.4.0] - 2026-10 — 接口与前端

### Added

- FastAPI 接口层：REST + SSE 进度推送（轮询数据库而非回调，崩溃重启后前端连上即可续看进度）
- Vue3 前端：任务看板、DAG 进度视图、角标溯源抽屉、发布前体检面板、成本归因面板
- 知识库管理：上传 / 删除 / 全文查看，上传后立即重建索引

---

## [0.3.0] - 2026-10 — 评测

### Added

- 40 主题标注评测集，三档方案对比（单 Agent 直出 / 多 Agent / 多 Agent + Reviewer）
- 配对比较 + 95% 置信区间；判官与生成模型分离（跨厂商）以避免自我偏好

### Changed

- **Reviewer 移出默认流水线**：实测覆盖率仅 +0.3pp（不显著）却贵 49%，
  改为导出时用 `cli export --check` 做一次性体检

---

## [0.2.0] - 2026-10 — Agent 与 LLM 网关

### Added

- Planner / Researcher / Writer / Reviewer 四个 Agent，职责单一可独立替换
- LLM 网关：模型分级、超时降级、token 与成本计量
- 录制回放：改前端或状态机时反复跑同一批 prompt 不再烧钱

---

## [0.1.0] - 2026-10 — DAG 编排引擎

### Added

- 自研轻量 DAG 引擎：拓扑排序调度、并发受限执行、失败阻塞传播到不动点、
  状态持久化与断点续跑（已完成的节点不重跑）
