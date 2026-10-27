# 阶段五验收记录：编排迁到 LangGraph + 三档检索

环境：Windows 11 / Python 3.12.10 / SQLite（WAL）/ 单进程
运行目录：`backend/`，`$PY` 代表项目虚拟环境里的 Python（`backend/.venv/Scripts/python.exe`）

阶段五交付范围：三件事，分属三层：

1. **编排层**：自研 DAG 引擎 → LangGraph `StateGraph`。对外行为等价（并行、串行、重试、阻塞传播、
   打回闭环、断点续跑），节点命名与落库契约不变。
2. **检索层**：单一 BM25 → 三档（`bm25` / `vector` / `hybrid`），默认仍是 `bm25`。
3. **工具层（T3）**：Agent 具备 Function Calling 能力 + 联网检索。**默认关闭**，
   开着时才多一轮工具循环；关闭时请求形状与引入前逐字节一致。

三者的共同底线是同一句：**历史 40 主题评测基线不能因为这些事漂移**。所以默认档不变、节点名不变、
`Snippet` 契约不变、对外崩溃契约不变、工具默认不启用。

---

## 0. 一句话结论

- 测试 **320 passed**（改造前基线 139），`ruff check .` 通过。
- **40 主题评测已在 LangGraph 编排下重跑**（`eval/results/v9-langgraph/`，真实模型 + 跨厂商判官）。
  三档全部 40/40 成功：覆盖率 A 73.2% / B 77.5% / C 79.3%，
  语义支持率 A 93.3% / B 98.2% / C 99.8%。
  与改造前 v6 相比三档绝对值都在同量级，**换编排没有造成质量数量级变化**。
- **工具调用 + 联网检索已可用但默认关闭**（T3，见 §6.6）。真实模型实测能自主决定
  「先查本地、不够再联网」，真实联网能拿到正规媒体的可引用摘要。
  默认关闭是刻意的：一开联网，40 主题评测就不再可复现。
- **重跑过程抓到一个只在 C 档出现的真 bug**（打回 `fix_target=research` 时死循环到
  `GraphRecursionError`），已修复并加了变异测试验证过的回归用例。见 §3.6。
- 并行/串行/断点续跑的验收口径**用时间重叠度与"是否重跑"实测**，不看日志自述：
  research 最大重叠 3、write 1、review 1；崩溃后续跑**没有一个已完成节点被重跑**。
- **仍未做**：`vector`/`hybrid` 两档没有 40 主题量化对照，只有单主题实测（§7）。

---

## 1. 为什么迁：诚实版

先把容易被误读的话说清楚：**不是因为自研引擎不行**。

`orchestrator/dag.py` + `orchestrator/scheduler.py` 那套实现跑过了阶段一~四的验收，
并行、串行、重试、阻塞传播到不动点、断点续跑都是实测过的，行为是对的。这次迁移是主动的技术选型变更，
理由按真实权重排序：

| 理由 | 具体是什么 | 权重 |
|---|---|---|
| **图原语对上问题形状** | 这条流水线需要三件事：写-审-改的**环**、章节数到运行时才知道的**动态并行**、进程崩溃后的**断点续跑**。自研为了第一条只能靠"重置节点 + attempt+1"绕开环，后两条要自己维护整套状态机与持久化。LangGraph 三件都是原语 | 主要 |
| **可维护性与招人成本** | 图结构、状态迁移、重试策略、checkpoint 序列化交给框架维护。新同事读官方文档，而不是读我们那 470 行调度循环 | 主要 |
| **生态与后续能力** | `astream_events`、Postgres/Redis saver 这类能力自研要重做一遍。现在只是"留着门"，没有真用 | 次要 |

同时必须承认代价，否则就是自欺：

- **多了一个不能随手升的依赖**。框架大版本一动，行为细节就可能变，评测基线随之不可复现。
  所以 `requirements.txt` / `pyproject.toml` 都锁了大版本上限。
- **"用框架"没有让编排变简单**，只是把复杂度从"自己写的状态机"换成了"必须理解对的图语义"。
  本次踩的坑（§3）全部属于这一类：代码能跑、不报错，但结果是错的。
- **失去"自研引擎"这个自述亮点**。真实情况是它的历史价值仍在（阶段一~四的验收记录、
  ARCHITECTURE §3.2.2 存档的调度循环），只是不再是当前实现。

> 迁移前的实现见 git 历史；阶段一~四的验收结论仍然有效，它们描述的是**行为契约**，
> 而这些契约这次一条没改（§5）。

---

## 2. 迁移映射

