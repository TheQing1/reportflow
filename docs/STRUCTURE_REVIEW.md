# 结构审查与修正记录

审查时间：2026-10-24 · 范围：`backend/`、`docs/`、根目录配置

---

## 0. 审查结论

**仓库当时没有任何代码**：只有 `README.md`、`docs/ARCHITECTURE.md`、`.env.example`、`.gitignore`，
以及 13 个空目录（`backend/app/*` 9 个、`backend/tests`、`deploy`、`eval/topics`、`frontend`）。

因此本次审的不是"既有实现"，而是**计划结构一旦落成代码必然踩的坑**。
结论：设计文档的整体判断（分层、DAG、断点续跑、交接协议）是对的，但**目录划分、依赖方向、状态存储口径**三处存在结构性缺陷，直接按原样开发会在阶段一就付出返工代价。

已按下列 9 条修正，并把"依赖方向"变成会失败的测试（`backend/tests/test_layering.py`），防止后续腐化。

---

## 1. 问题清单与修正

### P1 空目录冒充已完成模块

**现象**：`app/api`、`app/llm`、`app/rag`、`app/tracing`、`app/evaluation`、`app/models` 等 9 个目录全空。

**为什么不合理**：目录树是"已完成"的视觉信号。空目录既不能承载职责，git 也不跟踪它（clone 下来就消失），
还会让"当前进度"表失真——看图以为写了一半，实际 0 行。

**修正**：删除全部空目录，改为**按阶段创建**（见 §3 的阶段闸门表）。

**理由**：结构应当描述"已存在的东西"，计划写在文档里即可。

---

### P2 缺契约层，交接协议无处安放 → 会形成反向依赖

**现象**：`ARCHITECTURE.md §4` 把"结构化交接协议"称作"整个项目最关键的设计"，
但目录里没有任何模块承载它；`NodeSpec` 被规划在 `orchestrator/dag.py` 里。

**为什么不合理**：`agents/` 的每个 Agent 都需要知道输入契约（节点 id、章节号、上游产出），
如果契约定义在 `orchestrator/`，就会出现 `agents → orchestrator` 的 import。
再叠加上阶段二 `orchestrator` 要调用 Agent，就成了**双向依赖**：
任何一方重构都会牵连另一方，Mock/真实 Agent 的切换也就不可能做到"调度层零改动"。

**修正**：新增叶子层 `backend/app/contracts/`（只依赖标准库）：

| 文件 | 内容 |
|---|---|
| `nodes.py` | `NodeType` / `NodeStatus` / `NodeSpec` / `Graph` + 静态校验（重复 id、悬空依赖、环） |
| `agent.py` | `AgentContext` / `Agent` Protocol / `AgentRegistry` |
| `task.py` | `TaskStatus` / `TaskState` / `NodeState` / `TaskStore` 端口 |

**理由**：依赖方向因此变成单向 `contracts ← 其他所有层`。
`AgentRegistry` 也放在契约层，编排层拿到的就是一个 `NodeType -> Agent` 的映射，
不需要 import `agents` 包——这是阶段二"MockAgent 与真实 Agent 可切换"的**唯一技术前提**。

---

### P3 `models/` 让编排层直接依赖 ORM

**现象**：原设计 `models/` 放 SQLAlchemy 模型，供 `orchestrator/state.py` 直接读写。

**为什么不合理**：
- 调度逻辑（拓扑推进、阻塞传播、并发控制）根本无法脱离数据库单测；
- 换存储（SQLite → MySQL → Postgres）要动编排层；
- 违反文档自己写的"编排层不关心上层怎么存"（§2 分层职责表）。

**修正**：拆成 `persistence/`（`db.py` 建引擎、`models.py` 表定义、`store.py` 实现 `TaskStore`），
编排层只依赖 `contracts.task.TaskStore` 协议。端口与实现的绑定集中在新增的 `app/assembler.py`（装配根）。

**理由**：ports & adapters。代价是多一层接口 + 一个装配根；收益是
阶段一的调度器可以在纯内存/临时 SQLite 上跑 21 个用例，且换库不动一行业务代码。

---

### P4 状态存储方式与设计文档自相矛盾（Redis 是伪需求）

**现象**：`README` 把 Redis 列为"执行进度 · 结果缓存"的必备组件，`.env.example` 写死 MySQL。

**为什么不合理**：`ARCHITECTURE.md §3.2` 明确写了"**轮询而非事件驱动**，轮询实现简单、崩溃后恢复自然"。
既是单实例 + 轮询 + 状态全落库，Redis 在**正确性上完全不参与**——它只是缓存，没有它系统照样正确。
让一个没有消费者的组件进入"快速开始"，等于把所有验收者挡在 `docker compose up` 之外。

