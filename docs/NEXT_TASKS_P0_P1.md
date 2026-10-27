# ReportFlow 后续任务清单（P0 / P1）

> 本文件**自包含**：不依赖任何对话上下文，可直接复制到新对话交给其他模型独立执行。
> 范围：仅 P0 与 P1。P2（框架关键词表述）与 P3（K8s / 分布式）本次不展开。

---

## 执行状态（最后更新：T1 / T2 完成后）

| 任务 | 状态 | 验收记录 |
|---|---|---|
| T1【P0】编排替换：自研 DAG → LangGraph | **已完成** | [STAGE5_ACCEPTANCE.md](STAGE5_ACCEPTANCE.md) |
| T2【P0】混合检索：BM25 + 向量 + RRF 融合 | **已完成** | [STAGE5_ACCEPTANCE.md](STAGE5_ACCEPTANCE.md) |
| T3【P1】Function Calling / 联网检索工具 | **未开始**（依赖 T1，T1 已就绪） | — |

几点与本文原计划**不同**的落地事实，读下文任务描述时请以这里为准：

1. **自研实现已删除，没有保留回退开关**——这是**用户决策，不是遗漏**。本文风险项建议的
   `ORCHESTRATOR=langgraph|builtin` 最终没做：用户明确选"彻底删除，只保留 LangGraph"，
   理由是"仓库里同时存在两套编排，面试官会质疑主链路到底是哪个"。
   旧实现见 git 历史。代价是这次迁移不可运行时降级，只能回滚到迁移前的 commit。
2. **T1 的测试目标从"139 条不减少"变成了净增**：删掉 `test_dag.py`(9) 与 `test_scheduler.py`(7)
   的同时新增 `test_graph_langgraph.py`(28)，全量从 139 → **260 passed**。
3. **T2 的评测结论没有产出**。本文要求"用 40 主题评测集产出三档量化对照"，实际只做到
   "三档可用 + 离线可复现 + 单主题实测"；40 主题三档对比**未重跑**（LangGraph 改造后基线也一起缺），
   属已知未完成项。**不要**把历史基线数字当成 T2 的评测结论引用。
4. **T2 的方案 A 里"向量存 sqlite-vec"改成了纯 Python 索引**：语料只有 22 篇 / 79 片段，
   暴力点积毫秒级；且自己写才能把"每条向量对应哪段原文"显式存下来（溯源前提）。
   理由与取舍见 [STAGE5_ACCEPTANCE.md](STAGE5_ACCEPTANCE.md)。
5. **T3 的前置已满足，但另有一项前置没写进本文**：缓存 key 要纳入 tools 字段（本文已提），
   此外 `LLM_MODE=replay` 下的评测可复现性约束同样适用于工具调用——未配置时必须完全不联网。

---

## 0. 总体执行顺序说明

三个任务按依赖排布，**T1 与 T2 可并行，T3 依赖 T1**：

```
T1【P0】编排替换：自研 DAG → LangGraph      ──┐
                                              ├─→ T3【P1】Function Calling / 联网检索工具
T2【P0】混合检索：BM25 + 向量 + RRF 融合     ──┘
```

- **T1 与 T2 分属不同层，互不阻塞**：T1 动 `orchestrator/`（编排层），T2 动 `rag/`（检索层）。
  项目有强制分层约束（见 §1.3），两层之间没有横向依赖，可同时开工。
- **T3 必须在 T1 之后**：工具调用需要 Agent 节点具备循环执行能力，而这正是 T1 要重写的部分。
- **每个任务都必须单独可验证**：完成后各自跑通测试 + 评测，不要攒到最后一起验证。
- **强烈建议 T1 保留自研实现作为回退开关**（见 T1 风险项），不要直接删除。

---

## 1. 项目背景速览

### 1.1 项目是什么

ReportFlow 是一个**多 Agent 研报生成流水线**：输入一个行业主题，自动产出一份分章节的研报初稿，
**每个数据断言都能追溯到知识库里的具体文档**。

核心区别点（也是求职时的卖点）：不是"问答式 AI"，而是"产出有结构长文档的流水线"——
Planner 拆大纲 → Researcher 并行检索 → Writer 分章撰写 → Reviewer 交叉校验。

### 1.2 目录结构与关键文件

