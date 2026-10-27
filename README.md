# ReportFlow

> 输入一个行业主题，自动生成一份**每个数据都能追溯到原文**的研报初稿。

[![CI](https://github.com/TheQing1/reportflow/actions/workflows/ci.yml/badge.svg)](https://github.com/TheQing1/reportflow/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green)
![Vue](https://img.shields.io/badge/Vue-3.4+-brightgreen)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

做案头研究的痛点是重复劳动：翻 20 篇报告 → 摘到 Word → 标引用 → 拼成文，通常要 1–3 天。

用通用大模型直接写，又有两个致命问题：**数据是编的，引用是假的**。

ReportFlow 解决的是这两件事——让生成过程**分阶段可控**，让每一句结论**都能回溯到知识库里的具体文档**。

它产出的不是成品，是一份**能直接改的初稿**：省掉查资料和搭框架的时间，把精力留给判断和观点。

### 和搜索类 AI 有什么不一样

搜索型 AI 解决的是「回答一个问题」，输出一段短文本加一组链接。ReportFlow 解决的是「产出一份有结构的长文档」：

| | 搜索类 AI | ReportFlow |
|---|---|---|
| 输出 | 一段回答 | 分章节长文 |
| 引用 | 链接列表 | 每个断言绑定到具体文档 |
| 质量 | 一次性生成 | 写完要核对，不合格退回改 |
| 成本 | 不可见 | 能说清钱花在哪个环节 |

一句话：**它是流水线，不是问答。**

---

## 输出长什么样

```markdown
# 2026 年中国工商业储能行业概览

## 1. 市场规模与增长

2025 年工商业储能新增装机约 8.4GWh，同比增长 62%[^doc_05_market_cig]，
其中浙江、广东、江苏三省合计占比 55% 以上[^doc_07_region_cig]……
```

每个 `[^doc_xx]` 都能点开看到对应文档的原文。发布前还能做一次体检，指出哪条断言在它所引的原文里找不到，并提示数据实际出自哪篇。

---

## 界面一览

**执行视图**：DAG 进度、按节点归因的成本表、每个节点的状态与耗时：

![执行视图：DAG 进度与成本归因](docs/assets/overview.png)

**引用溯源抽屉**：点正文里任意一个角标，右侧打开原文，命中片段高亮——你可以逐条核对「这句话真的出自那里」：

![引用溯源抽屉：原文命中片段高亮](docs/assets/citation-drawer.png)

**素材清单**：这一章到底用了哪些文档、哪些原文片段、丢弃了多少条不可核验的素材：

![溯源面板：每章素材与丢弃计数](docs/assets/trace.png)

> 支持深度链接：`?doc=<doc_id>` 直接打开某篇原文，`?tab=trace|check|corpus` 直达对应面板。把某条引用的原文链接发给同事核对时，对方打开就是高亮后的那一篇。

---

## 核心特性

**多 Agent 流水线，而非一次性生成**
Planner 拆大纲 → Researcher 并行检索 → Writer 分章撰写 → Reviewer 交叉校验。每个环节职责单一，可独立替换、独立评测。

**引用必须能溯源，否则丢弃**
Researcher 产出的每条素材，其 `quote` 必须能在原文中逐字找到，找不到就丢弃；Writer 的角标映射不到素材就直接剥掉；页码由程序按素材重建，不采信模型输出。

**程序校验优先于模型判断**
Reviewer 先用程序核对「断言里的数字是否出现在所引原文」，命中即判定，不调模型；只有语义层面的问题才交给 LLM。又快又准又免费的事，不该花钱让模型猜。

**Agent 能自主决定「还要不要查」**
Researcher 具备 Function Calling 能力：先查本地知识库，材料不够时**自己决定**换个说法补查、或转去联网检索，而不是被动接受一次性检索结果。
工具是可选能力（`ENABLE_TOOLS`，默认关）：关掉时请求形状与之前完全一致，评测基线不受影响；开着时新增的两个工具——`kb_search` 与 `web_search`——都受同一条溯源约束：**返回的是原文，模型必须逐字引用**，否则素材照样被丢弃。
默认关闭联网（`ENABLE_WEB_SEARCH=false`）不是保守，而是正确性要求：联网内容会变，一开 40 主题评测就不可复现。

**图编排，支持并行与断点续跑**
整条流水线是一张 LangGraph `StateGraph`：`Send` 按章节数在运行时动态扇出并行、barrier 边做真汇合、条件回边构成写-审-改闭环、Checkpointer 按 `thread_id` 落盘。进程重启后从断点继续，已完成的节点不重跑；节点级失败按 `RetryPolicy` 重试，确定性失败（如回放缓存缺失）不重试。

**全链路成本可归因**
每次调用的 token / 延迟 / 成本落入 spans 表，能回答「这份研报的钱花在哪个环节」，并按节点归因。

**效果有数据，不是感觉良好**
内置 40 个主题的标注评测集，横向对比「单 Agent 直出 / 多 Agent / 多 Agent + Reviewer」三档，配对比较 + 95% 置信区间。结论见[效果与代价](#效果与代价)。

---

## 快速开始

**方式 A · Docker（不需要本地装 Python / Node）**

```bash
cp .env.example .env      # 填 LLM_API_KEY
docker compose up --build # 后端 8000 + 前端 5173
```

**方式 B · 本地**

```bash
# 1) 装依赖
cd backend  && pip install -r requirements.txt
cd ../frontend && npm install

# 2) 配 key（也可不填，见下）
cp .env.example .env      # 填 LLM_API_KEY

# 3) 一键启动：后端 8000 + 前端 5173
./scripts/start.sh          # Git Bash / WSL
scripts\start.bat           # Windows cmd / PowerShell
make dev                    # 同上（Linux / macOS）
```

打开 http://localhost:5173 提交任务、看 DAG 进度、点角标看原文。

常用命令见 `make help`：测试 / lint / 切语料 / 起容器都在里面。

质量门禁（CI 每次推送都跑，本地 `make lint` / `make test` 等价）：

| 检查 | 命令 | 覆盖范围 |
|---|---|---|
| 测试 | `pytest -q` | **323 条**，全离线（fake/scripted LLM，不联网、无需 key） |
| Lint | `ruff check .` | 全仓库 |
| 格式 | `ruff format --check .` | 全仓库（不只 lint，排版也锁住） |
| 类型 | `mypy app/contracts app/core` | 域模型与配置两层开 `strict`，其余层刻意不严 |
| 构建 | `npm run build` + `docker build` | 前端产物与两个镜像 |
| 架构 | `tests/test_layering.py` | 用 AST 强制分层依赖方向，越界即失败 |

**没有 key 也能跑开发**：默认 `LLM_MODE=auto`，第一次真调后写入 `data/llm_cache/`，之后命中缓存——毫秒级、结果逐字一致、成本 ¥0。要验收真实生成效果时传 `--live`。

命令行同样可用（不启前端）：

```bash
cd backend
python -m app.cli doctor                                   # 自检：key 可用？知识库空不空？
python -m app.cli run --profile real \
    --topic "2026年中国新型储能行业" --sections 5            # 建任务并跑
python -m app.cli export --task-id <id> --check            # 导出 + 发布前体检
python -m app.cli cost   --task-id <id>                    # 钱花在哪个节点
python -m app.cli corpus ls                                # 知识库有哪些文档
```

### 知识库

| 语料 | 篇数 | 性质 | 用途 |
|---|---|---|---|
| `data/docs/`（运行时，**评测基线**） | 22 | 虚构数据，由脚本生成 | **40 主题评测集就是按这套标注的**，跑评测必须用它 |
| `real-corpus/` | 29 | 真实公开数据，可溯源 | 体验真实效果 |
| `sample-corpus/` | 12 | 虚构数据，分目录组织 | 早期功能验证用，与评测集**不对应** |

```bash
python -m scripts.seed_corpus --clean       # 生成评测基线语料（22 篇，字节可复现）
python -m app.cli corpus init --real        # 装真实语料（替换，不是叠加）
```

> ⚠️ `data/docs/` 是运行时目录、不进版本库，所以**新 clone 下来必须先跑 `seed_corpus`**，
> 否则知识库为空（会用 `sample-corpus` 兜底，而那套的 `doc_id` 与评测集 `must_cite` 对不上，
> 评测结果没有意义）。语料由 `scripts/corpus_data.py` 单一事实源生成，
> 同一指标全库只有一个数，重跑得到的文件字节一致。

也可以直接把自己的 `.md` / `.txt` 放进 `data/docs/`，或在界面「知识库」页拖拽上传（同名文件不覆盖，另存为 `_2`）。

> 切换语料是**替换**而非叠加——两套语料文件名不同，叠加会让虚构与真实数据混进同一个库，那样生成的报告真假难辨。

---

## 架构

```mermaid
flowchart TB
    subgraph client["前端"]
        UI["Vue3 + Vite<br/>任务看板 · 角标溯源 · 体检面板"]
    end

    subgraph api["接入层"]
        FA["FastAPI<br/>REST + SSE 流式推送"]
    end

    subgraph orch["编排层 · LangGraph StateGraph"]
        PL["planner 节点<br/>拆解大纲"]
        SCH["Send 动态扇出 + barrier 汇合<br/>research 并行 · write/review 串行"]
        SM["Checkpointer + RetryPolicy<br/>thread_id 断点续跑 · 节点级重试"]
    end

    subgraph agents["Agent 层"]
        RE["Researcher<br/>检索 + 素材包"]
        WR["Writer<br/>分章撰写"]
        RV["Reviewer<br/>引用一致性校验（默认不在写作循环内）"]
    end

    subgraph support["支撑层"]
        GW["LLM 网关<br/>模型分级 · 超时降级 · 计费"]
        TR["Trace<br/>token · 延迟 · 成本"]
    end

    subgraph data["数据层"]
        DB[("SQLite / MySQL<br/>tasks · nodes · spans")]
        KB[("本地语料<br/>三档检索：bm25 默认 / vector / hybrid")]
    end

    UI --> FA
    FA --> PL
    PL --> SCH
    SCH --> RE
    SCH --> WR
    SCH --> RV
    SCH -. 每步状态写回 .-> SM
    RE --> KB
    RE --> GW
    WR --> GW
    RV --> GW
    GW --> TR
    TR --> DB
    SM --> DB
```

### 一次任务的时序

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant A as API
    participant S as LangGraph 图
    participant R as Researcher
    participant W as Writer

    U->>A: POST /tasks { topic, sections }
    A->>S: 创建任务，落库
    A-->>U: task_id + SSE 连接

    S->>S: planner 节点生成大纲
    S-->>U: SSE: 大纲已生成（5 章）

    par 并行检索
        S->>R: 检索第 1 章素材
        S->>R: 检索第 2 章素材
    end
    R-->>S: 素材包（key_facts + 原文片段）

    S->>W: 撰写第 1 章
    W-->>S: 正文（带引用角标）

    S-->>U: SSE: 任务完成，返回研报 ID
```

### 分层与依赖方向

```
contracts  ←  core  ←  {persistence, agents, rag, llm, evaluation}  ←  orchestrator  ←  {assembler, cli, api}
```

`contracts/` 是叶子层：定义跨层契约，零内部依赖。所有实现只依赖它，彼此不互相依赖。

这条约束由 `tests/test_layering.py` 用 AST 强制校验——任何一层越界引用，测试直接失败。靠自觉守不住的约束，交给机器。

### 代码在哪

| 目录 | 内容 |
|---|---|
| `backend/app/contracts/` | 跨层契约（叶子层，零内部依赖） |
| `backend/app/orchestrator/` | LangGraph 图：State、节点、条件边、Checkpointer |
| `backend/app/agents/` | 四个 Agent + MockAgent（可切换做回归） |
| `backend/app/llm/` | 网关：分级 / 降级 / 计量，以及录制回放 |
| `backend/app/rag/` | 本地语料检索与读写 |
| `backend/app/evaluation/` | 评测：数据集、judge、语义审计、统计 |
| `backend/app/api/` | REST + SSE |
| `backend/scripts/` | 造语料、抓真实语料、造评测集、离线假 LLM |
| `frontend/src/` | Vue3 界面 |
| `eval/` | 40 个标注主题 + 评测入口（`run_eval.py`） |
| `sample-corpus/` `real-corpus/` | 自带的两套语料 |

---

## 技术选型

| 层 | 选型 | 为什么 |
|---|---|---|
| 编排 | **LangGraph `StateGraph`** | 原生就有本项目真正需要的三件事：循环（写-审-改回边）、运行时才知道分支数的并行（`Send`）、断点续跑（Checkpointer）。生态与团队可维护性也明显好于自研。**迁移前是自研 DAG 引擎**（建图 + 调度约 470 行），换框架的动机与代价写在 [STAGE5_ACCEPTANCE.md](docs/STAGE5_ACCEPTANCE.md)，旧实现见 git 历史 |
| 后端 | FastAPI | 原生 async，SSE 实现简单，与 asyncio 调度天然契合 |
| 存储 | SQLite（开发）/ MySQL（生产） | 状态唯一事实来源是数据库；SQLite 让本地零运维跑通，切 MySQL 只改 `DATABASE_URL` |
| 检索 | **三档：bm25（默认）/ vector / hybrid** | 目标是「引用可溯源」而非召回率。默认仍是 BM25：零依赖、结果可复现，也不会因 embedding 升级导致评测基线漂移；向量档是补召回，不是替换。`hybrid` 用 RRF 按**名次**融合两路，不做归一化加权 |
| 工具调用 | **原生 Function Calling（OpenAI tools 协议）** | 不引 LangChain Agent / 插件体系：本项目的工具只有两个（查本地、查网页），自建 `Tool` 协议 + 一个循环就够了，多一层抽象只是多一处要理解的东西。代价是循环要自己写（轮次上限、失败回灌、证据池合并），好处是每一处行为都可控、可单测 |
| 联网检索 | **自带 provider + 搜索索引摘要** | 不引搜索 SDK / 爬虫框架。实测主流搜索源的跳转链接带反爬（单独请求 400），所以直接用结果页的 `data-mdurl` + 摘要，比逐页抓取更稳也更快。代价是拿不到整页正文，详见「已知边界」 |
| 前端 | Vue3 + Vite | **没有用图布局库**：图形状固定（planner → 并行 research → 串行 write），三行自己算就够 |
| 可观测 | **自建 Trace** | 只需要 token / 延迟 / 成本三个维度，上 OTel 属于过度设计 |
| 部署 | `scripts/start.sh` / `start.bat` | 一条命令起后端 + 前端。当前是单进程 + SQLite 的本地服务，容器化收益不抵复杂度 |
| 开发期成本 | **LLM 录制/回放** | 改前端/改图结构时会反复跑同一批 prompt。录一次之后全命中缓存：毫秒级、结果确定、¥0 |

刻意不引入：OpenAI SDK（只用 OpenAI 兼容的 HTTP 接口）、向量数据库（22 篇 / 79 片段，纯 Python 点积够用，且自己写才能把「每条向量对应哪段原文」显式存下来）、分词库。
少一个依赖，就少一处版本漂移导致的评测不可复现。

例外只有编排层：`langgraph` 是显式引入的框架依赖。它只用到 `langchain-core` 的基础抽象，
不引入 LangChain 的链，也不替换 `app/llm/` 的网关——检索与 LLM 调用仍然是我们自己的代码。

---

## HTTP 接口

| 接口 | 用途 |
|---|---|
| `POST /api/tasks` | 建任务（`{topic, sections, task_id?, review?}`） |
| `POST /api/tasks/{id}/run` | 后台执行（幂等，已完成的节点不重跑） |
| `GET  /api/tasks/{id}/stream` | SSE：`task` / `node` / `done` 三类事件 |
| `GET  /api/tasks/{id}/export` | 拼好的 markdown |
| `GET  /api/tasks/{id}/trace` | 每章真实用到的素材（溯源面板的数据） |
| `GET  /api/tasks/{id}/check` | 发布前体检结果 |
| `GET  /api/tasks/{id}/cost` | 成本归因（总计 + 按节点 + 回放命中数） |
| `GET  /api/corpus` | 知识库有哪些文档 |
| `POST /api/corpus/upload` | 上传 .md/.txt，立即重新索引 |
| `DELETE /api/corpus/{doc_id}` | 删除一篇 |
| `GET  /api/corpus/{doc_id}` | 一篇文档的全文 |

SSE 用**轮询数据库**而不是给编排层加回调：图完全不知道 HTTP 的存在，崩溃重启后前端连上就能继续看到进度，不需要任何「事件重放」。代价是 300ms 粒度。

---

## 效果与代价

40 个标注主题，三档方案对比（配对比较，同主题内比较以抵消主题难度差异）。
下表是**当前 LangGraph 编排**的基线（`eval/results/v9-langgraph/`，40 主题 × 1 次重复，真实模型）：

| 方案 | 要点覆盖率（95% CI） | 语义支持率 | 不支持断言 | 必引命中 | 单份成本 |
|---|---|---|---|---|---|
| A · 单 Agent 直出 | 73.2% [65.9, 80.6] | 93.3% | **14 条** | 84.1% | ¥0.0056 |
| B · 多 Agent 协作 | 77.5% [69.6, 85.5] | 98.2% | **1 条** | 86.9% | ¥0.0204 |
| C · 多 Agent + Reviewer | **79.3%** [71.7, 87.0] | **99.8%** | **0 条** | 92.7% | ¥0.0272 |

**配对比较（本轮）**

| 对比 | 要点覆盖率 | 语义支持率 |
|---|---|---|
| A → B | +4.3pp [−4.7, +13.4]　不显著 | **+4.9pp [+1.5, +8.2]　显著** |
| A → C | +6.1pp [−1.5, +13.7]　不显著 | **+6.4pp [+3.6, +9.3]　显著** |
| B → C | +1.8pp [−4.1, +7.6]　不显著 | **+1.6pp [+0.2, +2.9]　显著** |

读这张表要注意三件事，否则会得出错误结论：

- **收益的实质是「引用归属正确」，不是「字数更多」**。单 Agent 那 14 条不支持断言几乎全是
  「用了 A 文档的数据、角标挂在 B 文档」——数据是真的，出处是错的。分章检索让角标只能落在
  本章真实用到的来源上，所以**语义支持率**（+4.9pp，显著）才是这个架构的核心收益。
- **覆盖率提升在本轮不显著**（+4.3pp，CI 跨 0）。改造前的基线曾测得 +7.7pp 显著
  （v6），本次重跑没有复现出来。以现有样本量不能声称「多 Agent 覆盖率更高」——
  如实写在这里，而不是挑一个好看的数字。
- **Reviewer 仍在默认流水线之外**。本轮 C 档语义支持率最高（99.8%，B→C 显著 +1.6pp），
  但覆盖率与 B 无显著差异，成本再高 33%。所以它继续作为导出时的一次性体检（`cli export --check`）
  而非每章必跑——按需使用，而不是默认开启。

**与改造前的对照**（同一套评测集、同一判官，可直接比）：

| 方案 | 覆盖率 v6（自研） | 覆盖率 v9（LangGraph） | 语义支持 v6 | 语义支持 v9 |
|---|---|---|---|---|
| A | 72.6% | 73.2% | 92.2% | 93.3% |
| B | 80.2% | 77.5% | 99.7% | 98.2% |
| C | 80.5% | 79.3% | 98.9% | 99.8% |

三档的绝对值都在同一量级（覆盖率差 1~3pp，语义支持差 1pp 内），
**换编排框架没有让质量出现数量级变化**——这正是"行为等价迁移"想要的结果。
逐主题看，A 档 39/40 完全一致，B/C 档各有约 1/3 主题上下浮动，属采样波动范围。

> 判官与生成模型分离（生成 DeepSeek / 判官 `zai-org/GLM-5.3-Flash`，跨厂商），
> 避免「自己给自己判卷」。但**不含人工校准**，绝对分数可能偏乐观——请看档间相对差异，别只看绝对值。

> 评测方法、judge 校准、以及「为什么覆盖率一度显示不显著」的完整复盘见
> [docs/STAGE3_ACCEPTANCE.md](docs/STAGE3_ACCEPTANCE.md)；
> 本轮重跑与迁移验收见 [docs/STAGE5_ACCEPTANCE.md](docs/STAGE5_ACCEPTANCE.md)。

---

## 已知边界

- 产出定位是**初稿**，还达不到可直接发布的水平
- 只支持中文；知识库需预先灌入
- **联网检索能力存在但默认关闭**（`ENABLE_TOOLS` + `ENABLE_WEB_SEARCH`，默认均 `false`）。
  开着时 Researcher 会先查本地、不够再联网；关着时行为与之前完全一致。
  默认关闭是刻意的：联网内容随时间变化，一开 40 主题评测就不再可复现。
  另外联网档的**素材质量依赖搜索源**——实测能拿到正规媒体的正文摘要，
  但也可能搜到文库站的目录页（只有页数/上传日期、没有实质内容），
  此时模型会如实报告"素材不足"，而不是硬编
- 联网结果用的是**搜索索引提供的摘要**（约 80~150 字/条），不是整页正文。
  对「抽一句话做引用」够用，对「通读全文做综述」不够——这是有意识的取舍：
  实测主流搜索源的跳转链接带反爬（单独请求返回 400），逐页抓取不可靠
- **覆盖率指标有天花板，且部分主题样本量不足**：40 主题 × 1 次重复，单主题覆盖率波动较大
  （同一主题不同次的差异可达 20pp+）。因此只看**配对比较的置信区间**，不要看单个主题的分数；
  「多 Agent 覆盖率更高」这条在本轮**不显著**（见「效果与代价」），不要当成定论引用
- 检索的 `vector` / `hybrid` 两档没有专属界面：切档靠 `.env` 的 `RETRIEVAL_MODE`，界面顶部只显示**实际生效**的档位（含降级标记与 embedding 模型），不显示「这条片段由哪一路召回」；这两档也**没有做 40 主题量化对照**，只有单主题实测
- **章节重复率约 8%**（跨章重复的论述片段占比）。根因不是算法：它由「每主题可用文档数」驱动——真实语料实验中该值从 8 提到 11 篇，重复率相应从 13.3% 降到 7.7%。解决它要靠补语料密度；两次专门优化（prompt 层去重、检索层文档预算）都无效甚至有害，已默认关闭。详见 [docs/REPEAT_RATE.md](docs/REPEAT_RATE.md)
- 评测判官跑在 `reasoning_effort=low`（为速度让步），绝对分数可能偏乐观
- 单实例假设：多副本部署需自行按 `task_id` 加分布式锁

---

## 文档

| 文档 | 看什么 |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | 设计决策与取舍：为什么这么做，放弃了什么 |
| [STRUCTURE_REVIEW.md](docs/STRUCTURE_REVIEW.md) | 结构审查：目录、分层、依赖方向的问题与修正 |
| [STAGE1_ACCEPTANCE.md](docs/STAGE1_ACCEPTANCE.md) | 自研编排引擎验收（历史记录）：并行/串行、阻塞传播、断点续跑 |
| [STAGE2_ACCEPTANCE.md](docs/STAGE2_ACCEPTANCE.md) | 四个 Agent 与网关验收 |
| [STAGE3_ACCEPTANCE.md](docs/STAGE3_ACCEPTANCE.md) | 评测方法论、三档对比、失败复盘 |
| [STAGE4_ACCEPTANCE.md](docs/STAGE4_ACCEPTANCE.md) | 接口与前端验收 |
| [STAGE5_ACCEPTANCE.md](docs/STAGE5_ACCEPTANCE.md) | 编排迁到 LangGraph + 三档检索：迁移映射、六个踩坑、40 主题重跑数据、未完成项 |
| [REPEAT_RATE.md](docs/REPEAT_RATE.md) | 章节重复率：两次失败与真实语料上的因果验证 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发环境、分层约束、测试与 commit 规范 |
| [CHANGELOG.md](CHANGELOG.md) | 版本演进，以及每次改动背后的理由 |

---

## License

MIT — 见 [LICENSE](LICENSE)。

`real-corpus/` 下的语料正文版权归原作者所有（国家能源局、CNESA、人民日报、经济日报、证券时报、央广网等公开来源），MIT 仅覆盖本仓库的代码与文档。