| 自研概念（原位置） | LangGraph 对应物 | 落地时的实际差异 |
|---|---|---|
| `Graph` + 拓扑排序（`dag.py`） | `StateGraph` + `add_edge` | `NodeSpec`/`Graph` **保留**：它现在是"落库契约"（`nodes.deps` 被前端分列、被阻塞判定使用），执行拓扑另由 `compile_report_graph` 从同一份清单推导 |
| 调度循环（`scheduler.py`） | 超步（super-step）自动推进 | 我们不再有 while 循环，但**并发闸仍要自己包**（`asyncio.Semaphore`）——超步并行不受 `max_concurrency` 约束 |
| 无依赖节点并行 | `Send` 动态 fan-out | 章节数由 planner 产出决定，`Send` 是唯一能表达"运行时才知道分支数"的原语 |
| 汇合（等全部 research） | barrier 边 `add_edge([...], target)` | 普通边是 **OR 触发**，表达不了 wait-for-all。这是坑一 |
| `max_attempts` + 指数退避 | `RetryPolicy(retry_on=..., max_attempts=...)` | **真正的重试仍在节点体内完成**，`RetryPolicy` 只做第二道防线。理由见下 |
| 阻塞传播到不动点（`_propagate_blockers`） | 节点执行前的闸门 `_gate` + 写回 `BLOCKED` | 框架不替我们表达"上游失败"，必须落库，否则前端看到一堆永远 PENDING 的节点。闸门只做同步判定，**不要**用轮询等待去补图语义，见坑五 |
| 断点续跑（`claim_orphans` + 状态重算） | Checkpointer + `thread_id` | `task_id` → `thread_id = reportflow:<task_id>`，SQLite 落 `data/reportflow.db.checkpoints`；**两条恢复路径**见 §5.3 |
| 打回（`_apply_revisions` 的重置 + attempt+1） | `add_conditional_edges` 条件回边 | 回边是原语了；但 `attempt+1` 仍然要保留（幂等键依赖它），只是不再兼做控制流 |
| SSE 轮询数据库 | 沿用轮询 | 没有改用 `astream_events`：消费方零改动比用上新特性重要 |
| `NodeSpec` / `Graph`（`contracts/nodes.py`） | `TypedDict` State | 契约类型**没删**，用途从"执行图"变成"持久化图"；State 另立 |
| 成本归因按 `node_id`（`spans.py`） | 节点函数内继续用 `span_context` | **节点命名一字未改**，这是硬约束不是偏好，见 §5.1 |

**为什么 `RetryPolicy` 不是唯一的重试机制**——这条反直觉，但它是"单章失败不拖累兄弟章节"的前提：

如果让节点体把最终失败抛出去，LangGraph 会中断整个超步，同层兄弟章节被连坐。
所以 `NodeRunner.run` 自己吞掉最终失败、写 `FAILED`/`BLOCKED` 后返回空更新；
`RetryPolicy` 只负责接住节点体外层那些非预期异常。两处口径一致（都尊重 `no_retry` 标记）。

---

## 3. 踩坑与修正

> 这一节是本文最有价值的部分。几个坑的共同特征：**不报错、不崩，只是结果是错的**。
> 都能通过"节点都执行了"这类测试，只有拿"时间重叠度""是否重跑""所有节点是否终态"这类硬指标去卡才暴露。

### 3.1 坑一：普通边是 OR 触发，不是 wait-for-all

**错误现象**

3 章任务的验收跑完，任务状态 `DONE`，但看节点表发现**有一半章节还停在 `PENDING`**。
日志里每个节点都"正常完成"，没有任何异常。

**根因**

第一版用普通边把 N 个 research 收进第一章：

```python
builder.add_edge("research_ch1", "write_ch1")
builder.add_edge("research_ch2", "write_ch1")   # 以为"两条都到齐才放行"
builder.add_edge("research_ch3", "write_ch1")
```

LangGraph 的普通边是 **OR 语义**：一个节点有多条入边时，**任意一条上游完成**就会把它放出去，
其余上游还没好也照跑。于是 `write_ch1` 在 `research_ch1` 一完成就启动了。

更麻烦的是第二层：即使把"依赖没齐就让路"写进节点体，**让路本身也会继续往下触发边**。
链条一路传到 `END`，整张图在真正的节点还是 `PENDING` 时就判为结束——所以任务显示 DONE，
章节却没写。

**修法**

用 barrier 边，它是 LangGraph 原生的 wait-for-all：

```python
research_nodes = [f"research_ch{i}" for i in range(1, sections + 1)]
builder.add_edge(research_nodes, "write_ch1")     # N 个 research 全部到齐才放行
```

**这个坑的第二次形态**（同一类语义，出现在别处）：给 `write_ch{i}` 同时加 barrier 边和链式前驱边，
会因为"多个入边组取任一满足"而在链式前驱还在跑时就放出去，随后被闸门以"依赖未就绪"跳过、
**且不再重触发**，整章永久 PENDING。所以现在 chain 只挂在起点：先 barrier 等齐全部 research，
之后由 `wire_chapter` 串成**每章只有一个触发来源**的单入边链。

**留下的教训**：图库里"边"的默认语义要逐个确认，不能按直觉（"依赖"）理解。
`add_edge`、`add_edge([...], x)`、`add_conditional_edges` 三者的触发语义完全不同。

### 3.2 坑二：`SystemExit` 在带 Checkpointer 的图里变成 `CancelledError`

**错误现象**

做断点续跑验收需要模拟"进程被杀"。第一版用 `raise SystemExit(137)`，然后调用方拿到的东西
既不是 `SystemExit` 也不是正常返回，而是挂住不返回；即使返回，冒出来的是 `CancelledError`
——"断电"被伪装成"任务被取消"。

CLI 与旧测试期望的是 `SystemExit`（退出码 137），于是对外契约也跟着坏掉。

**根因**

`SystemExit` 在 asyncio 语境里会被特殊处理（它本身就用于终止解释器），
异步任务因此被取消；而 `AsyncSqliteSaver` 在取消路径上要做 checkpoint 收尾，
这条路径在测试里表现为"卡住"。合并起来就是：类型不可预测 + 偶发挂死。

**修法**

引入自定义信号，**刻意继承 `BaseException`**：

```python
class SimulatedCrash(BaseException):
    exit_code = 137
```

两个设计点：

1. **继承 `BaseException` 而不是 `Exception`**：它是进程级事故，绝不能被节点内的重试循环
   或 `RetryPolicy` 当成可重试错误吞掉。`_retryable()` 里对 `BaseException` 家族明确返回 `False`。