仓库根：`reportflow/`

| 路径 | 作用 |
|---|---|
| `backend/app/contracts/` | **叶子层**，跨层契约，零内部依赖。`nodes.py`（NodeSpec/Graph/NodeType/NodeStatus）、`agent.py`（AgentContext/AgentRegistry）、`retrieval.py`（Retriever/Snippet/Document）、`task.py`、`tracing.py`、`llm.py`、`eval.py`、`handoff.py` |
| `backend/app/core/` | 配置与日志。`config.py` 的 `Settings.load()` 读取 `.env`；**注意** `PROJECT_ROOT` 由 `Path(__file__).parents[3]` 推导 |
| `backend/app/orchestrator/` | **本次 T1 要改的地方**。`dag.py`（`build_report_graph` 建图）、`scheduler.py`（调度循环：并发/重试/阻塞传播/断点续跑）、`runner.py`（`TaskRunner`：submit / run 任务生命周期） |
| `backend/app/agents/` | 四个 Agent：`planner.py`、`researcher.py`、`writer.py`、`reviewer.py`，均继承 `base.py` 的 `JsonAgent`；另有 `mock.py` |
| `backend/app/rag/` | **本次 T2 要改的地方**。`corpus.py`（BM25 检索实现 `LocalCorpusRetriever`）、`budget.py`（每章文档预算）、`store.py`（知识库读写） |
| `backend/app/llm/` | `gateway.py`（模型分级/超时降级/计量）、`openai_compat.py`（OpenAI 兼容 HTTP）、`cache.py`（录制回放）、`fake.py`（`ScriptedLLM`） |
| `backend/app/persistence/` | `store.py`（`SqlTaskStore`：节点状态持久化）、`spans.py`（成本归因）、`models.py`、`db.py` |
| `backend/app/api/app.py` | FastAPI 工厂函数 `create_app()`（用 `--factory` 启动）；REST + SSE |
| `backend/app/assembler.py` | `build_runtime()` 组装依赖，是唯一注册 Agent 的地方 |
| `backend/app/checkup.py` / `exporter.py` | 发布前体检 / markdown 拼装 |
| `backend/tests/` | 139 个测试。**全部是同步函数，内部用 `asyncio.run()`**，因此不需要 pytest-asyncio |
| `eval/run_eval.py` + `eval/topics/` | 40 个标注主题的评测集与入口（A/B/C 三档方案对比） |
| `backend/requirements.txt` | 运行时依赖清单（改依赖必须同步改 `pyproject.toml` 的 `dependencies`） |

> ⚠️ 上表是**开工前**的结构，保留作决策记录。T1 / T2 完成后有变化：
> `orchestrator/` 现在是 `graph_langgraph.py`（图与节点）+ `runner.py`（任务生命周期），`dag.py` 与
> `scheduler.py` 已删除；`rag/` 新增 `embedding.py` / `vector_index.py` / `hybrid.py`，
> `corpus.py` 多了 `build_retriever(mode=...)` 工厂；测试数从 139 变为 260。

### 1.3 必须遵守的架构约束（违反会导致测试失败）

**分层依赖方向**（由 `backend/tests/test_layering.py` 用 AST 强制校验）：

```
contracts  ←  core  ←  {persistence, agents, rag, llm, evaluation}  ←  orchestrator  ←  {assembler, cli, api}
```

- `contracts/` 是叶子层，**零内部依赖**
- 每层只能依赖左边的层，**不能反向、不能跨层横向依赖**
- `api/` 不含业务逻辑，只做协议转换
- 例：`orchestrator` 不能 import `rag`（文档预算函数是通过 `runner.py` 的 `budget_fn` **注入**的，就是为了不破坏这条约束）

其他硬约束：

- **引用必须能溯源**：Researcher 产出的每条素材，其 `quote` 必须能在原文中**逐字**找到，找不到就丢弃。这是一切改动的前提，任何任务都不得绕过。
- **程序校验优先于模型判断**：能用代码判定的（如数字是否在原文出现）就不要调 LLM。
- 改 prompt 必须跑评测，"感觉变好了"不算数。

### 1.4 常用命令

