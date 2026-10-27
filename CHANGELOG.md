# 更新日志

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

编排层换成 LangGraph，检索层从单一 BM25 扩成三档（默认不变），对外契约与评测基线口径刻意保持一致。
详见 [docs/STAGE5_ACCEPTANCE.md](docs/STAGE5_ACCEPTANCE.md)。

### Added

- **检索三档**：`RETRIEVAL_MODE=bm25|vector|hybrid`，`build_retriever(mode=...)` 统一装配，默认 `bm25`
  - `rag/embedding.py`：OpenAI 兼容 `/embeddings` 客户端，**独立于 LLM 的**录制回放缓存，键含模型名
  - `rag/vector_index.py`：纯 Python 余弦索引，带「模型名 + 全部 chunk 文本」指纹，落盘 `data/vector_index/`
  - `rag/hybrid.py`：`HybridRetriever` + `rrf_fuse` 纯函数（RRF，k=60）
  - 新配置项：`EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL`（默认 `BAAI/bge-m3`）/
    `EMBEDDING_TIMEOUT` / `EMBEDDING_BATCH_SIZE` / `RRF_K` / `VECTOR_INDEX_DIR`
- **编排层依赖**：`langgraph>=0.6,<0.7`、`langgraph-checkpoint>=3.0,<4.0`、`langgraph-checkpoint-sqlite>=3.0,<4.0`
- `docs/STAGE5_ACCEPTANCE.md`：迁移映射、踩坑与修正（barrier 边 / SystemExit 变 CancelledError /
  serial_tail 判据错致默认配置只生成第 1 章 / 流水线 END 提前收工 / 闸门让路即卡死）、实测数据、未完成项
- `tests/test_graph_langgraph.py::TestTopologyMatrix`：`serial_tail × with_review` 四种组合
  都必须把每一章跑完的回归矩阵（4 条），已做变异测试验证（注回旧判据后有 2 条立即失败）
- `/api/health` 增加 `retrieval_mode` / `retrieval_effective` / `retrieval_degraded` / `embedding_model`；
  前端顶栏显示**实际生效**的检索档位（降级时带标记）——按配置显示会让人以为向量检索在跑，实际在跑关键词

### Changed

- **编排从自研 DAG 引擎迁到 LangGraph `StateGraph`**：`Send` 做运行时动态 fan-out、barrier 边做真汇合、
  条件回边做写-审-改闭环、`RetryPolicy` 做节点级重试、Checkpointer 按 `thread_id = reportflow:<task_id>` 断点续跑
- **节点 status / output / attempt / review_round 仍然双写 `nodes` 表**，图的 state 只做推进与路由的簿记。
  SSE、exporter、spans 成本归因的消费方一行没改——换框架不该改变它们的契约
- 节点命名完全沿用 `planner` / `research_ch{i}` / `write_ch{i}` / `review_ch{i}`：spans 按 `node_id` 归因、
  前端 DagView 按 `_ch(\d+)$` 分列、exporter 按 `write_ch{i}` 取正文，改名等于同时改动三个消费方
- `requirements.txt` 与 `pyproject.toml` 同步新增三个依赖；版本上限锁大版本，避免评测基线因漂移不可复现
- 文档：README（核心特性 / 架构图 / 技术选型 / 已知边界）、ARCHITECTURE（编排层与检索层）、
  CONTRIBUTING（新增 Agent 的步骤）、NEXT_TASKS_P0_P1（标注完成状态）

### Removed

- `orchestrator/dag.py`（约 100 行）与 `orchestrator/scheduler.py`（约 443 行）：自研建图与调度循环
- `tests/test_dag.py`（9 条）与 `tests/test_scheduler.py`（7 条）：被测对象已不存在，覆盖由
  `tests/test_graph_langgraph.py` 接手

### Fixed

- **Reviewer 打回不再靠"重置节点 + attempt+1"迂回**：StateGraph 允许成环，条件边直接回边即可。
  `attempt` 回归它本来的语义（第几次尝试），不再承担控制流职责
- **默认配置下只生成第 1 章（最严重的一个）**：`runner.py` 从落库 deps 反推 `serial_tail` 时，
  判据写成"deps 里是否含 `review_ch`"。评测 B 组（`REVIEW_IN_PIPELINE=false`，也是默认值）
  没有 Reviewer 节点，判据恒为 False → 串行尾部被误判成流水线 → 下游提前派发 → 闸门让路 → 该章永久 PENDING。
  现象是静默少几章、任务仍以 PARTIAL 收尾。判据改为"`write_ch{i}`(i>1) 的 deps 是否多于 1 个"，
  对"有没有 Reviewer"不敏感。**这个 bug 直接污染评测口径**，因为默认配置正是 B 组
- **流水线模式（`serial_tail=False`）各章直连 `END` 导致"第一章跑完整图就收工"**：
  LangGraph 在任一分支到达 `END` 时即判整图结束。改为各章先汇到 `pipeline_join` 再连 `END`。
  现象是"3 章任务只跑了 ch1，节点表留着 4 个 PENDING，任务却报完成"
- **依赖闸门一度被误加上"轮询等待"**：上游刚 `finish_node` 的瞬间闸门读库会看到依赖仍在 RUNNING，
  判 skip 后 LangGraph 不再重触发，于是曾用 2s（后调到 180s）轮询等待去兜底。
  但那是在掩盖上游"拓扑判据错"的真因（见上一条）；判据修好后闸门不需要等，
  已删掉等待逻辑、回归纯同步判定 `_gate`。教训记在 `docs/STAGE5_ACCEPTANCE.md` 坑五
- **检索层的 RRF 尺度与下游预算折扣冲突**：`app/agents/researcher.py` 增加 `_looks_like_rrf()` 判定，
  RRF 档下把"分数乘 0.5"换成名次惩罚 `_RRF_RANK_PENALTY = 30`。
  RRF 相邻名次只差约 1.6%，字面乘 0.5 会把片段推出 61 名开外，降权会退化成硬过滤

### Known issues

- 40 主题评测**未在 LangGraph 改造后重跑**。README「效果与代价」的数字仍是改造前的编排基线，
  新基线需另起目录（如 `eval/results/v9-langgraph/`）重跑，不能与旧数据直接比
- `vector` / `hybrid` 两档没有 40 主题三档量化对照，只有单主题实测；也没有"哪一路召回"的可视化
  （顶栏只显示实际生效档位与降级原因）
- 流水线模式（`serial_tail=False`）的拓扑正确性已覆盖，但"各章确实重叠推进"没有时间重叠度断言
  ——串行档有，流水线档没有对应的量化口径

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