2. **对外契约不变**：`runner._drive` 抓捕 `SimulatedCrash` 后翻译回
   `raise SystemExit(SimulatedCrash.exit_code)`，CLI/测试看到的仍然是退出码 137。
   内部用自定义异常、对外保持旧契约——这让旧测试一行没改。

**留下的教训**：`BaseException` 家族（`SystemExit` / `KeyboardInterrupt` / `CancelledError`）
在异步框架里语义各异，不要拿它们当"我自己的信号"。自己定义、自己在边界翻译。

### 3.3 坑三：默认配置下只生成第 1 章（最贵的一个）

**错误现象**

默认配置（`REVIEW_IN_PIPELINE=false`，也就是评测 B 组的口径）下跑 5 章任务，
**只生成第 1 章**，`write_ch2` 之后的节点全部停在 `PENDING`，任务终态 `PARTIAL`，
**而且不报任何错**。真实 LLM 与 mock 都能复现。

**根因**

`runner.py` 从落库的 `deps` 反推 `serial_tail`（串行尾部标志）时，判据写成了
"`write_ch{i}` 的 deps 里是否含 `review_ch`"。但 B 组**没有 Reviewer 节点**，
那条 dep 是 `write_ch{i-1}`，于是判据**恒为 False**。

连锁反应：

```
判据错 → 把串行尾部误判成流水线模式
       → 图按流水线拓扑编译（各章独立并行分支）
       → 下游章节被提前派发
       → 闸门读库时上游还在 RUNNING，只能让路
       → LangGraph 不再给第二次触发机会 → 该章永久 PENDING
```

**为什么这个比其它坑都危险**，三条叠加：

1. **静默失败**：不抛异常、任务还能"成功"结束，只是报告少了几章。
2. **只在默认配置下出现**：`REVIEW_IN_PIPELINE=false` 是默认值，开发时若开着 `--review` 就永远看不到。
3. **污染评测口径**：默认配置正是 40 主题评测 B 组的口径。这个 bug 在，**评测基线会整体失真**——
   不是"多跑几趟"能发现的，得看节点表才知道少了几章。

**修法**

判据换成"`write_ch{i}`（`i>1`）的 deps 是否多于 1 个"：

```python
serial_tail = any(
    len(node.deps) > 1
    for node in graph
    if node.id.startswith("write_ch") and node.id != "write_ch1"
)
```

串行尾部下它必然有两个依赖（本章 research + 上一章尾部，有 review 时是 `review_ch{i-1}`，
无 review 时是 `write_ch{i-1}`）；流水线模式下只有 research 一个。**这个判据对
"有没有 Reviewer"不敏感**，这正是原判据缺的性质。

修法的第二层是闸门：拓扑判对之后，闸门里那段"依赖未就绪就原地轮询等待"的代码
其实是多余的（见坑五）——图已经保证了派发顺序，闸门只需要做纯同步判定。
删掉等待后行为更确定，也不会把顺序问题伪装成超时。

**回归保护**

新增 `TestTopologyMatrix` 4 条用例，覆盖 `serial_tail × with_review` 四种组合都要**每章跑完**。
并且做过**变异测试**验证有效性：把旧判据注回去，这 4 条里有 2 条立刻失败——
证明测试真能抓住它，不是"凑数的绿"。

**留下的教训**：**默认配置是最该被测试的组合，也是最容易被开发时的方便选项绕过的组合。**
这个 bug 之所以活到最终验收，是因为它只在"没人特意去开 review"时出现。

### 3.4 坑四：流水线模式下第一章跑完，整张图就收工

**错误现象**

`serial_tail=False`（每章各自 write→review、各章可重叠推进）时，3 章任务只跑了第 1 章。
节点表里留着 4 个 `PENDING`，**任务却被标记为完成**。

**根因**

各章 write/review 是彼此独立的并行分支，如果都直接连 `END`，LangGraph 会在
**第一个到达 `END` 的分支**把整张图判为结束——它把 `END` 当成"有人走到终点就结束"，
而不是"所有分支都到齐"。

串行模式（`serial_tail=True`）为什么没这个问题：那里本来就只有一条链，最后一个节点到 `END` 时
前面必然都完成了。所以这个 bug 只在流水线模式出现，**而流水线模式不是默认档，很容易漏测**。

**修法**

加一个不参与业务的汇合节点，让"结束"变成显式的最后一步：

```python
join_id = "pipeline_join"
builder.add_node(join_id, make_join(sections))      # 只做控制流收拢，不写 state
for i in range(1, sections + 1):
    builder.add_edge(f"research_ch{i}", f"write_ch{i}")
    wire_chapter(i, join_id)                        # 各章通过后都去汇合点
builder.add_edge(join_id, END)
```

`pipeline_join` 有 N 条入边，但正因为它不是业务节点，"等所有分支到齐"由 barrier 语义保证，
不会提前放行。**已修复，且已由 `TestTopologyMatrix` 覆盖**（四种组合现在全部通过）。

**留下的教训**：并行图里 `END` 的语义要当"汇合点"对待，不是"出口"。任何多分支收尾都必须显式汇合
——而且这类错误同样不报错，只让图"提前正常结束"。

### 3.5 坑五：闸门与图的边语义不一致（曾一度用"轮询等待"掩盖）

**错误现象**

拓扑判对之后，B 组仍有章节卡住：`write_ch2` 及其后续停在 `PENDING`，任务 `PARTIAL`。
（修坑三之前，这个现象被误当成"串行判据"的同一件事；修完后才发现还有独立的一层。）

**根因**