```bash
cd backend
python -m pytest -q                    # 跑全部测试（本文写作时 139 passed；T1/T2 完成后为 260）
ruff check .                           # 静态检查（当前 All checks passed）
python -m app.cli doctor                # 环境自检：key 可用？知识库空不空？
python -m app.cli run --profile real --topic "2026年中国新型储能行业" --sections 1
python -m app.cli export --task-id <id> --check
cd ../eval && python run_eval.py --arm B   # 单档评测
```

注意：本机 pip 装包若失败，需显式指定代理与官方源：
`pip install --proxy http://127.0.0.1:63680 -i https://pypi.org/simple <pkg>`

---

## 2. 编排技术选型：用 LangGraph 替代自研 DAG

### 2.1 为什么换

自研引擎（`orchestrator/` 约 300 行）质量不差，但它是**简历关键词的缺口**：
2026 年 AI 应用开发 / AI 全栈岗位的 JD 里，LangChain / LangGraph / LlamaIndex 几乎必现，
而 HR 初筛常按关键词过滤——技术深度再好，简历过不了这一关也没用。

### 2.2 候选对比与结论

| 候选 | 判断 |
|---|---|
| **LangGraph** | **选它。** 原生支持循环/分支/并行/持久化，正是本项目所需 |
| Prefect / Dagster | 数据工程向，无 Agent 语义与 LLM 生态，不适合 |
| Temporal | 需要独立集群与服务，对单进程项目过重 |
| CrewAI / AutoGen | 更高层，循环与分支控制力弱；本项目需要精细控制重试粒度与打回循环 |
| Dify / n8n | 低代码平台，不适合作为代码项目的核心编排层 |

**关键兼容性**：LangGraph 只依赖 `langchain-core` 的基础抽象，**不强制使用 LangChain 的链或 SDK**。
节点函数内可以是任意 Python 代码、任意 LLM 客户端。这与本项目"刻意不引入 openai SDK、
只用 OpenAI 兼容 HTTP 接口"的哲学不冲突——现有 `app/llm/` 网关可以原样保留。

### 2.3 能力对应关系（自研 → LangGraph）

| 自研概念（现有位置） | LangGraph 对应物 | 说明 |
|---|---|---|
| `Graph` + 拓扑排序（`dag.py`） | `StateGraph` + `add_edge` | 图结构由框架维护 |
| 节点状态机 PENDING/RUNNING/DONE（`scheduler.py`） | Checkpointer 的 checkpoint 序列 | 状态由框架隐式表达，不再需要手写状态机 |
| 并发受限执行 `concurrency=3`（`scheduler.py`） | super-step 自动并行 + `Send` | `Send` 支持运行时动态数量的并行（章节数由 Planner 决定） |
| 失败重试 `max_attempts` + 指数退避（`scheduler.py`） | `add_node(..., retry_policy=RetryPolicy(max_attempts=N))` | 节点级重试，框架负责 |
| 阻塞传播到不动点（`scheduler.py` 的 `_propagate_blockers`） | 条件边 + 状态字段 | 上游失败写入 state，下游条件边判断跳过 |
| 断点续跑 / `claim_orphans`（`store.py`） | Checkpointer + `thread_id` | 进程崩溃后复用同 `thread_id`  invoke 即可继续 |
| Reviewer 打回重写循环（`_apply_revisions`） | `add_conditional_edges` 回边 | 天然支持循环，无需"复用 attempt 机制"这种迂回做法 |
| SSE 轮询推送（`api/app.py`） | `astream_events` 或沿用现有轮询 | 建议先沿用轮询，降低改动面 |
| `NodeSpec` / `Graph`（`contracts/nodes.py`） | `TypedDict` State + 节点函数 | 契约层需相应调整 |
| 成本归因按节点（`spans.py`） | 节点函数内继续用 `span_context` | 现有 tracing 机制可保留 |

### 2.4 最小必要知识（不熟悉底层实现也够用）

只需掌握 8 个概念，**不需要理解 Pregel 超步调度原理或 channel 内部机制**：

1. **State**：用 `TypedDict` 定义；并行分支要写入同一字段时，该字段需带 reducer，
   例如 `summaries: Annotated[list, operator.add]`（否则后写的覆盖先写的）。
