# 架构设计

本文记录 ReportFlow 的设计决策与取舍。README 讲「是什么」，这里讲「为什么这样做」。

---

## 1. 设计目标与非目标

**目标**

1. 一份研报的每个事实性断言都能追溯到知识库中的具体文档和页码
2. 生成过程可观测：能看清哪个环节慢、哪个环节贵、哪一章被打回
3. 失败可恢复：不因为一次 API 超时就丢掉整篇已生成的内容
4. 效果可量化：能用数据回答「多 Agent 是否值得这个成本」

**非目标**

1. 不追求生成可发布的成品，定位是初稿
2. 不做通用 Agent 框架，只服务「长文档生成」这一条流水线
3. 不做实时联网检索，知识源固定在本地知识库

明确非目标比列目标更重要——它决定了哪些功能**不做**。

---

## 2. 分层职责

| 层 | 职责 | 不做什么 |
|---|---|---|
| 契约层 | 节点/状态/交接协议/存储端口的定义 | 不含任何实现与 IO |
| 接入层 | 接收任务、推送进度、鉴权 | 不含任何业务逻辑 |
| 编排层 | 拆解图、驱动执行、管理状态与断点 | 不关心 Agent 内部怎么实现 |
| Agent 层 | 单一职责的生成/校验动作 | 不感知调度、不互相直接调用 |
| 支撑层 | LLM 调用、上下文交接、成本归集 | 不含业务语义 |
| 数据层 | 状态、产出、向量的持久化 | — |

关键约束：**Agent 之间不直接调用**。所有协作通过编排层调度 + 交接协议完成。这样任何一个 Agent 可以独立替换、独立测试，也能避免 Agent 互相调用形成的隐式循环依赖。

依赖方向只能是单向的，且由 `backend/tests/test_layering.py` 用 AST 扫描强制执行：

```
contracts  ←  core  ←  {persistence, agents, llm, rag}  ←  orchestrator  ←  assembler / cli
```

注意两处细节，否则会退化成双向依赖：

1. **契约必须放在叶子层**。如果 `NodeSpec` 定义在 `orchestrator/`，Agent 读它就要 import 编排层，
   而编排层又要调用 Agent —— 环就闭合了，Mock 与真实 Agent 也不可能做到"调度层零改动"。
2. **编排层只认端口不认实现**。`TaskStore` / `Agent` 都是协议定义在契约层，
   具体实现（SQLAlchemy、Mock、真实 LLM）在 `app/assembler.py` 这唯一一处完成绑定。

结构审查的完整结论见 [STRUCTURE_REVIEW.md](STRUCTURE_REVIEW.md)。

---

## 3. 编排层：LangGraph 图

> **迁移说明（先说清现状，免得下面读成"一直如此"）。**
> 编排层原本是自研的 DAG 引擎（`orchestrator/dag.py` 建图 + `orchestrator/scheduler.py` 调度循环，
> 两个文件合计约 540 行，其中核心逻辑约 470 行）。后来整体迁到 LangGraph `StateGraph`，
> 自研实现已删除，旧代码见 git 历史。
> 迁移**不是**因为自研写不动：那套引擎跑过了阶段一~四的验收，行为是对的。
> 换的真实理由有三条，按重要性排序：
>
> 1. **图原语对上了问题形状**。这个流水线需要三件事：写-审-改的**环**、章节数到运行时才知道的
>    **动态并行**、以及进程崩溃后的**断点续跑**。自研引擎为了第一条只能靠"重置节点 + attempt+1"绕开环
>    （见 §3.5），而后两条要自己维护整套状态机与持久化。LangGraph 三件都是原语。
> 2. **可维护性与招人成本**。图、状态迁移、重试、checkpoint 由框架维护，新同事看的是官方文档而不是
>    我们那 470 行的调度循环。
> 3. **生态**。`astream_events`、Postgres/Redis saver 这类能力，自研要重做一遍。
>
> 代价也明确记下来：多了一个不能随手改的依赖（版本漂移会让评测基线不可复现，所以 `requirements.txt`
> 锁定大版本）、框架的隐式语义要靠踩坑才认得（真踩到的几个坑见
> [STAGE5_ACCEPTANCE.md](STAGE5_ACCEPTANCE.md)）。**"用框架"没有让编排变简单，只是把复杂度从
> "自己写的状态机"换成了"必须理解对的图语义"。**

### 3.0 迁完之后，编排层只剩三个部件

迁完之后，编排层只有三件事，边界比自研时代更清楚：

| 文件 | 管什么 | 不管什么 |
|---|---|---|
| `orchestrator/graph_langgraph.py` | 节点清单（落库契约）+ `StateGraph` 的节点/边/汇合/回边 | 不碰任务生命周期，不碰 checkpoint 装配 |
| `orchestrator/runner.py` | `submit` / `run`、`task_id → thread_id`、孤儿回收、终态汇总 | 不写图结构 |
| `persistence/store.py`（`TaskStore`） | 节点状态、产出、attempt、review_round 的**唯一事实来源** | 不感知 LangGraph |

三条"必须靠对的图原语"决定了这张图长什么样，写在这里是因为它们都属于**看起来能跑、实际会静默错**的那类：

- **汇合要用 barrier 边，不是普通边**。`add_edge([research_ch1..N], "write_ch1")` 才是 wait-for-all。
  普通边是 OR 触发：任一上游完成就放行。第一版用普通边，验收表现为"任务 DONE 但一半章节还是 PENDING"。
- **动态扇出用 `Send`，不是写死的边**。章节数由 planner 产出决定，只有 `Send` 能在运行时决定分支数。
- **打回用条件边回边，不再绕**。`review_ch{i}` 判 revise 就 `add_conditional_edges` 回 `write_ch{i}`。

#### 3.0.1 State 只放能 JSON 化的簿记

```python
class ReportState(TypedDict, total=False):
    topic: str
    sections: int
    outline: list[dict[str, Any]]
    findings: Annotated[dict[str, dict], _merge_dict]     # 并行分支各写各的章，按键合并
    chapters: Annotated[dict[str, dict], _merge_dict]
    reviews:  Annotated[dict[str, dict], _merge_dict]
    review_round: Annotated[dict[str, int], _merge_dict]
    reopened: Annotated[dict[str, bool], _merge_dict]
    unresolved: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]
```

三个决定值得解释：

1. **并行字段必须带 reducer**。五个 research 在同一超步里各写 `findings` 的一个键，
   没有 `Annotated[..., _merge_dict]` 就是后写的覆盖先写的——而这种丢失不会报错，只会让某几章"没查到东西"。
   用按键合并而不是 `operator.add` 累加列表，是因为下游要按章号取值，列表还得再扫一遍。
2. **state 里绝不放 retriever / gateway / registry**。checkpoint 要 JSON 序列化落盘，对象进 state
   要么当场炸，要么更糟：反序列化出一个"看起来能用但连接已死"的副本。
   这些依赖一律用闭包传（`compile_report_graph(...)` 的参数进 `NodeRunner`），state 只放纯数据。
3. **state 不是产出的事实地**。`findings` / `chapters` 这些字段只服务于"条件边要判断这一章审过没有"。
   真正的产出（正文、citation_map、review verdict）在 `nodes` 表里，理由见下。