不在边，而在**闸门判据的来源**。图上的边语义与库里的 `deps` 不是一一对应的——
B 组图上 `write_ch{i}` 的前驱是 `write_ch{i-1}`，而库里 `deps` 还列着 `research_ch{i}`。
两者不一致时，闸门读库看到依赖还在 `RUNNING`，就判"依赖未就绪"让路；
而 **LangGraph 不会再给第二次触发机会**——让路即永久 PENDING。

C 组为什么不中招：那里 `write_ch{i}` 的前驱是 `review_ch{i-1}`，时序上刚好错开。
**这类 bug 只在特定档位组合下出现。**

**走过的弯路（值得记下来）**

第一版修法是给闸门加"有界等待"（`_gate_wait`，在 2s 内轮询复查依赖是否就绪）。
这个修法**能让测试变绿，但方向是错的**：它把"图何时派发下游"这件事交给超时撞运气，
真实 LLM 下一次 `write_ch1` 要跑 5 秒，2 秒窗口根本不够，于是又把等待上限调到 180 秒——
节点在"正在执行"里空转，顺序问题被伪装成性能问题。
更糟的是它掩盖了真因：**坑三修好之后，图的派发顺序本身就是对的，闸门根本不需要等。**

**最终修法**

删掉轮询等待，闸门回归纯同步判定（`_gate`）。它只承担两件框架不负责的事：
① 上游失败 → 写 `BLOCKED` 落库（否则前端看到一堆永远 PENDING 的节点）；
② 断点续跑后按库里的真实状态判断该不该跑。
至于"何时该跑"，交给图——这才是把编排交给框架的意义。

**教训**：当"加个等待/重试"能让测试变绿时，先怀疑是不是别处有更深的一致性错误。
用超时掩盖顺序问题，症状会从"卡住"变成"偶发变慢"，更难查。

### 3.6 坑六：`fix_target=research` 的打回不收敛（重跑评测时才发现）

**这个坑值得单独记，因为它说明了"单测全绿 ≠ 生产正确"。**

**错误现象**

重跑 40 主题评测时，C 档偶尔出现：

```
GraphRecursionError: Recursion limit of 200 reached without hitting a stop condition
```

表现为整条用例 `ERROR`、该主题从统计里消失。它只在 C 档（有 Reviewer）出现，
且只在打回建议是"重新检索"（`fix_target=research`）时触发——
在 40 个主题里命中频率很低，一眼看去像偶发。

**根因**

打回时 `reopen_for_revision` 会把目标节点重置为 PENDING 并 `attempt+1`。
`fix_target=research` 时被重置的是 `research_ch{i}`、`write_ch{i}`、`review_ch{i}` 三个。
但条件边只写了"打回就回到 `write_ch{i}`"——

于是 `research_ch{i}` 被重置成 PENDING 后**再没有任何入边能触发它**
（它在图上唯一的入边来自 `planner`，而 planner 早已完成）。
`write_ch{i}` 的闸门看到依赖还是 PENDING 就不断让路，
`review_ch{i}` 因此反复重试，一路跑到 200 步上限炸掉。

**为什么单测没抓到**：原有的打回测试全部用 `fix_target="write"`
（改稿），从来不重新检索。这是一个"代码路径真实存在、但测试只覆盖了一半"的典型。

**修法**

`reopened` 字段从布尔改成"回到哪一层"的字符串（`"write"` / `"research"` / `""`），
条件边按它分别回边到对应节点：

```python
if action == "research":
    return research_id     # 修复前这里没有分支，只有 write_id
return forward
```

**回归保护**

新增 2 条用例（`test_fix_target_research_terminates_instead_of_looping` /
`..._gives_up_at_cap`），断言"所有节点都到达终态、没有卡 PENDING"。
并做**变异测试**验证有效性：把 `research` 分支注掉，这 2 条立刻失败。
`_ReviseAgents` 也因此加了 `fix_target` 参数，让重检路径以后能被持续覆盖。

**教训**：真实评测不只是"给简历凑个数"。这个 bug 靠 260 条单元测试没发现（当时是 260 条），
是跑真实数据跑出来的。评测的价值一半在于验证质量，另一半在于**逼出只在长链路里
才现形的控制流错误**。

---

## 4. 检索层：三档、RRF、溯源

### 4.1 三档与默认档

| 档 | 实现 | 需要 `EMBEDDING_API_KEY` |
|---|---|---|
| `bm25`（**默认**） | `LocalCorpusRetriever`，行为与改造前逐条一致 | 否 |
| `vector` | `HybridRetriever`，只用向量一路 | 是 |
| `hybrid` | `HybridRetriever`，BM25 + 向量 RRF 融合 | 是 |

默认为什么不改：历史 40 主题基线跑在 BM25 上。默认档一换，历史数字立刻不可比，
而"不可比"不会报错，只会让以后每次评测都含糊。所以三档是**新增能力**，不是静默替换。

新增配置项（`.env.example` 与 `app/core/config.py`）：`RETRIEVAL_MODE`、`EMBEDDING_BASE_URL`、
`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`（默认 `BAAI/bge-m3`）、`EMBEDDING_TIMEOUT`、
`EMBEDDING_BATCH_SIZE`、`RRF_K`（默认 60）、`VECTOR_INDEX_DIR`。

### 4.2 为什么是 RRF

- BM25 分数无界（实测单片段 0.5~30），余弦在 [0,1]，量纲不可比。要加权求和必须先归一化，
  而归一化方法（min-max / z-score / 除以 top1）**会随查询变化**——每次查询换一把尺子。