2. **Node**：普通函数 `(state) -> dict`，**返回要更新的字段**，不是返回完整 state。
3. **Edge**：`add_edge(a, b)` 固定转移；`add_conditional_edges(a, 路由函数, [...候选])` 动态路由，
   路由函数返回下一个节点名（或 `END`）。
4. **Checkpointer**：持久化执行状态。开发用 `SqliteSaver`，生产用 `PostgresSaver`，测试用 `MemorySaver`。
   项目已有 SQLite，直接复用即可。
5. **thread_id**：`config={"configurable": {"thread_id": ...}}`。**把 `task_id` 映射为 `thread_id`**，
   即可天然实现多任务隔离与断点续跑。
6. **Send**（`from langgraph.types import Send`）：运行时才知道分支数量时的并行 fan-out。
   本项目章节数由 Planner 产出，正需要它。
7. **RetryPolicy**：`add_node("x", fn, retry_policy=RetryPolicy(max_attempts=3))`。
8. **recursion_limit**：`config` 里显式设置，是防止两节点互相循环跑飞的断路器。默认 1000 太高，建议设 50 左右。

### 2.5 替换的固有风险（必须先接受再动手）

- **会失去"自研引擎"这个简历亮点**。缓解：保留自研实现并通过配置开关切换，
  在 README 里写成"自研 → 迁移至 LangGraph 的演进与对比"，这本身就是一个有说服力的故事。
- **现有测试会失效**：`tests/test_dag.py`（75 行）与 `tests/test_scheduler.py`（214 行）测的是自研引擎行为，
  必须重写为针对 LangGraph 图的测试；`test_api.py`、`test_llm_cache.py`、`test_stage2.py` 也可能受影响。
  **目标是 139 个测试不减少**，改一个就要补一个。
- **历史评测基线不可直接对比**：`eval/results/` 下的 v3/v4/v6/v7-dedup/v8-budget 是旧编排跑出来的。
  替换后新结果要**另起目录**（如 `v9-langgraph`），不要覆盖旧数据，也不要拿新旧数据直接比。
- **`spans` 成本归因按 `node_id` 归因**，节点命名若变化需同步更新 `persistence/spans.py` 的消费方与前端展示。

---

## 3. 任务清单

---

### ~~T1【P0】用 LangGraph 替换自研 DAG 编排~~ ✅ 已完成（见 [STAGE5_ACCEPTANCE.md](STAGE5_ACCEPTANCE.md)）

**目标**
将 `backend/app/orchestrator/` 的自研 DAG 引擎替换为 LangGraph，保持现有行为等价：
拓扑结构不变、并行不变、重试不变、**断点续跑能力不退化**，且 139 个测试不减少。

**前置依赖**
- 无（可与 T2 并行）
- 需先安装 `langgraph` 与 `langgraph-checkpoint-sqlite`，并同步写入 `backend/requirements.txt`
  与 `backend/pyproject.toml` 的 `dependencies`

**必要背景**
- 现有拓扑见 `backend/app/orchestrator/dag.py` 顶部注释：
  `planner → research_ch1..N（全部并行）→ write_ch1 → review_ch1 → write_ch2 → ... → review_chN`
  （尾部串行是刻意设计：`serial_tail=True`，让第 N 章的 review 反馈约束第 N+1 章）
- 现有调度行为在 `scheduler.py`，四条容易写错、替换时必须保住的规则写在该文件开头 docstring：
  1. 阻塞传播要到不动点（否则下游节点永远停在 PENDING，任务看起来卡住却已经死了）
  2. 状态迁移带条件（保证同一 `(node_id, attempt)` 只执行一次——这是幂等与断点续跑的基础）
  3. 既无 ready 也无 running 时必须收敛退出，不能继续轮询
  4. Reviewer 打回不能加 DAG 回边（会成环），原实现靠"复用 attempt 机制"迂回实现
- 任务生命周期在 `runner.py`：`submit()` 建图入库，`run()` 调 `Scheduler.run()`，
  启动时会 `claim_orphans()` 把上个进程遗留的 RUNNING 节点重置