**修正**：
- 状态唯一事实来源 = 数据库；阶段一默认 `sqlite:///<repo>/data/reportflow.db`，`DATABASE_URL` 一键切 MySQL；
- Redis 从"核心依赖"降级为"可选缓存"，README 与 `.env.example` 标注阶段四（SSE 多副本）再引入；
- 配置全部可缺省：零 `.env` 即可跑通阶段一。

**理由**：验收标准要求"可复现的运行命令"，不能以"先装一套 MySQL+Redis"为前提。
延迟引入缓存组件是常规做法，等真的出现跨进程广播需求再加。

---

### P5 断点续跑缺"执行策略"持久化

**现象**：原 `nodes` 表只有 `status/output`，`max_retries` / `timeout` 只存在于内存里的 `NodeSpec`。

**为什么不合理**：续跑时要靠数据库重建 DAG。若 `max_retries`、`timeout_s` 没落库，
重建出的节点会退回代码默认值——**首跑和续跑的执行策略不一致**，
而且这种不一致只在崩溃后出现，测试很难覆盖到。

**修正**：`nodes` 表显式存 `attempt`、`max_retries`、`timeout_s`、`deps`、`payload`、`owner`、`retry_at`。
`NodeState.to_spec()` 从这些列还原 `NodeSpec`。

**理由**：断点续跑要成立，"重建出的 DAG 与首跑等价"是充分必要条件。

---

### P6 状态机伪代码有两个会导致"任务假死"的缺陷

**现象**（`ARCHITECTURE.md §3.2` 原伪代码）：

```python
for n in nodes:                       # ① 只扫一轮
    if n.status == PENDING and any_failed(n.deps): mark_blocked(n)
...
if not ready:
    if any_running(nodes): sleep; continue
    break                             # ② RETRYING 不算 running → 直接判死
```

**为什么不合理**：
- ① 阻塞是**传递**的：串行链上 `review_ch1` 被 BLOCKED 后，`write_ch2` 才具备阻塞条件。
  单轮遍历受列表顺序影响，很可能本轮漏掉 `write_ch2`，而下一轮又没有 running 而直接 `break`，
  于是 `write_ch2` 永远停在 PENDING，任务状态却是"结束"——下游"不执行"是对的，但状态表撒谎。
- ② 引入退避重试（§6.1 明确要求指数退避）后，`RETRYING` 是**非终态但也不在跑**。
  按 `any_running` 判定会把它当成死锁，退避还没结束任务就退出了。

**修正**：`scheduler.py` 中实现为
- 阻塞传播 **迭代到不动点**（`while progressed`），每轮重扫；
- 循环退出条件改为"**所有节点都是终态**"；`RETRYING` 参与等待判定（按 `retry_at` 睡到最早到期者）；
- 引入 `RETRYING` 状态并记录 `retry_at`，`attempt` 在进入重试时自增（幂等键随之变化）。

**理由**：这两条正是"任务已经死了但界面显示运行中"的成因，属于必须在引擎里解决、
不能靠上层兜底的问题。已用 `test_failed_node_blocks_all_downstream`（8 个下游全部 BLOCKED 且不执行）覆盖。

---

### P7 `evaluation` 双份，职责重叠

**现象**：`backend/app/evaluation/` 与根 `eval/` 都规划了"评测执行器"。

**为什么不合理**：两个入口、两处指标实现，最后一定出现"结果对不上、不知道信哪个"。

**修正**（约定，阶段三落地）：根 `eval/` 只放**数据集 + 薄 CLI**；指标计算、judge 调用、校准流程留在
`backend/app/evaluation/`，通过 `python -m app.evaluation` 暴露。

**理由**：与"数据/代码分离"一致，也让评测能复用后端的 LLM 网关与 trace（否则要重写一套计费）。

---

### P8 缺配置加载器，`.env` 与运行目录错位

**现象**：只有 `.env.example`，没有加载代码；而脚本预期在 `backend/` 下运行，`.env` 在仓库根。

**修正**：`core/config.py` 向上查找 `.env`（最多 4 层）+ 环境变量优先 + 代码默认值兜底；
不引入 `python-dotenv` / `pydantic-settings`（阶段一仅 7 个配置项，少一个依赖少一份版本冲突）。

**理由**：`cp .env.example .env` 是文档承诺的第一步，必须真的生效。

---

### P9 CLI 缺席，但验收标准要求"命令行可观察"

**现象**：骨架只有 `api/`（FastAPI），验收却要求"通过日志或命令行观察到"。

**修正**：阶段一先做 `app/cli.py`（`run` / `resume` / `status` / `ls`），REST 层推迟到阶段四随前端一起做。