- RRF 只看名次：`score = Σ 1/(k + rank)`，`rank` 从 1 开始。k=60 是原论文
  （Cormack et al., 2009）与工业界默认值。天然免疫量纲差异，也不用调权重。

`rrf_fuse` 抽成纯函数是为了可测：融合的错法（rank 从 0 开始、k 用错、同一路重复项被重复累加）
**都不会报错，只会让排序悄悄变差**。

**跨层副作用（这条最容易被忽略）**：`budget.py` 与 `researcher._rerank_by_budget` 原先用
"分数乘 0.5"给预算外文档降权。BM25 下相邻名次相对差 20%~50%，乘 0.5 是"降权但仍可能翻盘"；
RRF 相邻名次只差约 1.6%（k=60），字面乘 0.5 会把片段推到 61 名开外，**降权退化成硬过滤**，
与"优先但不独占"的设计意图正好相反。

修法：`researcher.py` 加 `_looks_like_rrf()` 判定（全部分数 ≤ 0.05 视为 RRF 尺度），
RRF 档下改用**名次惩罚** `_RRF_RANK_PENALTY = 30`（`1/(60+1)` 与 `1/(60+31)` 之比约 1.98，
与 0.5 的折扣强度基本对齐）。`DOC_BUDGET` 默认 false，所以这条只在开启预算时触发。

### 4.3 溯源怎么保住的

- **`Snippet.text` 一律从 BM25 的 chunk 表取**。向量索引里那份文本只用于建向量。
  融合只合并"片段身份"（`doc_id\x00page\x00text`，用 `\x00` 分隔防拼接碰撞），
  返回时统一从原文取。**索引文件被手改，输出仍是当前语料的逐字切片。**
- **索引指纹** = 模型名 + 全部 chunk 文本（同样 `\x00` 分隔）。语料一变、模型一换自动作废重建，
  杜绝"新语料配旧向量"。
- **embedding 缓存键含模型名**（与 LLM 缓存刻意不同）：`bge-m3` 换 `bge-large` 是不同向量空间，
  沿用"不含模型名"会静默回放错向量。落 `<llm_cache_dir>/embeddings/`，按单条文本缓存。

### 4.4 三种降级语义不同，不能混

| 情况 | 行为 | 为什么 |
|---|---|---|
| 没配 `EMBEDDING_API_KEY` | 全程按 BM25（`effective_mode` 变 bm25） | 知识库功能不该因为缺一项配置就整体起不来；行为与改造前逐条一致（有测试逐条比对 text 与顺序） |
| replay 模式缓存缺失 | 抛 `EmbeddingReplayMiss`（`no_retry=True`） | **不静默降级**：否则回放结果 ≠ 录制时的混合检索结果，基线就假了 |
| 网络抖动 / 5xx / 超时 | 本次降级 BM25 并告警 | 检索只是素材入口，一次抖动不该让整份报告失败 |

第二条最关键：静默降级会让一次"hybrid 评测"实际跑的是 BM25，而报告上写着 hybrid。

### 4.5 为什么不引 faiss / 向量数据库

22 篇 / 79 片段，79 × 1024 维暴力点积在纯 Python 里是毫秒级，引几十 MB 原生依赖换不来可感知收益；
且只有自己写才能把"每条向量对应哪段原文"**显式**存下来——向量库只保证"能查到某个 id"，
不保证 id 背后的文本没被改过，而这正是溯源的前提。代价认：语料上万片段后会真变慢。

### 4.6 界面显示的是"生效档"，不是"配置档"

前端没有 hybrid 专属 UI（没有"这一条由哪一路召回""两路各自排第几"的可视化），
但顶栏做了一件必须做的事：**显示实际生效的档位**。

`/api/health` 返回的是 `retrieval_effective`（生效档）与 `retrieval_degraded`（降级原因），
不是 `RETRIEVAL_MODE` 的配置值：

| 配置 | 是否有 key | 顶栏显示 |
|---|---|---|
| `RETRIEVAL_MODE=hybrid` | 有 | `hybrid + BAAI/bge-m3` |
| `RETRIEVAL_MODE=hybrid` | 无 | `bm25` + 标注「（已降级）」 |

**为什么非要显示生效档**：`hybrid` 没配 key 时会降级成 `bm25`。界面若照配置显示 hybrid，
会让人以为向量检索在跑、实际在跑关键词——排查"召回为什么没变好"时会白走一大圈，
而且方向完全找错（去查 embedding 服务，而真正的原因是根本没调用它）。

这和 §4.4 的第二条是同一个原则的两个落点：**降级可以发生，但必须说出来**。

---

## 5. 契约与兼容性

### 5.1 节点命名一字未改（硬约束）

`planner` / `research_ch{i}` / `write_ch{i}` / `review_ch{i}` 完全沿用，原因有三个消费方绑着：

| 消费方 | 依赖什么 |
|---|---|
| `spans` 成本归因 | 按 `node_id` 分组 |
| 前端 DagView | 按 `_ch(\d+)$` 正则取列号 |
| exporter | 按 `write_ch{i}` 取正文 |

改一个名字要同时改三个消费方，而且评测脚本会静默失配。所以这条写成了测试
（`test_node_names_are_stable`），不是口头约定。

### 5.2 双写 `TaskStore`

LangGraph 的 state 只做**推进与路由的簿记**；节点 `status` / `output` / `attempt` / `review_round`
在每次变化时写回 `nodes` 表。checkpoint 与 `nodes` 表回答的是不同问题：