**交付物**
1. `backend/app/orchestrator/graph_langgraph.py`（新）：LangGraph 图构建，产出 compiled graph
   - State 用 `TypedDict`，含 `topic / sections / outline / chapters / review_round / errors` 等字段
   - 并行字段必须带 reducer（如 `Annotated[list, operator.add]`）
   - `planner` 后用 `Send` 动态 fan-out 到 `research_ch{i}`
   - `write_ch{i}` → `review_ch{i}` 用条件边实现打回循环（**这次可以直接用回边**）
   - 节点注册 `RetryPolicy(max_attempts=...)`
2. `backend/app/orchestrator/runner.py`（改）：`TaskRunner` 改为驱动 LangGraph
   - `task_id` → `thread_id` 映射
   - `submit()` 仍保持幂等（同 `task_id` 不重建）
   - 保留 `SqliteSaver` 作为 Checkpointer（复用现有 SQLite 基础设施）
3. `backend/app/orchestrator/scheduler.py`：**保留但停用**（见风险项），不删除
4. `backend/tests/test_graph_langgraph.py`（新）：替换 `test_dag.py` 与 `test_scheduler.py` 的覆盖
5. `docs/ARCHITECTURE.md` 补充一节：迁移前后的对比与取舍

**验收标准**
- `cd backend && python -m pytest -q` → **passed 数 ≥ 139**（不减少）
- `ruff check .` → All checks passed
- 行为等价验证（必须逐条实测，不能只看测试）：
  - 5 章任务仍是 `research` 并行、`write/review` 串行
  - 单节点失败会重试，重试耗尽后下游不再执行（阻塞传播）
  - **断点续跑**：在任务中途 kill 进程，重启后用同一 `task_id` 继续，已完成的节点不重跑
    （现有 `test_scheduler.py::test_crash_then_resume_does_not_rerun_done` 是这条的参考用例）
  - Reviewer 打回能形成"写-审-改"闭环
- 前端仍能从 SSE 看到进度（`api/app.py` 的轮询可先不动）
- `python -m app.cli run --profile real --topic "..." --sections 1` 端到端跑通

**风险与注意事项**
- **不要删除自研实现**：用配置项（如 `ORCHESTRATOR=langgraph|builtin`）保留回退开关，
  既降低迁移风险，也保留"演进对比"的简历素材。
- `contracts/nodes.py` 的 `NodeSpec`/`Graph` 被 `persistence` 与 `api` 消费，改动要向后兼容或同步更新消费方。
- LangGraph 的 state 是 JSON 序列化的：**不能把不可序列化的对象（如 retriever、gateway）放进 state**，
  这些应通过闭包或 `config` 传入节点，否则 Checkpointer 落盘会失败。这是最常见的坑。
- `recursion_limit` 必须显式设置（建议 50），否则打回循环可能跑飞。
- 成本归因 `spans.py` 依赖 `node_id`，节点命名尽量沿用 `research_ch1` 这类现有命名，避免前端与评测脚本失效。

---

### ~~T2【P0】混合检索：BM25 + 向量 + RRF 融合~~ ✅ 已完成（三档已可用；40 主题三档对比未重跑，见 [STAGE5_ACCEPTANCE.md](STAGE5_ACCEPTANCE.md)）

**目标**
在现有 BM25 关键词检索之上加入向量检索，做混合检索（RRF 融合），
并用**已有的 40 主题评测集**产出 BM25 / 纯向量 / 混合 三档的量化对照结论。

**前置依赖**
- 无（可与 T1 并行，分属 `rag/` 层与 `orchestrator/` 层）
- 需选定 embedding 来源（见下"方案选择"）

**必要背景**
- 检索契约在 `backend/app/contracts/retrieval.py`，是一个 `Protocol`：

  ```python
  class Retriever(Protocol):
      async def search(self, query, *, top_k=4, chapters_hint="") -> list[Snippet]: ...
      def corpus_size(self) -> int: ...
      def full_text(self, doc_id: str) -> str: ...
      def doc_ids(self) -> list[str]: ...
  ```

  `Snippet` 字段：`doc_id / title / page / text / score`。
  **新增实现必须完整实现这四个方法**，这样上层（Researcher、checkup、api）零改动。
- 现有 BM25 实现在 `backend/app/rag/corpus.py`（`LocalCorpusRetriever`）。
  该项目刻意在 `contracts/retrieval.py` 定义了端口而非直接依赖 ChromaDB，
  注释里已写明"后续换向量检索只需换实现"——**这次正是这个设计预留的扩展点**。