#### 3.0.2 为什么保留双写 TaskStore

这是迁移里唯一一个"框架已经替我存了状态，为什么还要再写一遍库"的问题。答案是**两者回答的不是同一个问题**：

| | checkpoint | `nodes` 表 |
|---|---|---|
| 回答什么 | 图推进到哪一步、下一个该跑谁 | 这个节点的状态/产出/attempt/第几轮 review 是什么 |
| 谁在消费 | 续跑逻辑（`ainvoke(None, cfg)`） | 前端 SSE 轮询、exporter 取正文、spans 成本归因、阻塞传播判定 |
| 存哪 | `data/reportflow.db.checkpoints`（独立 sqlite 文件） | 任务库 `nodes` 表 |

如果让消费方改读 checkpoint，等于把"对外数据契约"绑死在某个框架的内部结构上：框架升级换了序列化格式，
前端就得跟着改。所以节点每次状态变化（`start_node` / `finish_node` / `fail_node` / `block_node`）
都在当次执行内写回表，**换编排框架没有改变任何一个消费方**——这也是这次迁移能保持 SSE、导出、
成本报表零改动的原因。

checkpoint 单独放一个 sqlite 文件，不塞进任务库：LangGraph 会自己建表，混在一起会让"哪个表是事实来源"
变得含糊。非 sqlite 部署（MySQL）暂时不落 checkpoint，只能整轮重放——此时靠 `nodes` 表的 DONE 状态
跳过已完成节点，失掉的是"半个超步"的中途续跑能力，日志里会明说。

### 3.1 节点模型

```python
class NodeType(str, Enum):
    PLANNER  = "planner"
    RESEARCH = "research"
    WRITE    = "write"
    REVIEW   = "review"

@dataclass
class NodeSpec:
    id: str
    type: NodeType
    deps: list[str]        # 依赖节点 id
    payload: dict          # 章节标题、检索 query 等
    max_retries: int = 2
    timeout_s: int = 120
```

一次任务的拓扑形态（5 章）：

```
planner
   ├── research_ch1 ─┐
   ├── research_ch2 ─┤   research 之间无依赖，全部并行
   ├── research_ch3 ─┤
   ├── research_ch4 ─┤
   └── research_ch5 ─┘

research_ch1..5 ═(barrier)═> write_ch1 → review_ch1 → write_ch2 → review_ch2 → ... → review_chN
```

`NodeSpec`/`Graph` 仍是契约层的类型，用途没变：它是**落库契约**。`nodes` 表的 `deps` 列被前端 DagView
用来分列、被失败阻塞判定用来找下游，所以它的形状必须与历史版本一致。执行拓扑由
`compile_report_graph` 从同一份清单推导——一份清单、两个用途，两者不会漂移。

两条边需要单独解释：

- `research_*` 只依赖 `planner`，所以它们**同时**进入就绪态，并行度只受全局并发上限约束。
  图上由 `Send` 动态扇出表达（章节数是 planner 的产出，写不成静态边）。
- `write_chN` 依赖 `research_chN` **和上一章的尾部**。后者是可选项（`serial_tail`）：
  打开时尾部严格一章一章走，好处是第 N 章的 review 意见能约束第 N+1 章，跨章衔接更稳；
  关闭时退化为"每章各自 write→review"，尾部出现流水线，但章节之间彻底独立。
  默认打开——既满足"write/review 串行可观测"的验收要求，也符合长文档逐章推进的真实节奏。
  代价写在 §10：没有流水线重叠，尾部耗时是线性的，接真实 LLM 后会被放大。
  两个容易踩的细节：
  1. `serial_tail` **不落库成布尔字段**，而是 `run()` 从 deps 反推（deps 是权威记录，
     换进程也不会出现"标记与 deps 不一致"）。判据必须是"`write_ch{i}`(i>1) 的 deps 多于 1 个"，
     不能写成"deps 里含 `review_ch`"——评测 B 组无 Reviewer 时那条是 `write_ch{i-1}`，
     判据会恒为 False，把串行尾部误判成流水线（真实事故，见 STAGE5 坑三）。
  2. 流水线模式（`serial_tail=False`）在图上有个额外要求：各章必须先汇到一个 join 节点再连 `END`，
     否则第一个分支到 `END` 时整图就被判结束（见 STAGE5 坑四）。

### 3.2 执行模型

#### 3.2.1 现在：StateGraph 的超步

迁移后不再有一个"我们自己的 while 循环"。执行由 LangGraph 的超步（super-step）驱动：

```
planner ──Send(N)──> research_ch1..chN  （同一步并行，被信号量压到并发上限）
                          │ barrier：全部到齐
                          ▼
    write_ch1 → review_ch1 ─pass→ write_ch2 → … → review_chN → END
        ▲            │
        └───revise───┘   ← 条件边回边
```

五个设计点，都是从自研版本继承下来的**语义**，只是换了承载方式：

- **并发仍然要自己压**。LangGraph 的超步并行不受 `max_concurrency` 约束，`Send` 扇出的 N 个 research
  会一次性全放出去。所以节点执行器外面包了一层 `asyncio.Semaphore`——验收要求"research 最大重叠 = 3"
  是硬指标，不能指望框架替我们节流。
- **汇合必须用 barrier 边**。这是本次迁移最贵的一课，细节见
  [STAGE5_ACCEPTANCE.md](STAGE5_ACCEPTANCE.md)。一句话：普通边是 OR 触发，"等所有 research 到齐"
  只能用 `add_edge([所有 research], "write_ch1")` 表达。
- **重试仍在节点体内完成，`RetryPolicy` 是第二道防线**。这点反直觉但必要：如果让节点体把最终失败抛出去，
  LangGraph 会中断整个超步，同层兄弟章节被连坐——而"单章失败不拖累兄弟章节"是本项目的核心卖点。
  所以 `NodeRunner.run` 自己吞掉最终失败、写 FAILED/BLOCKED 后返回空更新；
  `RetryPolicy` 只负责接住节点体外层那些非预期异常，两处口径保持一致。
- **执行前的闸门只做同步判定，不要用"等待"去补图语义**。闸门（`_gate`）只承担两件框架不管的事：
  ① 上游失败 → 写 `BLOCKED` 落库（否则前端看到一堆永远 PENDING 的节点）；
  ② 断点续跑后按库里的真实状态决定该不该跑。"何时该跑"交给图，这是把编排交给框架的意义。
  曾经因为坑三（`serial_tail` 判据错）导致下游被提前派发，于是给闸门加了轮询等待去兜底；
  判据修好后等待就成了纯粹的坏味道——它把顺序问题伪装成超时，还把"偶发卡住"变成"偶发变慢"。
  现已删除，详见 `STAGE5_ACCEPTANCE.md` 坑五。
- **单实例假设没变**。`start_node` 仍是条件更新实现的原子抢占；多副本部署要按 `task_id` 加分布式锁，
  否则两个进程会同时驱动同一个 `thread_id`。

#### 3.2.2 迁移前：自研调度循环（存档，供对照）

下面这段是**旧**实现（`orchestrator/scheduler.py`，已删除）。保留它是因为其中四条规则
在 LangGraph 里换了形式但没有消失，读旧代码更容易看清"为什么需要它们"：