- checkpoint：图推进到哪、下一个该跑谁 → 只给续跑逻辑看
- `nodes` 表：这个节点现在什么状态、产出是什么 → 给 SSE、exporter、spans、阻塞判定看

好处是**换编排框架没有改动任何一个消费方**。代价是每处状态变化都要记得写回库，写漏了前端就不动。

### 5.3 两条恢复路径要分清

| 入口 | 判据 | 做法 |
|---|---|---|
| 中途断电 | checkpoint 里还有 `next`（半个超步） | `ainvoke(None, cfg)` 从最后一个 checkpoint 继续 |
| `reset_failed` 重跑 | 图已走到 `END`，没有待执行节点 | 以初始 state 重放，靠 `nodes` 表的 DONE 跳过已完成节点 |

判据就是一行 `bool(snapshot.next)`（`runner._resume_mode`）。**"图负责推进、库负责裁决"**——
这一点如果混了，就会在 `reset_failed` 场景下变成"图没有待执行节点 → 什么都不做"。

`task_id` → `thread_id` 是 `reportflow:<task_id>`，一一映射且**不做哈希**（哈希会带来碰撞风险）。

### 5.4 对外崩溃契约不变

对外仍是 `SystemExit(137)`。内部用 `SimulatedCrash(BaseException)`，理由见坑二。

---

## 6. 验收清单与实测数据

### 6.1 测试

```bash
cd backend
$PY -m pytest -q
# 320 passed, 1 warning
```

那个 warning 是 langgraph 上游的 `LangChainPendingDeprecationWarning`（`allowed_objects` 默认值将来会变），
与本项目代码无关。

大数关系：**改造前基线 139 + 本轮新增 181 = 320 passed**。

其中新增用例的主要落点（只列重点，不复述全部）：`test_graph_langgraph.py` **28 条**
（图构建、并行/串行、阻塞传播、断点续跑、打回闭环、四种拓扑组合矩阵）、
`test_hybrid.py`（RRF 融合、三档切换、降级、缓存、RRF 分数尺度契约）、
`test_vector_index.py`（索引构建、指纹校验、余弦排序、落盘恢复）、
`test_assembly_retrieval.py`（装配层真的把档位接进 `rt.retriever`，
防"工厂单测全绿、生产永远只跑 bm25"）。

**同时删掉了 16 条**：`test_dag.py`（9 条）与 `test_scheduler.py`（7 条）的被测对象已不存在，
覆盖转移到 `test_graph_langgraph.py`。所以"320"不是"139 条原封不动再加 181 条新的"，
而是删旧补新的净结果。

```bash
$PY -m ruff check .
# All checks passed!
```

### 6.2 实测版本

| 包 | 实测版本 |
|---|---|
| `langgraph` | 0.6.11 |
| `langgraph-checkpoint` | 3.0.1 |
| `langgraph-checkpoint-sqlite` | 3.0.3 |
| `langchain-core` | 1.7.0 |

依赖声明：`langgraph>=0.6,<0.7`、`langgraph-checkpoint>=3.0,<4.0`、`langgraph-checkpoint-sqlite>=3.0,<4.0`。

### 6.3 真实端到端（真实 DeepSeek + 真实硅基流动 `bge-m3`）

2 章任务，两种档位各跑一次：

| 档位 | 节点状态 | LLM 调用 | prompt tok | completion tok | 估算成本 |
|---|---|---|---|---|---|
| `bm25` | 5 个节点全 DONE | 5 次 | 4045 | 2756 | ¥0.0123 |
| `hybrid` | 5 个节点全 DONE | 5 次 | 3699 | 2537 | ¥0.0113 |

成本量级与改造前（阶段二实测 5 章 ¥0.031）一致。**这两组数字只证明链路打通与量级合理，
不构成"hybrid 更好"或"更便宜"的结论**——2 章、跑一次、没做配对比较，不具备统计意义。

### 6.4 混合检索确实在起作用

只看"任务成功了"证明不了向量路参与排序。两条硬证据：

1. **79 条向量建成**（22 篇语料切分后的片段数），索引文件落盘并带指纹。
2. **同一 query 下 hybrid 的 top4 第 4 名与纯 BM25 不同**：
   BM25 给 `doc_19_overseas_eu_me`，hybrid 给 `doc_21_outlook`。
   如果向量路没参与，融合结果不可能在两路之一之外产生新的第 4 名。

**谱系验证**：hybrid 返回的每个 `Snippet.text` 都能在原文中逐字命中（不是"看起来像"，
是逐字子串匹配）。这条直接对应 §4.3 的设计约束。

### 6.5 并行/串行/续跑：用时间重叠度实测，不信日志

日志说"并行"不算证。验收用的是**执行区间重叠度**与**是否重跑**这两个客观量：

| 验收点 | 口径 | 实测 |
|---|---|---|
| research 并行 | 同时在跑的 research 节点数上限 | **3**（= 并发上限，5 个 research 分 3+2 两波） |
| write 串行 | 同时在跑的 write 节点数 | **1** |
| review 串行 | 同时在跑的 review 节点数 | **1** |
| 崩溃后续跑 | 崩溃前 DONE 集合 ∩ 续跑执行集合 | **空集**（没有一个已完成节点被重跑） |

崩溃续跑的完整过程：崩溃时已完成 **10 个节点**；续跑只重跑了**孤儿节点及其下游共 6 个**；
最终任务 `DONE`，**16 个节点的输出全部保留**（不是"重新生成了看起来一样的"）。

对应测试：`test_research_parallel_tail_serial`（重叠度断言）、
`test_crash_then_resume_does_not_rerun_done`（集合交集必须为空）、
`test_outputs_survive_restart`（重启前后 16 个 output 完全一致）。