- 知识库是 `data/docs/` 下的 `.md`/`.txt`，由 `rag/store.py` 的 `CorpusStore` 管理
  （上传、重名另存、空库自动用 `sample-corpus/` 兜底）。
- 该项目选择 BM25 而非向量的**原始理由**写在 README「技术选型」：目标是"引用可溯源"而非召回率，
  且零依赖、结果可复现、不因 embedding 升级导致评测基线漂移。
  **本次改动必须回应这个理由**：不能牺牲可复现性，也不得破坏溯源约束。

**方案选择（执行时二选一，推荐 A）**

- **方案 A（推荐）**：embedding 走 OpenAI 兼容的 `/embeddings` HTTP 端点（如硅基流动的 BGE 系列），
  向量存 `sqlite-vec`。
  优点：与项目"只用 OpenAI 兼容 HTTP 接口"的哲学一致，且**可直接复用 `app/llm/cache.py` 的录制回放机制**，
  保证 embedding 调用也能离线复现、评测基线不漂移。
- **方案 B**：本地 `BAAI/bge-small-zh-v1.5` + `faiss-cpu`，完全离线。
  优点：不依赖外部服务；缺点：需下载约 100MB 模型，且要在依赖里引入新包。

**交付物**
1. `backend/app/rag/embedding.py`（新）：embedding 客户端，
   **必须接入 `app/llm/cache.py` 的录制回放**，保证离线可复现与 ¥0
2. `backend/app/rag/vector_index.py`（新）：向量索引构建与查询（含持久化路径）
3. `backend/app/rag/hybrid.py`（新）：实现 `Retriever` Protocol 的混合检索器，
   BM25 与向量各自召回后用 **RRF（Reciprocal Rank Fusion，k=60）** 融合
4. `backend/app/rag/corpus.py`（改）：保留 `LocalCorpusRetriever`，
   新增 `build_retriever(mode=...)` 工厂，支持 `bm25 | vector | hybrid` 三档切换
5. `.env.example` 与 `backend/app/core/config.py`：新增配置项
   `RETRIEVAL_MODE`（默认 `bm25`，保持向后兼容）、`EMBEDDING_MODEL`、`EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`
6. `backend/tests/test_hybrid.py`（新）：RRF 融合正确性、三档切换、空库兜底
7. 评测结果：新目录 `eval/results/v9-hybrid/`，含三档对比表

**验收标准**
- `cd backend && python -m pytest -q` → passed 数 ≥ 139（新增测试后应更多）
- `ruff check .` → All checks passed
- **引用溯源不破坏（最高优先级）**：`Snippet.text` 必须是原文的**逐字切片**，
  不得由模型生成或改写；`checkup` 的断言核对仍能通过。
  这一条要专门写测试验证。
- 三档可切换且默认 `bm25`（不破坏现有行为与历史评测基线）
- 产出量化结论：`eval/results/v9-hybrid/` 下给出三档在 40 主题上的覆盖率/引用准确率对比
  + 95% 置信区间（复用 `app/evaluation/stats.py` 的配对比较）
- **离线可复现**：`LLM_MODE=replay` 下 embedding 也走缓存，不联网也能跑通评测

**风险与注意事项**
- **可复现性是本项目的立身之本**：embedding 调用一旦绕过录制回放，历史评测基线就失效了。
  这是本次最大的技术风险，务必先确认缓存机制覆盖了 embedding 路径。
- 向量检索召回的是 chunk，**chunk 切分策略要保证 `quote` 能逐字命中原文**。
  建议切片按段落/句子边界而非固定字数硬切，并在切片时保留 `doc_id` 与 `page`。
- 语料只有 22 篇（当前 `data/docs/`），规模很小，向量检索的召回优势可能不明显甚至更差——
  **这是正常的，不要为了"证明混合检索更好"而调整评测口径**。
  如实报告结论即可，"在你的语料规模下 BM25 仍更优"同样是有价值的工程结论
  （参考项目已有的 `docs/REPEAT_RATE.md`，那个文档就是记录了一次"优化无效"的诚实结论）。
- 新增依赖必须同时更新 `requirements.txt` 与 `pyproject.toml`，否则 CI 会红（CI 只装 requirements-dev.txt）。
- 向量索引文件要加进 `.gitignore`（类似 `data/` 运行时产物的处理）。