```python
while True:
    promote_due_retries(task_id)           # RETRYING 且 retry_at 已到期 → PENDING
    nodes = load_nodes(task_id)

    # 阻塞传播必须迭代到不动点：串行链上 review_ch1 阻塞后，write_ch2 才具备阻塞条件
    while propagate_blockers(nodes):       # 每轮重扫，直到没有新变化
        nodes = load_nodes(task_id)

    if all(is_terminal(n.status) for n in nodes):
        break                              # 唯一退出条件

    ready = [n for n in nodes if n.status == PENDING and all_done(n.deps)]
    capacity = CONCURRENCY - count(RUNNING)
    if ready and capacity > 0:
        await asyncio.gather(*(execute(n) for n in ready[:capacity]))
        continue

    if count(RUNNING):
        await asyncio.sleep(POLL_INTERVAL); continue
    if any(RETRYING):                      # 退避等待中，睡到最早到期
        await asyncio.sleep(sleep_until_earliest_retry()); continue
    break                                  # 既无 running 也无 retrying → 收敛
```

这四条规则以及它们现在的去向：

- **就绪批次 + 容量判定** → 换成 `asyncio.Semaphore`，理由不变：20 个章节同时打 LLM 会直接 429。
- **轮询而非事件驱动** → 图的推进由框架驱动，节点内部仍然按 `retry_at` 轮询等退避到期。
  保留轮询的理由也还在：两条时间线（进程 monotonic 与入库的墙钟 utcnow）差几微秒就够让节点
  永远停在 RETRYING，所以 `_await_retry_due` 多等一点并轮询确认，把"偶尔差一点"变成"一定能推进"。
- **终态判定必须显式** → 图的执行语义天然保证走到 END，但**任务终态仍要自己算**：
  节点全 DONE 但存在"打回到上限仍未通过"的章节时，终态必须是 `PARTIAL` 而不是 `DONE`。
  这个判定现在落在 `runner._final_task_status` / `_has_unresolved`，从库里推导，重启后结论一致。
- **`RETRYING` 不能当死锁** → 语义保留：它是非终态也不在跑，必须按 `retry_at` 等，不能因为
  "没有 running"就收工。

### 3.3 状态机

> 状态机的**定义**没有因为换框架而变——它记录的是"节点在库里是什么状态"，而不是"框架内部执行到哪"。
> 变的是**谁在驱动迁移**：过去是调度循环每轮扫表重算，现在是 `NodeRunner.run` 在节点每次状态变化时
> 立即写回（`start_node` / `finish_node` / `fail_node` / `block_node`），再加上一个执行前的闸门
> （`_gate`）负责 skip / block 两种让路。

```
                ┌──────────────── 退避到期 ────────────────┐
                ▼                                          │
PENDING ──► RUNNING ──► DONE                               │
                │                                          │
                └──► 失败 ──┬── 还有重试额度 ──► RETRYING ───┘
                            └── 额度耗尽 ─────► FAILED
PENDING/RETRYING ── 任一上游 ∈ {FAILED, BLOCKED} ──► BLOCKED
```

落库口径（`nodes` 表）：

| 状态 | 含义 | 是否终态 |
|---|---|---|
| `PENDING` | 等待依赖满足 | 否 |
| `RUNNING` | 已被某个进程抢占，`owner` 记录持有者 | 否 |
| `RETRYING` | 失败但还有额度，`retry_at` 记录可重跑时刻；`attempt` 此时已自增 | 否 |
| `DONE` | 成功，`output` 已写入 | 是 |
| `FAILED` | 重试耗尽 | 是 |
| `BLOCKED` | 上游失败，永久不执行 | 是 |

三点说明：

- **`BLOCKED` 是显式状态而非隐式推导**。一个章节检索三次都失败后，它的 `write`/`review`
  必须变成 BLOCKED 而不是一直 PENDING。在 LangGraph 里这条尤其重要：图已经被 barrier 边保住了顺序，
  但"上游失败"这件事**框架不替我们表达**，必须写进库，否则前端会看到一堆永远 PENDING 的节点。
- **`BLOCKED` 只能从 `PENDING` / `RETRYING` 迁移而来**，绝不能覆盖 `DONE`。
  DONE 是下游判定的依据，被覆盖会让已完成的工作被"撤销"。
- **`RETRYING` 必须落库**，不能只存在于内存。进程在退避期间被杀，重启后要能知道
  "这个节点还欠一次重试、且下一次的 attempt 已经定了"，否则会重复消耗额度。

### 3.4 断点续跑

现在有**两条**恢复路径，必须区分清楚——这是迁移后最容易混淆的地方：

| 入口 | 判据 | 做法 | 代价 |
|---|---|---|---|
| 中途断电 | checkpoint 里还有 `next`（半个超步） | `ainvoke(None, cfg)` 从最后一个 checkpoint 继续 | 已完成的节点不会再跑 |
| 失败重置后重跑（`reset_failed`） | 图其实已经走到 END，没有待执行节点 | 以初始 state 重放，靠 `nodes` 表的 DONE 状态跳过 | 图要重走一遍，但节点不重跑 |

**分工是"图负责推进、库负责裁决"**：`task_id` 一一映射成 `thread_id`（`reportflow:<task_id>`，不做哈希，
避免碰撞），同一个任务在任何进程里都落到同一条 checkpoint 链上；而"这个节点到底做过没有"永远问 `nodes` 表。
判据本身就是一行 `bool(snapshot.next)`，见 `runner._resume_mode`。

节点侧仍然沿用旧的四条规则：

1. `DONE` → 跳过（`start_node` 的条件更新天然幂等）
2. `RUNNING` → 视为孤儿（进程已死），重置为 `PENDING` 重跑（`attempt` 不变，幂等键不变）
3. `PENDING` / `RETRYING` → 正常调度（RETRYING 等 `retry_at` 到期）
4. `FAILED` / `BLOCKED` → 不自动复活，需要显式"重置失败节点"命令（`--reset-failed`）。
   自动复活是危险的：根因没修就重跑，只会把同样的钱再烧一遍。

这里有个容易漏的前提：**执行策略也必须落库**。如果只存 `status`/`output`，重启后
从节点行重建图时，`max_retries` 与 `timeout_s` 会退回代码默认值，
于是"首跑"和"续跑"用了两套策略——而这种不一致只在崩溃后出现，测试极难覆盖。
所以 `nodes` 表显式保存 `attempt / max_retries / timeout_s / deps / payload / owner / retry_at`。

另外任务本身的 `status` 也要能自愈：进程崩溃时任务停在 `RUNNING`，
续跑入口必须重新接管，而不是看到"已在运行"就拒绝启动。

**代价是要求节点执行必须幂等**。做法：写入产出前先查该节点是否已有 `output`，有则直接返回。否则重启重跑会产生重复数据。

⚠️ **但朴素的幂等检查与「Reviewer 打回重写」是冲突的**。write 节点被打回后要用同一个 `node_id` 再跑一次，如果幂等检查只认 `node_id`，看到已有 output 就直接返回旧内容，重写永远不会生效，Reviewer 会反复打回同一份东西直到耗尽重试次数。

正确做法是引入 `attempt` 序号，幂等键用 `(node_id, attempt)`：