---

## 6.5 40 主题评测重跑（本轮新增）

**这一步是补掉改造前最大的可信度缺口**：README 原先引用的三档数字来自自研编排，
"这些数据对应现在的代码吗"无法回答。现在可以了。

设置：40 主题 × 1 次重复（与 v6 口径一致，可直接比）、`LLM_MODE=live`（真调真实模型）、
`RETRIEVAL_MODE=bm25`（不把向量检索这个新变量混进来）、
生成 `deepseek-chat` / 判官 `zai-org/GLM-5.3-Flash`（跨厂商）。
结果目录 `eval/results/v9-langgraph/`，明细 `cases/` 不入库（可重跑），
`report.md` 入库以便引用数字可查。

| 方案 | 覆盖率（95% CI） | 语义支持率 | 不支持断言 | 必引命中 | 重复率 | 单份成本 | 失败 |
|---|---|---|---|---|---|---|---|
| A · 单 Agent | 73.2% [65.9, 80.6] | 93.3% | 14 | 84.1% | 0.7% | ¥0.0056 | 0 |
| B · 多 Agent | 77.5% [69.6, 85.5] | 98.2% | 1 | 86.9% | 9.2% | ¥0.0204 | 0 |
| C · 多 Agent + Reviewer | 79.3% [71.7, 87.0] | 99.8% | 0 | 92.7% | 8.9% | ¥0.0272 | 0 |

**配对比较**

| 对比 | 覆盖率 | 语义支持率 |
|---|---|---|
| A → B | +4.3pp [−4.7, +13.4]　**不显著** | +4.9pp [+1.5, +8.2]　显著 |
| A → C | +6.1pp [−1.5, +13.7]　**不显著** | +6.4pp [+3.6, +9.3]　显著 |
| B → C | +1.8pp [−4.1, +7.6]　**不显著** | +1.6pp [+0.2, +2.9]　显著 |

**必须如实说明的三点**

1. **"多 Agent 覆盖率更高"这条，本轮测出来不显著**。v6 曾测得 +7.7pp 显著，
   本次 +4.3pp、CI 跨 0。所以 README 已改成"覆盖率提升本轮不显著，不能作为定论"。
   这是本次重跑最有价值的产出之一：它把一条要写进简历的结论从"确定"降级成了"存疑"。
2. **语义支持率才是这个架构的真实收益，且三档都显著**。收益来源是"引用归属正确"——
   分章检索让角标只能落在本章真实用到的来源上，单 Agent 那 14 条不支持断言几乎全是
   "数据是真的、出处挂错了"。
3. **换框架没有让质量出现数量级变化**（三档覆盖率差 1~3pp、语义支持差 1pp 内）。
   这正是"行为等价迁移"要的结果；如果有大落差，说明迁移引入了行为改变，得回去查。

**顺带修正一处文档错误**：评测报告原先硬编码"judge 与生成使用同一模型"，
而项目实际一直配着跨厂商判官。已改为按实际配置动态生成，避免报告与配置自相矛盾。

全文明细：`eval/results/v9-langgraph/report.md`。

---

## 6.6 工具调用（T3）：Function Calling + 联网检索

**目标**：让 Researcher 能自己决定「还要不要查」，并补掉「不具备实时联网检索」这条边界。

**形状**：标准 function calling 循环——模型要求调工具 → 执行 → 以 `role=tool` 回填 →
直到模型不再要工具 → 关掉 tools、打开 `json_mode` 收尾要 JSON。

两个工具：`kb_search`（本地知识库）、`web_search`（互联网）。

### 三条刻意的边界

| 边界 | 做法 | 为什么 |
|---|---|---|
| **默认关闭** | `ENABLE_TOOLS=false`、`ENABLE_WEB_SEARCH=false` | 不传 tools 时请求形状与之前逐字节一致，40 主题基线不受影响；开着会多 1~3 轮调用，联网还会让结果不可复现 |
| **溯源不放松** | 工具返回原文，模型必须逐字引用，`_verify` 照旧做子串匹配 | 工具只扩大材料范围，不降标准 |
| **失败不抛出** | 一律 `ToolResult.ok=False` 回给模型 | 抛异常会让整章崩掉，而问题往往只是"这个站点抓不到" |

### 两个不报错、只让结果悄悄变错的坑（T3 最贵的两个）

**坑一：渲染没打 `doc_id`，网页素材全被丢弃。**
`web_search` 原先只渲染 `[网页1]`，模型就自己编了 `web_1` 当 `source_id`，
结果全部被溯源校验判成「source_id 不在检索结果中」丢弃——**网页明明查到了，
却一条 fact 也用不上**，而且不报错、只是"素材变少了"。
修法：改为与 `kb_search` 一致的 `doc_id=web_xxx` 格式。

**坑二：工具片段没进证据池，网页引用被误判成编造。**
`_verify` 原本只比对预检索片段。模型引用网页内容时，即便查到了也会被判"编造"。
修法：`ToolRegistry` 收集本轮工具的 `items`，`Researcher._evidence_pool()`
把它们合并进证据池后再校验。

这两个坑的共同点：**都不抛异常**，症状都是"素材变少"，很容易被当成
"模型这次检索没查到"而放过。已各加回归测试锁住。

### 搜索源的实测选择（不是拍脑袋）

| provider | 可达性 | 结果质量 | 结论 |
|---|---|---|---|
| DuckDuckGo HTML | **超时**（本机不可达） | — | 放弃（尽管它是惯例做法） |
| Bing | 可达 | **JS 渲染空壳页**，抓到的标题与查询无关 | 放弃 |
| 360（so.com） | 可达 | 真实结果，标题精准命中 | **默认** |