---

### T3【P1】Function Calling / 联网检索工具 ⬜ 未开始（前置 T1 已完成，可开工）

**目标**
让 Agent 具备工具调用能力，并新增一个联网检索工具，
顺带解决项目已有的已知边界"不具备实时联网检索"（见 README「已知边界」）。

**前置依赖**
- **依赖 T1 完成**：工具调用需要 Agent 节点支持"模型决定调工具 → 执行 → 回填结果 → 再调模型"的循环，
  这个循环由 T1 的 LangGraph 图提供
- T2 无依赖（但两者都在 `rag/`、`agents/` 层改动，建议串行以免冲突）

**必要背景**
- LLM 网关在 `backend/app/llm/`：`gateway.py`（分级、超时降级、计量）、
  `openai_compat.py`（OpenAI 兼容 HTTP 请求构造）、`cache.py`（录制回放）。
  **新增 tools 参数需要改这三个文件**，否则录制回放的缓存 key 会漏掉 tools 字段，
  导致"回放命中了错误的响应"。
- Agent 基类在 `backend/app/agents/base.py`（`JsonAgent`），四个 Agent 继承它。
- Researcher 的输入输出契约在 `contracts/handoff.py` 与 `contracts/agent.py`。
- 项目已有"引用必须能溯源"的硬约束：**联网检索回来的内容同样必须满足 `quote` 可逐字命中**，
  否则不能作为素材（这一条对联网内容尤其重要，因为网页内容会变）。

**交付物**
1. `backend/app/llm/openai_compat.py`（改）：请求体支持 `tools` 与 `tool_choice`
2. `backend/app/llm/cache.py`（改）：缓存 key 纳入 tools 字段
3. `backend/app/tools/`（新）：
   - `base.py`：工具定义与注册
   - `web_search.py`：联网检索工具（建议用可替换的 provider，默认关闭）
4. `backend/app/agents/researcher.py`（改）：支持工具调用循环
5. 配置：`.env.example` 与 `config.py` 新增 `ENABLE_WEB_SEARCH`（**默认 false**）与相关 key
6. `backend/tests/test_tools.py`（新）：工具注册、缓存 key 含 tools、关闭时不联网

**验收标准**
- `cd backend && python -m pytest -q` → passed 数 ≥ 139
- `ruff check .` → All checks passed
- **默认关闭时完全不联网**：`ENABLE_WEB_SEARCH=false` 下跑任务，不应产生任何外网请求
  （可用 `python -m app.cli run` 在未配置 key 时跑通来验证）
- 工具调用能被录制回放正确缓存（同一请求第二次命中缓存且结果一致）
- 联网素材同样满足溯源约束：`quote` 能在抓取内容中逐字找到，否则丢弃
- 未配置检索 key 时优雅降级为纯本地检索，不抛异常

**风险与注意事项**
- **最大的风险是破坏评测可复现性**：联网内容随时间变化，一旦默认开启，
  40 主题评测集的结果就不再稳定。因此**必须默认关闭**，且评测脚本要明确声明不启用联网。
- 工具调用的缓存 key 若漏掉 `tools` 字段，会出现"参数不同却命中同一条缓存"的隐蔽错误，
  这个 bug 极难排查，必须专门写测试。
- 网页抓取要设超时与大小上限，避免单篇过大拖垮节点（参考 `rag/store.py` 里 `MAX_FILE_BYTES = 2MB` 的做法）。
- 不要在工具里引入重型依赖（如 playwright/selenium）；用一个轻量 HTTP + HTML 文本抽取即可。
- 若 T1 未完成就先做 T3，会导致工具循环无处安放——**严格遵守依赖顺序**。

---

## 4. 完成后的收尾（不属于任务范围，仅提醒）

- 每完成一个任务，更新 `CHANGELOG.md` 的 `Unreleased` 段
- 每个任务单独 commit（Conventional Commits 规范，见 `CONTRIBUTING.md`），便于回滚
- 每次 push 后确认 GitHub Actions 全绿（仓库：`https://github.com/TheQing1/reportflow`）
- 若评测口径发生变化，同步更新 `docs/STAGE3_ACCEPTANCE.md`