| 场景 | 幂等键 | 期望行为 |
|---|---|---|
| 同一次执行因进程崩溃重跑 | `(write_ch1, 1)` 已存在 | 命中幂等，返回旧结果，不重复烧钱 |
| 被打回后的重新生成 | `(write_ch1, 2)` 不存在 | 正常执行新一次生成 |

这个坑不写清楚，照着文档实现出来的系统会卡在「Reviewer 永远打回」的死循环里。

### 3.5 打回重写（质量闭环）

> **现状：默认关闭。** 40 主题 × 跨厂商判官的评测显示，把它放进写作循环后
> 语义支持率反而从 99.7% 掉到 98.9%，成本却涨 49%、方差更大（CI ±8.7pp vs ±6.7pp）。
> 现在默认流水线是 `planner → research×N → write×N`，Reviewer 改为导出时的一次性体检
> （`cli export --check`，只标记不改稿）。下面的机制仍然保留，用 `--review` 可重新启用。
> 依据见 [STAGE3_ACCEPTANCE.md](STAGE3_ACCEPTANCE.md) §7。

Reviewer 判 `revise` 后要让 Writer 重写。在自研 DAG 时代，直觉做法是加一条 `review_chN → write_chN`
的边，**那是错的**：DAG 立刻成环，拓扑排序直接失败。

**迁到 StateGraph 之后不用绕了**：图允许成环，条件边直接回边就是它本来的语义：

```
review_chN verdict=revise  →  add_conditional_edges(review_chN, route, [write_chN, 下一站])
                              route 看 state["reopened"][ch]：True 回 write_chN，否则往下走
```

同时保留的两件旧机制，**都不是历史包袱**：

```
  → 目标节点（write_chN [+ research_chN] + review_chN）重置为 PENDING，attempt+1
  → 把审稿意见写进 revision_hint（唯一允许跨轮次传递的自然语言）
  → review 节点 review_round+1，超过上限则接受当前稿，任务终态降级 PARTIAL
```

- **`attempt+1` 仍然必要**：幂等键是 `(node_id, attempt)`，不涨 attempt 就会命中"这个节点已完成"的保护，
  回边会被闸门直接让路，重写永远不生效（见 §3.4）。**但它的职责变了**：现在它只表示"第几次尝试"，
  不再承担控制流——控制流由回边负责。
- **重置要在一次事务里做完**（`store.reopen_for_revision`）：目标回 PENDING、attempt+1、轮数+1 一起提交。
  否则崩溃会留下"写了 PENDING 但没加 attempt"的半套状态。
- **循环必须有上限**：`recursion_limit` 显式设 200（LangGraph 默认 25，章节多或打回多会撞
  `RecursionError`），再加 `max_review_rounds` 兜底。上限用尽仍不通过就接受当前稿并记 `unresolved`，
  任务终态降为 `PARTIAL`——"没通过审查"不能看起来像成功。

两个容易写错的地方：

- **打回必须在下一章开始写之前生效**。串行尾部下 `review_ch1` 之后本来就轮到 `write_ch2`，
  所以路由函数必须先判 `reopened` 再决定去下一章，否则第 2 章会基于一份被否掉的第 1 章往下写。
  自研时代这条是"必须在算 ready 之前处理打回"，同一个约束换了表达方式。
- **打回目标要分流**（见 §5）。实测踩到过一次：Reviewer 对"本章没有引用"一律判
  `missing_evidence`，而素材本来就是空的，于是"打回 research → 重检 → 还是空 → 再打回"，
  两轮上限烧完、任务降级 PARTIAL，问题一点没解决。
  现在的处理是：Writer 若已显式声明「资料未涉及」，Reviewer 直接放行并记录缺口，
  不再触发重检 —— 因为"打回"能修的是写偏，修不了"库里本来就没有"。

### 3.6 Span 与成本归因

网关每完成一次调用（成功或失败）写一条 span：模型、prompt/completion token、延迟、
成本、是否降级、attempt。`node_id` 通过 contextvar 传递，Agent 完全不需要知道追踪存在。

`python -m app.cli cost --task-id <id>` 直接给出总调用数、总 token、估算成本、
降级次数、失败次数，以及"最贵的前 5 个节点"。
降级场景下这个数字很有用：4 个节点产生 12 次调用、其中 4 次降级、8 次失败，
一眼就能看出"备用模型在顶着跑"。

---

## 4. 上下文交接协议

这是整个项目里最关键的设计，也是多 Agent 系统最容易翻车的地方。

### 4.1 问题

如果 Agent 之间传完整对话历史，token 消耗随阶段线性叠加：

| 阶段 | 传入上下文 | 累计 token |
|---|---|---|
| research_ch1 | 大纲 | ~1k |
| write_ch1 | 大纲 + research_ch1 全程对话 | ~6k |
| review_ch1 | 大纲 + research + write 全程对话 | ~15k |
| write_ch1（重写） | 以上全部 + review 意见 | ~20k |

章节数一多，单次调用就会撑爆上下文窗口，而且成本按平方级增长。

### 4.2 方案

定义结构化交接协议，**不传对话历史，只传结论与证据**：

```json
{
  "from": "research_ch1",
  "summary": "本章覆盖装机量、增速、区域分布三个数据点",
  "key_facts": [
    {
      "claim": "2025 年工商业储能新增装机 8.4 GWh",
      "source_id": "doc_07",
      "page": 23,
      "quote": "2025 年我国工商业储能新增装机容量约 8.4GWh……"
    }
  ],
  "open_questions": ["缺少海外市场对比数据"]
}
```

三条硬规则：

1. **`summary` 有长度上限**（200 字），超了要压缩而不是截断
2. **`quote` 必须原样保留，绝不摘要** —— 因为 Reviewer 要靠它做校验，摘要过的引用等于没有
3. **下游只拿这个 JSON，拿不到上游的任何 messages**

### 4.3 取舍

摘要一定会有信息损失。这个设计的前提是：**能损失的是推理过程，不能损失的是证据**。

损失推理过程的代价是 Writer 少了" Researcher 是怎么想的"这层上下文，可能写出角度偏差的内容——这是可接受的，因为 Reviewer 会兜住事实层面的问题。

**收益到底来自哪里，必须说清楚**，否则容易被自己的数字打脸：

- 降低的是**对话历史**的 token，不是**证据**的 token。`quote` 原样保留意味着证据部分只增不减
- 真正的收益来自「不重复传递上游的完整 messages」——每个环节原本要把前面所有轮次的对话整体重传一遍
- 因此收益随章节数和打回轮次增长而放大。**单章单轮几乎没有收益**

这就是为什么这个设计叫「防膨胀」而不是「省 token」。README 或简历里如果写「token 降低 X%」，必须注明是相对哪种基线（全量历史传递）对比出来的。

另外 `quote` 要设长度上限：取命中位置前后各 150 字、总计不超过 300 字。不设上限的话，一条长段落就能把全部收益吃干净。

---

## 5. 四个 Agent 的契约

每个 Agent 都是纯函数：`(input_contract) -> output_contract`，不读写全局状态。