另一个实测约束：360 的结果链接是 `so.com/link?m=...` 跳转，**单独请求返回 400**
（需要搜索页会话，属反爬），"跟随重定向拿真实 URL"这条路走不通。
但结果页每条都带 `data-mdurl`（真实 URL）与 80~150 字原文摘要，
所以最终**不逐页抓取，直接用搜索索引的摘要**：更稳、更快，且摘要本身
就是从原文截取的连续文字，仍满足"逐字可查"。

代价也写清楚：拿不到整页正文，对「抽一句话做引用」够用，
对「通读全文做综述」不够（已写进 README「已知边界」）。

### 缓存键的处理（T3 最隐蔽的风险）

原来照 T3 计划书要求把 `tools` 纳入缓存 key，但加了一个约束：
**只在真的用了工具时才纳入**。这样引入 T3 之前录的那批缓存键仍然有效——
否则整库缓存失效，对靠回放离线开发的人等于凭空多花一笔钱。
同时工具往返消息（`role=tool` 的 content 与 `tool_call_id`）也进键，
否则会出现"工具结果不同却命中同一缓存"这种极难排查的错误。

### 验收

- 工具层 38 条 + 循环/缓存 21 条测试，全离线（不联网、不调真实模型）
- **真实模型实测**：`tool_choice=required` 时模型正确返回
  `kb_search {"query": "新能源配储比例要求", "top_k": 8}`，工具返回 8 条真实片段
- **真实联网实测**：查询「2025 新型储能装机规模 GW」拿到搜狐/北极星/凤凰网等
  正规媒体结果，摘要含可引用的具体数据
- **`tool_choice=auto` 实测**：模型自主决定调用工具，曾一次要求两个
  `web_search`（中文 + 英文查询），策略合理
- 默认配置端到端：5/5 DONE，无工具轮次，与 T3 之前一致
- 全开配置端到端：7/7 DONE

---

## 7. 未完成项（明确记录，不含糊）

1. **~~40 主题评测没在 LangGraph 改造后重跑~~ → 已完成**，见 §6.5。
   注意结论有变化：覆盖率提升**不显著**了，别再引用 v6 的 +7.7pp 当本轮结论。
2. **`vector` / `hybrid` 没有 40 主题三档量化对照**。只有 §6.3 的单主题实测。
   按 `docs/REPEAT_RATE.md` 的先例，如实报告"在你的语料规模下 hybrid 未必更好"同样是有价值的结论，
   所以这一项不能为了好看而调口径。
3. **前端没有 hybrid 专属 UI**。切档靠 `.env` 的 `RETRIEVAL_MODE`。做了的最低限度是顶栏显示
   **实际生效**的档位（`retrieval_effective`，降级时加标记、带 embedding 模型名）——
   这一条是必要的：照配置显示会让人以为向量检索在跑，实际在跑关键词。
   但仍然**没有**"这条片段由哪一路召回 / 两路各自排名"的可视化，排查混合检索效果仍需看日志。
4. **流水线模式（`serial_tail=False`）已修复并覆盖，但"并行度"本身没有专门断言**。
   四种拓扑组合现在都能把每章跑完（`TestTopologyMatrix` 覆盖），坑四的汇合节点归位也已验证；
   缺的是"流水线档下各章确实重叠推进"这类时间重叠度断言——`serial_tail=True` 有
   （research 重叠 3 / write 1 / review 1），流水线档没有对应的量化口径。
5. **`ORCHESTRATOR=langgraph|builtin` 回退开关没做**——这是**用户决策，不是遗漏**。
   原计划保留自研实现作回退，开工前单独确认过，用户明确选择"彻底删除，只保留 LangGraph"，
   理由是"仓库里同时存在两套编排，面试官会质疑主链路到底是哪个"。
   代价也认：这次迁移不能运行时降级，只能回滚到迁移前的 commit。旧实现见 git 历史。
6. **非 sqlite 部署没有 checkpoint**。MySQL 部署下只能整轮重放（靠 `nodes` 表 DONE 跳过），
   失去"半个超步"的中途续跑能力，日志里会明说。
7. **评测只有 1 次重复**（与 v6 保持一致以便对比）。单主题覆盖率波动可达 20pp+，
   所以只看配对 CI，不要看单主题分数。若要更稳，可跑 `--repeats 2`（成本翻倍）。

---

## 8. 复现

```bash
cd backend

# 1) 测试与静态检查
$PY -m pytest -q          # 320 passed
$PY -m ruff check .        # 全仓库，All checks passed

# 2) 单档端到端（bm25 默认，不联网，命中录制缓存时成本 ¥0）
$PY -m app.cli doctor
$PY -m app.cli run --profile real --topic "2026 年中国工商业储能行业" --sections 2

# 3) hybrid 档（需要 .env 里 EMBEDDING_API_KEY）
#    把 RETRIEVAL_MODE 改成 hybrid 后重跑同一条命令
#    首次会建索引：data/vector_index/

# 4) 断点续跑
$PY -m app.cli run   --topic "储能（崩溃演示）" --sections 5 \
    --task-id demo-crash5 --crash-before write_ch3     # exit 137
$PY -m app.cli status --task-id demo-crash5
$PY -m app.cli resume --task-id demo-crash5             # 已完成的节点不重跑
```

checkpoint 落 `data/reportflow.db.checkpoints`，与任务库分开；`data/vector_index/` 已被 `.gitignore` 忽略。