**理由**：阶段一不需要 HTTP。多一层协议只会让"并行/串行"的观察变间接。

---

## 2. 修正后的结构

```
reportflow/
├── backend/
│   ├── pyproject.toml            # 阶段一唯一依赖：SQLAlchemy
│   ├── app/
│   │   ├── contracts/            # 叶子层：零内部依赖
│   │   │   ├── nodes.py          # NodeType/NodeStatus/NodeSpec/Graph(+校验)
│   │   │   ├── agent.py          # AgentContext/Agent Protocol/AgentRegistry
│   │   │   └── task.py           # TaskStatus/NodeState/TaskStore 端口
│   │   ├── core/                 # 配置、日志、时间
│   │   ├── persistence/          # db / models / store（TaskStore 的 SQL 实现）
│   │   ├── agents/               # 阶段一：mock.py
│   │   ├── orchestrator/         # dag.py 建图 / scheduler.py 调度 / runner.py 生命周期
│   │   ├── assembler.py          # 装配根：唯一绑定"接口→实现"的地方
│   │   └── cli.py                # 命令行入口
│   └── tests/                    # 26 个用例（含依赖方向检查）
├── data/                         # 运行时生成（gitignored）
├── docs/
│   ├── ARCHITECTURE.md           # 设计取舍
│   ├── STRUCTURE_REVIEW.md       # 本文
│   └── STAGE1_ACCEPTANCE.md      # 阶段一验收记录（真实输出）
└── README.md
```

依赖方向（只允许向下）：

```
contracts  ←  core  ←  {persistence, agents}  ←  orchestrator  ←  assembler / cli
```

`test_layering.py` 用 AST 扫描全部模块，断言：
1. 不存在反向 import；
2. `contracts/` 不 import 任何 `app.*`；
3. `orchestrator/` 不 import `persistence` 与 `agents`（只认契约）。

---

## 3. 阶段闸门与目录创建时机

| 阶段 | 新增目录/文件 | 旧路径迁移 |
|---|---|---|
| **一（已完成）** | `contracts/`、`core/`、`persistence/`、`agents/mock.py`、`orchestrator/`、`assembler.py`、`cli.py`、`tests/` | `models/` → `persistence/models.py`；`orchestrator/state.py` → `contracts/task.py` + `persistence/store.py`；`orchestrator/dag.py` 的 `NodeSpec` → `contracts/nodes.py` |
| **二（已完成）** | `llm/`（网关/传输/替身）、`rag/`、`agents/{base,planner,researcher,writer,reviewer}.py`、`persistence/spans.py`、`scripts/` | 新增；`assembler` 增加 `profile="real"` 分支。**没有**单独建 `tracing/`：span 本质是只追加明细表，放在 persistence 就够，为它单开一层是过度设计 |
| 三 | 根 `eval/`（只有数据集 + 薄 CLI）、`app/evaluation/` | `evaluation/` 延后到此阶段创建 |
| 四 | `api/`（FastAPI + SSE）、`frontend/`、`deploy/` | `cli.py` 保留为运维/回归入口 |

阶段二~四的目录**现在不建**，避免出现"空壳看起来像已完成"。

---

## 4. 明确确认合理、未改动的部分

- **自研 DAG 引擎而非 LangGraph**：并发上限、重试粒度、幂等与断点续跑都需要精细控制，判断成立。
- **分章生成 + 尾部串行**：阶段一采用 `serial_tail=True`（write→review 一章一章走），
  保证验收要求的"write/review 串行"可观测，且第 N 章的 review 反馈能约束第 N+1 章。
  流水线模式（`serial_tail=False`）保留在图构建参数里，随时可切换（只改依赖，其余代码不变）。
- **幂等键 `(node_id, attempt)`**：文档 §3.4 的坑点判断准确，已在 `nodes` 表落地
  （`attempt` 落库 + `start_node` 条件更新 + `finish_node` 同事务写产出）。
- **单实例假设**：文档主动写明而非回避，合理。`start_node` 用条件更新，
  将来加 Redis 分布式锁即可平滑多副本，无需改状态机。

---

## 5. 一处仍存在的取舍（留待后续）

尾部串行意味着 **research 全部跑完后，write/review 只能逐章推进**，
5 章任务的尾部耗时约等于 5×(write+review)，没有流水线重叠。
阶段二接入真实 LLM 后，这个串行代价会被放大（单章 10~30s）。
届时若要提速，应改为"分章并行 + Editor 统稿"，但会牺牲跨章衔接——
这是一个需要先用评测数据（阶段三）说话的决定，不做拍脑袋优化。