| Agent | 输入 | 输出 | 关键约束 |
|---|---|---|---|
| Planner | topic, 章节数 | 大纲 + 每章检索 query 列表 | 查询词必须是可检索的名词短语，不能是问句 |
| Researcher | 单章标题 + 3 条 query | 素材包（key_facts） | 每条 fact 必须带 source_id 和 quote |
| Writer | 素材包 + 篇幅要求 | 章节正文 + 引用角标映射 | **只能使用素材包里的 fact**，不得引入外部知识 |
| Reviewer | 正文 + 该章引用的原文 | verdict + 缺陷列表 | 只看「断言 vs 原文」，不看 Writer 的推理过程 |

三条值得单独说明：

**Writer 的"不得引入外部知识"约束，是抑制幻觉的第一道防线，但它不是硬约束**。prompt 只能降低编造概率，不能保证模型不编——没有素材时模型照样会编，只是频率更低。所以必须配三道防线：

1. **prompt 约束**：每个数字、每个结论必须挂在某个 `key_facts` 上，找不到支撑就写"资料未涉及"
2. **生成后程序化校验**：正文里每个引用角标必须能映射到 `key_facts` 中真实存在的 `source_id`，映射失败直接标红
3. **Reviewer 语义校验**：逐条判断断言是否被所引用的原文支持

把"prompt 约束 = 不会幻觉"当成结论是危险的，这是本项目最容易被面试官追问的地方。

**Reviewer 不看 Writer 的推理过程**，是为了避免被话术带偏。如果让 Reviewer 看到"Writer 是这么推导的"，它会倾向于认可这个推导链，而不是独立核对原文。只给它「断言 + 原文」这一对输入，校验才有效。

**校验失败必须分流，不能一律打回 Writer**。这两类失败的根因完全不同：

| 现象 | 根因 | 正确处理 |
|---|---|---|
| 正文断言与所引原文不一致 | Writer 改写时偏离了原文 | 打回 write 节点，附上原文要求它在证据范围内重写 |
| 整章没有任何素材能支撑核心要点 | Researcher 没检索到（query 不好或库里没有） | 打回 research 换 query 重检，或标记 `open_questions` 让 Writer 显式写"资料未涉及" |

如果一律打回 Writer，会出现「素材本来就缺，但反复要求重写」的死循环——Writer 怎么写都过不了，最后耗尽重试次数交付 PARTIAL。这是多 Agent 系统里非常典型的失败模式，必须在设计阶段就区分开。

**这个坑在阶段二实现时真的踩到了**：Reviewer 最初对"本章没有引用"一律判 `missing_evidence`，
但素材本来就是空的，于是「打回 research → 重检同样空的知识库 → 再打回」，
两轮上限烧完后任务降级 PARTIAL，问题一点没解决。现在的处理是双保险：

1. 分流规则写在**解析层**而不是交给模型：`missing_evidence` 一律指向 research，
   模型自己说 `fix_target=write` 也不采信；
2. Writer 若已显式声明「资料未涉及」（`insufficient_evidence`），Reviewer 直接放行并记录缺口 ——
   「打回」能修的是写偏，修不了"库里本来就没有"。

### 5.1 三个 Agent 的校验分工

| Agent | 谁来做校验 | 具体做法 |
|---|---|---|
| Researcher | **程序** | `source_id` 必须在检索结果里、`quote` 必须能在原文中逐字找到（去空白后子串匹配）、不得短于 6 字，否则丢弃并计入 `dropped_facts` |
| Writer | **程序** | 正文角标必须能映射到素材里的 source_id，无效角标直接剥掉；`citation_map` 由程序按 fact 的 page 重建，不采信模型返回的页码 |
| Reviewer | **程序优先，LLM 兜底** | 先查角标与数字（claim 里的带量纲数字必须出现在 quote 里），命中即判 `revise` 且**不调用模型**；程序通过后再让 LLM 判语义 |

顺序不能反：程序能确定的事又快又准又免费，让模型去判断"这个数字对不对"既慢又会漏。

### 5.2 检索层：三档、RRF 与溯源约束

Researcher 的输入全部来自检索层，所以检索的契约变了，Researcher 的可靠性跟着变。这一节解释三档检索
为什么这样切、RRF 为什么是 RRF、以及最要紧的一条——**换检索方式不能削掉溯源**。

#### 5.2.1 三档，默认仍然是 BM25

| 档 | 实际做什么 | 需要 `EMBEDDING_API_KEY` | 用途 |
|---|---|---|---|
| `bm25`（默认） | 纯关键词检索（`LocalCorpusRetriever`） | 否 | 历史 40 主题评测基线的口径 |
| `vector` | 纯向量检索 | 是 | 补同义改写的召回 |
| `hybrid` | BM25 + 向量，RRF 融合 | 是 | 两路互补 |

**默认为什么不变**：现有基线是在 BM25 上跑的。默认档一换，历史数字立刻不可比，而"不可比"这件事
不会报错，只会让以后每一次评测都含糊。所以三档是**新增能力**，不是静默替换；`build_retriever(mode=...)`
是唯一入口，装配层不出现 if/else 分叉。

`build_retriever` 刻意保持**同步**：装配层（assembler / api）本来就是同步代码。代价是向量索引不能
在构造时建，改成第一次 `search()` 时惰性构建。同理 `reload()` 也必须同步（api 在上传文档后直接调它），
所以它只重建 BM25（纯本地、毫秒级），向量索引标记为脏、等下一次 `search()` 时再异步重建——
那条路径本来就在事件循环里。

#### 5.2.2 为什么是 RRF，不是归一化加权求和

这是本节唯一一个"选错了会静默变差"的决策：

- BM25 的分数是**无界**的词频×IDF（实测单片段 0.5~30），余弦相似度在 [0,1]，量纲根本不可比。
  要加权求和就得先归一化，而归一化的方法（min-max / z-score / 除以 top1）**会随查询变化**——
  等于每次查询都换一把尺子。
- RRF 只看**名次**：`score = Σ 1/(k + rank)`，`rank` 从 1 开始，`k=60` 是原论文
  （Cormack et al., 2009）与工业界的默认值。它天然免疫两路量纲不一致，也不需要调权重。

RRF 的实现（`rag/hybrid.py` 的 `rrf_fuse`）是个纯函数，抽出来是为了可测——融合的错法（rank 从 0 开始、
k 用错、同一路里重复项被重复累加）**都不会报错，只会让排序悄悄变差**。同分时按首次出现顺序决定，
保证结果确定性。

**RRF 带来的一个跨层副作用**：`rag/budget.py` 与 `researcher._rerank_by_budget` 原先把"预算外文档"
的分数乘 0.5 来降权。BM25 下相邻名次相对差 20%~50%，乘 0.5 是"降权但仍可翻盘"；而 RRF 相邻名次
只差约 1.6%（k=60），字面乘 0.5 会把片段推到 61 名开外，**降权就退化成硬过滤**，与"优先但不独占"的
设计意图正好相反。所以 `researcher.py` 里加了 `_looks_like_rrf()` 判定，RRF 档下改用名次惩罚
（`_RRF_RANK_PENALTY = 30`，等效折扣强度与 BM25 档的 0.5 基本对齐）。`DOC_BUDGET` 默认关闭，
这条只在开启预算时生效——但它是"换检索尺度会打到别的层"的典型例子。

#### 5.2.3 溯源约束怎么保住的

**`Snippet.text` 一律从 BM25 的 chunk 表取，向量索引里那份文本只用于建向量。** 融合只合并
"片段身份"（`doc_id + page + text` 拼键，用 `\x00` 分隔防拼接碰撞），返回时统一从原文取。
于是即便索引文件被人手改过，输出仍然是当前语料的逐字切片。

**索引带指纹**：模型名 + 全部 chunk 文本（同样 `\x00` 分隔）。语料一变、模型一换，指纹对不上就
自动作废重建——绝不能出现"新语料配旧向量"这种静默错误。

**embedding 缓存键含模型名**（与 LLM 缓存刻意不同）。LLM 缓存不含模型名，是为了"换便宜模型调试
不该让缓存全失效"；embedding 反过来：`bge-m3` 换 `bge-large` 是**完全不同的向量空间**，
沿用"不含模型名"的口径会静默回放另一个模型的向量，检索结果看起来正常、实际全错。
缓存落 `<llm_cache_dir>/embeddings/`，按单条文本缓存。

#### 5.2.4 三种降级，语义必须分开

| 情况 | 行为 | 为什么 |
|---|---|---|
| 没配 `EMBEDDING_API_KEY` | 全程按 BM25 跑（`effective_mode` 变 bm25） | 知识库功能不该因为缺一项配置就整体起不来；行为与改造前逐条一致 |
| replay 模式缓存缺失 | 抛 `EmbeddingReplayMiss`（`no_retry=True`） | **不静默降级**：回放出来的结果 ≠ 录制时的混合检索结果，基线就假了 |
| 网络抖动 / 5xx / 超时 | 本次降级为 BM25 并告警 | 检索只是素材入口，一次网络抖动不该让整份报告失败 |

第二条是三条里最重要的：静默降级会让一次"混合检索评测"实际上跑的是 BM25，而报告上写着 hybrid。
这类错误不报错、不异常，只会产出错误结论。

#### 5.2.5 为什么不引 faiss / 向量数据库

语料 22 篇 / 79 片段。79 × 1024 维的暴力点积在纯 Python 里是毫秒级，引一个几十 MB 的原生依赖
换不来可感知收益。更关键的是第二条理由：**自己写才能把"每条向量对应哪段原文"显式存下来**——
向量库只保证"能查到某个 id"，不保证 id 背后的文本没被改过，而这正是溯源的前提。

代价也认：语料规模上去（比如上万片段）之后，暴力点积会真的变慢，那时该换就得换。

---

## 6. 容错设计

### 6.1 失败分类与处理策略

| 失败类型 | 处理策略 |
|---|---|
| LLM 超时 | 指数退避重试（1s / 2s / 4s），超过 `max_retries` 后降级到备用模型 |
| 限流 429 | 退避后重试，同时降低全局并发度 |
| 检索无命中 | 不重试，返回空素材包 + `open_questions`，让 Writer 显式写"资料未涉及" |
| Reviewer 不通过 | 携带缺陷打回对应 write 节点，最多 2 次；超限则标记 `PARTIAL` 交付 |
| 结构化输出解析失败 | 重试一次，仍失败则用正则兜底抽取关键字段 |
| 循环风险 | 全局轮次上限 = 正常任务收敛轮次的 P95 × 1.5 |

最后一条需要先说清"正常收敛轮次"怎么来的：统计历史任务的打回次数分布，取 P95。没有数据就先设一个保守值（如 2 次），跑出分布后再回调。

### 6.2 任务生命周期管理

长任务系统还必须处理三种「非正常结束」，这是原设计里漏掉的部分：

| 场景 | 处理 |
|---|---|
| 用户主动取消 | 标记 `CANCELLED`，运行中的节点在下一次检查点退出（不强杀进程，避免写出半截数据） |
| 单任务超时 | 总时长上限（如 30 分钟），超时标记 `TIMEOUT`，保留已完成章节 |
| 僵尸任务 | `RUNNING` 超过 N 分钟无心跳，由清理任务重置为 `PENDING` |

### 6.3 SSE 断线重连

SSE 连接一定会断（切网、休眠、代理超时）。不能假设连接一直存在：

1. 每个事件带自增 `seq`
2. 前端重连时带 `Last-Event-ID: <last_seq>`
3. 服务端从 `seq + 1` 补发；没有历史事件时直接推送当前全量状态快照

断线重连做不好，用户刷新一次页面进度就没了——这是长任务系统最容易被忽略、又最影响体验的地方。

---

## 7. 可观测性

> 阶段二已实现：`spans` 表 + `SqlSpanRecorder` + `app.cli cost`（见 §3.6）。

### 7.1 span 模型

每次 LLM 调用产生一条 span：

```sql
CREATE TABLE spans (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  task_id       VARCHAR(32),
  node_id       VARCHAR(64),
  span_type     VARCHAR(32),   -- llm_call / retrieval / validate
  model         VARCHAR(64),
  prompt_tokens INT,
  completion_tokens INT,
  latency_ms    INT,
  cost          DECIMAL(10,6),
  created_at    DATETIME
);
```

### 7.2 成本归因

有了 spans，可以直接回答三个问题：

```sql
-- 一份研报花了多少
SELECT SUM(cost) FROM spans WHERE task_id = ?;

-- 钱花在哪个环节
SELECT node_id, SUM(cost) FROM spans WHERE task_id = ? GROUP BY node_id ORDER BY 2 DESC;

-- 重写占比
SELECT SUM(CASE WHEN node_id LIKE '%retry%' THEN cost ELSE 0 END) / SUM(cost) FROM spans WHERE task_id = ?;
```

"重写占总成本的比例"是一个很有价值的指标——如果重写花了 40% 的钱，说明 Writer 的 prompt 或素材质量有问题，应该优化上游而不是加机器。

**为什么不上 OpenTelemetry**：需求只有 token、延迟、成本三个维度，且都在进程内。OTel 的价值在于跨服务、跨语言的链路串联，这里是单体应用，接入成本大于收益。如果未来拆服务，再迁移。

### 7.3 成本量级预估

做设计时就要有数量级概念，否则「成本治理」无从谈起。一份 5 章研报的粗算：

| 环节 | 调用次数 | 单次 token | 小计 |
|---|---|---|---|
| Planner | 1 | ~2k | 2k |
| Researcher | 15（5 章 × 3 query） | ~1.5k | ~22k |
| Writer | 5 | ~4k | ~20k |
| Reviewer | 5 | ~3k | ~15k |
| 重写（按 20% 触发） | ~1 | ~4k | ~4k |
| **合计** | | | **~63k token** |

按国内主流模型价格换算，单份成本在 0.1–0.3 元量级。这意味着 20 主题 × 3 组的全量评测总花费在 10 元以内——这也是选本地知识库而非联网搜索 API 的另一个现实理由。

**实测校正（阶段二，DeepSeek）**：5 章任务 16 次调用、prompt 12.3k / completion 6.3k token，
实际花费 **¥0.031**，耗时 25 秒。比估算低 3–10 倍，原因是估算按"每章 3 条 query 各检索一次"
假设了 15 次 researcher 调用，而实际实现是每章合并 query 后一次调用。
**估算不能当结论用，这一条现在有实测数字了。**

数字会随模型与篇幅变化，实现后必须用 spans 实测数据替换这里的估算，估算不能当结论用。

### 7.4 开发阶段的模型成本：录制 / 回放

真正拖慢开发的不是钱（单份 ¥0.03），是**每次改界面都要等 20 秒，而且每次产出都不同**——
调试前端时你分不清"是我改坏了"还是"模型这次就是这么写的"。所以传输层之外加了一层缓存：

```
Agent → LLMGateway（重试/降级/计量）→ CachedTransport（录制/回放）→ OpenAICompatibleTransport
```

四个设计点：

- **缓存键只含 prompt 内容 + 是否 JSON 模式，不含模型名**。换更便宜的模型调试不该让缓存全失效；
  代价是"用 A 录的响应可能被 B 回放"，所以录制文件里存了 model 备查。
- **prompt 变了键就变**。这是刻意的：改了 prompt 还命中旧缓存，等于用过期响应骗自己。
- **回放命中记 ¥0**（span 成本为 0）。否则成本报表会显示"花了钱"而银行卡一分没动，那是假账。
- **缺失缓存不重试**。回放 miss 是确定性缺失，重试只会白等退避时间；
  用异常自带的 `no_retry` 标记，调度层不必认识任何具体异常类型。

最重要的约束在评测侧：**`EvalRunner` 拒绝在非 `live` 模式下运行**。
回放会让「重复 2 次取均值」变成「同一次结果算两遍」，置信区间被人为压窄，
看起来"显著"其实什么都没测——这类错误不会报错，只会静默产出错误结论。

实测（2 章任务，同一 prompt）：录制 18.44s / ¥0.0215，回放 0.09s / ¥0.0000，产物逐字相同。

---

## 8. 评测体系

> 阶段三已实现：`app/evaluation/`（数据集/指标/judge/语义审计/配对统计）+ `eval/topics/`（40 主题 / 141 要点 / 81 错误说法）+ `eval/run_eval.py`。
> 实测结果、四个被评测暴露的 bug、以及为什么它们会让结论反向，见 [STAGE3_ACCEPTANCE.md](STAGE3_ACCEPTANCE.md)。
>
> 一句话结论（40 主题 × 3 档，跨厂商判官，配对比较）：
> **多 Agent 相对单 Agent 覆盖率 +7.7pp [+1.6, +13.7]、语义支持率 +7.4pp [+3.3, +11.6]，两个维度都显著；
> Reviewer 无收益（B→C 覆盖率 +0.3pp、语义支持率 −0.8pp，均不显著），成本再涨 49%。**
>
> 这个结论花了四轮才对上：前两轮先是样本太小（20 主题，CI ±10pp，显著性在两批之间翻转），
> 再是判官就是被测模型本身（自我偏好），最后是语义抽样偏向简单句（三档都是 96~97%，天花板效应）。
> 三次都是**测量方法的问题，不是系统的问题**——所以"分不出胜负"的结论，该先怀疑尺子。
>
> 单 Agent 的失分形态很具体：16 处把 A 文档的数据挂到 B 文档的角标上，多 Agent 只有 1 处。
> 多 Agent 的收益不是"更聪明"，是**引用归属正确**。

### 8.1 评测集

40 个主题（前 20 个覆盖主流细分，后 20 个刻意换角度），每个主题的标注格式：

```yaml
topic: "2026 年中国工商业储能行业"
must_cover:
  - "2025 年新增装机量约 8.4 GWh"
  - "同比增长超过 60%"
  - "浙江、广东、江苏三省占比超 55%"
  - "CR5 约 38%，集中度低"
must_cite:
  - doc_07   # 白皮书
  - doc_12   # 协会报告
forbidden:
  - "储能装机量同比下降"   # 与事实相反，测幻觉
  - "CR5 超过 80%"        # 常见误传
```

`forbidden` 里的错误说法是从真实资料里故意构造的，用来测模型会不会"自信地写错"。

**判定不能靠关键词匹配**。错误说法可能出现在否定语境中（"有观点认为 CR5 超过 80%，但实际为 38%"），关键词匹配会把正确表述判成错误。正确做法：把「生成正文 + forbidden 说法」交给 LLM judge，要求判断"正文是否**主张**了该错误说法"，输出 yes/no 加证据句，同样走 20% 人工校准。

**样本量决定结论能说多细**。20 个主题时，两档差 1 个主题就是 5 个百分点，配对 CI 约 ±10pp——
这正是 A→B 在三轮之间从「显著」翻成「不显著」的原因。扩到 40 主题后 CI 收到 ±6~8pp，
+7.7pp 才站得住。想支持更细的结论（比如 3pp 量级），还要继续扩样本，这是数学约束，不是调参能解决的。

### 8.2 指标定义

| 指标 | 定义 | 计算方式 |
|---|---|---|
| 要点覆盖率 | 命中的 must_cover 数 / 总数 | LLM judge 逐条判定，输出结构化 yes/no |
| 引用准确率 | 引用有效的断言数 / 总断言数 | 程序校验 source_id 存在性 + LLM 判定原文是否支持断言 |
| 事实错误数 | 与来源矛盾的断言数 | 人工抽查 5 份 |
| 单份成本 | 一次任务的总花费 | `SUM(spans.cost)` |
| 耗时 | 任务创建到完成 | `finished_at - created_at` |
| **论述重复率** | 跨章重复的 12 字论述片段占比 | 程序计算，`claim_repeat` |
| 来源复用率 | 被 ≥2 章引用的来源占比 | 参考用，**比论述重复高约 4 倍，不可单独下结论** |

最后一行是实测之后补的，不是设计时想到的。5 章里 4 章复述同一批数字，
但每条断言都"可溯源"、覆盖率也不低——**只看得分的话这作品会被判为合格**。
重复率把这种"正确但无用"的产出暴露出来，所以它必须进指标表。

另外「引用准确率」在实现上被拆成了两个更可操作的口径，因为合成一个数会掩盖问题：

- **引用有效率**（程序）：角标能否映射到真实存在的检索来源。回答"有没有假引用"。
- **语义支持度**（judge）：原文是否支持该断言。回答"引对了没有"。

阶段三只实现了前者（确定性、免费、可复现），后者依赖 judge 校准，留待后续。
这个拆分要写清楚，否则"引用准确率 100%"会是误导性结论。

### 8.3 LLM judge 的偏差控制

用 LLM 当裁判本身有偏差，所以：

1. **温度设 0**，且要求结构化输出（JSON），避免自由发挥
2. **抽 20% 人工复核**，计算 judge 与人工的一致率
3. **一致率低于 85% 就换 judge 模型或重写判定 prompt**，而不是直接相信分数
4. 判定时**只给「要点原文 + 生成正文」，不给评分标准**，减少引导性偏差

这套流程本身也是要写进报告的——只说"覆盖率达 90%"，不说 judge 怎么校准的，分数不可信。

### 8.4 三档对比的控制变量

| 组别 | 配置 |
|---|---|
| A 单 Agent | 一个 prompt，喂入**同样的全部检索素材**，直接生成全文 |
| B 多 Agent | Planner + Researcher + Writer，无 Reviewer |
| C 多 Agent + Reviewer | 完整流水线，含打回重做 |

**A 组必须给同样的素材**。很多人做对比时让单 Agent 组"裸写"，结论自然偏向多 Agent，但这种对比是不公平的，面试官一问就露馅。

变量控制：同一评测集、同一模型、同一温度、同一 chunk 策略、同一篇幅要求。

**素材相同还不够，检索输入也必须公平**。第一版评测在这里翻车了：A 组的检索 query 用了评测集的
`must_cover`（等于拿参考答案去查资料），结果 A 组覆盖率 97.5%，看起来"单 Agent 最强"。
改成只用 topic + 通用维度词后，A 组掉到 78.8%——多出来的那 18.7 个百分点全是作弊。

这个坑很隐蔽，因为它不会报错、也不会让结果看起来荒谬，只是把结论方向整个反转。
**凡是"某一档表现异常好"的时候，先检查它的输入是不是偷偷多了信息。**

---

## 9. 关键取舍记录

| 决策 | 备选方案 | 为什么这样选 | 付出的代价 |
|---|---|---|---|
| 编排顶层用 LangGraph `StateGraph` | 继续维护自研 DAG 引擎 / AutoGen / Prefect | 图原语（环、`Send` 动态并行、Checkpointer）正好是这个流水线的形状；状态迁移、重试、checkpoint 由框架维护，新人看官方文档而不是我们的调度循环 | 多一个不能随手升的依赖；框架的隐式语义要踩坑才认得（见 §3 开头与 STAGE5）。**迁移前是自研引擎，见 git 历史** |
| 节点状态仍自己落库（双写） | 让消费方直接读 checkpoint | checkpoint 是框架内部结构，绑定它等于把对外契约交给框架版本；`nodes` 表才是 SSE / exporter / spans 的数据源 | 每处状态变化都要记得写回库，写漏了前端就不动 |
| 分章生成 | 一次性生成长文 | 单章质量更可控，支持局部重写 | 章节间连贯性弱，需要 Editor 统稿补 |
| 本地知识库 | 联网搜索 API | 结果可复现、零成本、引用可溯源 | 知识时效性受限，需预先灌库 |
| 自建 Trace | OpenTelemetry | 需求仅三个维度且在单进程内 | 无通用 APM 生态，跨服务需重做 |
| 结构化交接 | 传完整历史 | 控制 token 膨胀 | 摘要会丢推理过程，需靠 Reviewer 兜底 |
| Reviewer 有锚校验 | 直接问 LLM「这段写得对吗」 | 避免 Reviewer 自己幻觉 | 依赖检索质量，原文不准则校验失效 |
| 打回上限 2 次 | 不限次数直到通过 | 防止成本失控 | 质量天花板被限制，需接受 PARTIAL 交付 |
| 打回用条件边回边 | 「重置节点 + attempt+1」（迁移前的做法） | StateGraph 允许成环，回边是它本来的语义；`attempt` 回归"第几次尝试"，不再兼做控制流 | 需要把"打回"翻译成条件边路由，校验不严就退化成无限循环（靠 `review_round` 上限兜底） |
| 程序校验优先于 LLM 校验 | 全部交给 LLM 判 | 角标映射、数字是否出现在原文里，程序判得又快又准且免费 | 只能覆盖"可机械判定"的错，语义层面的偏离仍需 LLM |
| 检索默认仍用 BM25 | 直接默认 hybrid / 上 ChromaDB + BGE | 目标仍是「引用可溯源」而非召回率：BM25 零依赖、可复现，也不会因 embedding 升级导致基线漂移。向量与 RRF 作为**新增档位**，不替换默认 | 同义改写召不回来，需要 query 里带够关键词；vector/hybrid 有网络与缓存依赖 |
| 向量索引自己写，不引 faiss | faiss-cpu / ChromaDB / sqlite-vec | 79 片段暴力点积毫秒级；且只有自己写才能把"每条向量对应哪段原文"显式存下来（溯源前提） | 语料上万片段后会真变慢，那时需要换 |

---

## 10. 已知未解决问题

- **40 主题评测基线未在 LangGraph 改造后重跑**（本轮最明确的欠账）。
  README「效果与代价」的三档数字来自改造前的自研编排（`eval/results/v6/`），仍然成立，但对应的是旧编排。
  新基线要另起目录（如 `eval/results/v9-langgraph/`）重跑，**不能**与旧数据混在一起比。
  在三档检索上同理：`vector` / `hybrid` 只有单主题实测，没有 40 主题的量化对照。
- **流水线模式（`serial_tail=False`）的拓扑正确性已覆盖，但并行度没有断言**。
  四种 `serial_tail × with_review` 组合"每章都要跑完"已由 `TestTopologyMatrix` 锁住
  （含一个真实踩过的严重 bug：`serial_tail` 从 deps 反推的判据在无 Reviewer 时恒为 False，
  导致默认配置只生成第 1 章，见 STAGE5 坑三）。缺的是"流水线档下各章确实重叠推进"的时间重叠度断言
  ——`serial_tail=True` 有这套口径，流水线档没有。
- **尾部串行没有流水线重叠**：`serial_tail=True` 下 5 章的尾部耗时 ≈ 5×(write+review)，
  research 早已跑完却在干等。是否需要提速，要看评测数据——并行化会牺牲跨章衔接，不能拍脑袋改。
  （注：迁移后"波次推进的槽位浪费"已经没有了：`asyncio.Semaphore` 在节点完成时立刻释放，
  后续节点随即补位，不再是"一波跑完再判下一批"。）
- **`vector` / `hybrid` 缺少可观测的检索过程**：切档靠 `.env` 的 `RETRIEVAL_MODE`，
  顶栏会显示**实际生效**的档位（含降级标记与 embedding 模型），但看不到"这条片段由哪一路召回、
  两路各自排第几"。判断混合检索到底有没有参与，目前只能看日志。
- **章节间重复**：相邻章节可能引用同一批数据，目前只靠 Editor 事后去重，没有根本解决。
  **阶段二用真实模型实测确认了这个问题**：5 章里有 4 章重复引用同一批数字（8.4GWh / 55% / 22/19/14%）。
  排查结论是**素材池不足**而非大纲重叠——把 Planner 的"维度互斥"约束加强后，大纲确实变干净了
  （定义 → 规模 → 区域 → 格局 → 展望），正文仍然重复，因为 3 篇文档只够支撑 1~2 章的独立论据。
  所以这条的正确修法是"让知识库规模匹配章节数"，而不是继续调 prompt；
  评测阶段应把**论述重复率**作为指标之一，否则"每句都能溯源但读起来像复读机"的作品会被判为合格。
  注意不能用「来源复用率」代替它：实测前者 43.5%、后者 8.7%，差 4 倍，
  且两者对实验效果的判定**方向会相反**（见 REPEAT_RATE.md §2）
- **长文档**（>10 章）时，跨章节的逻辑一致性无法保证
- **表格生成**：素材包里的表格目前被降级为文本描述，丢失了结构
- **Reviewer 漏判率**：只在 5 份样本上做过人工核对，样本量不足以给出置信区间
- **知识时效**：知识库是静态的，无法反映最新数据

把这些写进文档不是为了显得诚实，而是因为这些正是面试官会问的"你这个方案有什么不足"。提前有答案，比临场编好。
