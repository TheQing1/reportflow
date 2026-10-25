# 阶段一验收记录

环境：Windows 11 / Python 3.12.10 / SQLite（WAL）/ 单进程
运行目录：`backend/`（本文命令均在此目录下执行，`$PY` 代表 Python 解释器路径）

```bash
cd backend
PY=/path/to/python          # 例：E:/Python/python.exe
```

阶段一交付范围：orchestrator（DAG 调度 + 状态落库 + 断点续跑）+ MockAgent。
**无 LLM 调用、无评测、无前端**（按阶段闸门执行）。

---

## 0. 单元测试

```bash
$PY -m unittest discover -s tests -t . 
# Ran 26 tests in 5.2s — OK
```

覆盖：图校验（环/悬空依赖/重复 id）、5 章并行度、串行链、失败阻塞传播、重试耗尽、
崩溃续跑不重跑、产出跨重启一致、存储原子性、**依赖方向（AST 扫描）**。

---

## 场景一：5 章任务，3 个 research 并行 + write/review 串行

```bash
$PY -m app.cli run --topic "2026 年中国工商业储能行业" --sections 5 --task-id demo-parallel
```

DAG 拓扑（16 个节点，`write_chN` 依赖 `research_chN` + `review_ch(N-1)`）：

```
  planner          type=planner   deps=[-]
  research_ch1     type=research  deps=[planner]
  research_ch2     type=research  deps=[planner]
  research_ch3     type=research  deps=[planner]
  research_ch4     type=research  deps=[planner]
  research_ch5     type=research  deps=[planner]
  write_ch1        type=write     deps=[research_ch1]
  review_ch1       type=review    deps=[write_ch1]
  write_ch2        type=write     deps=[research_ch2, review_ch1]
  review_ch2       type=review    deps=[write_ch2]
  ...
```

关键日志（并发上限 3）：

```
wave-01 并发启动 1 个节点: planner
wave-01 结束 | DONE=1 RUNNING=0 PENDING=15
wave-02 并发启动 3 个节点: research_ch1, research_ch2, research_ch3
START   research_ch1   attempt=1/2 timeout=120.0s
START   research_ch2   attempt=1/2 timeout=120.0s
START   research_ch3   attempt=1/2 timeout=120.0s
DONE    research_ch2   657ms
DONE    research_ch1   657ms
DONE    research_ch3   719ms
wave-02 结束 | DONE=4 RUNNING=0 PENDING=12
wave-03 并发启动 3 个节点: research_ch4, research_ch5, write_ch1
...
DONE    write_ch1      530ms
DONE    research_ch5   671ms
DONE    research_ch4   702ms
wave-03 结束 | DONE=7 RUNNING=0 PENDING=9
wave-04 并发启动 1 个节点: review_ch1
...
任务结束 status=DONE 用时=5.70s 执行节点=16 跳过(已完成)=0
```

**判读**

| 验收点 | 证据 |
|---|---|
| 3 个 research 并行 | `wave-02` 三条 `START` 时间戳相差 1ms 内，耗时 657~719ms 相互重叠；5 个 research 分 3+2 两波 |
| write / review 串行 | `wave-04` 起每波只启动 1 个节点，`write_ch1 → review_ch1 → write_ch2 → ...` 严格交替，无任何两个 write/review 节点重叠（单测 `test_research_parallel_tail_serial` 用时间区间重叠度断言：research 最大重叠 = 3，write = 1，review = 1） |
| 状态落库 | 任务结束打印 16 个节点的 STATUS/ATT/耗时全表（见 `status` 输出） |

---

## 场景二：第 2 章失败 → 下游 BLOCKED 且不执行

```bash
$PY -m app.cli run --topic "储能（第2章故障注入）" --sections 5 \
    --task-id demo-fail --fail-node research_ch2
# exit code = 2 (PARTIAL)
```

```
wave-02 并发启动 3 个节点: research_ch1, research_ch2, research_ch3
FAILED  research_ch2   attempt=1 296ms → RETRYING(attempt=2, +1.0s) | MockInjectedFailure: injected failure on research_ch2 (attempt=1)
wave-02 结束 | DONE=3 RUNNING=0 PENDING=12 RETRYING=1 BLOCKED=0 FAILED=0
wave-03 并发启动 3 个节点: research_ch4, research_ch5, write_ch1
wave-04 并发启动 2 个节点: research_ch2, review_ch1        ← 退避 1s 后重试
FAILED  research_ch2   attempt=2 重试已耗尽 → FAILED | MockInjectedFailure: injected failure on research_ch2 (attempt=2)
wave-04 结束 | DONE=7 RUNNING=0 PENDING=8 RETRYING=0 BLOCKED=0 FAILED=1
BLOCKED write_ch2      ← 上游失败: research_ch2
BLOCKED review_ch2     ← 上游失败: write_ch2
BLOCKED write_ch3      ← 上游失败: review_ch2
BLOCKED review_ch3     ← 上游失败: write_ch3
BLOCKED write_ch4      ← 上游失败: review_ch3
BLOCKED review_ch4     ← 上游失败: write_ch4
BLOCKED write_ch5      ← 上游失败: review_ch4
BLOCKED review_ch5     ← 上游失败: write_ch5
任务结束 status=PARTIAL 用时=2.03s 执行节点=9 跳过(已完成)=0
```

节点状态表：

```
NODE            TYPE      STATUS    ATT  DEPS                          ms      NOTE
planner         planner   DONE      1    -                             283     output keys: mock,outline,topic
research_ch1    research  DONE      1    planner                       667     ...
research_ch2    research  FAILED    2    planner                       315     MockInjectedFailure: injected failure on research_ch2
research_ch3    research  DONE      1    planner                       713     ...
research_ch4    research  DONE      1    planner                       681     ...
research_ch5    research  DONE      1    planner                       677     ...
review_ch1      review    DONE      1    write_ch1                     318     ...
review_ch2      review    BLOCKED   1    write_ch2                     -       上游失败: write_ch2
review_ch3      review    BLOCKED   1    write_ch3                     -       上游失败: write_ch3
review_ch4      review    BLOCKED   1    write_ch4                     -       上游失败: write_ch4
review_ch5      review    BLOCKED   1    write_ch5                     -       上游失败: write_ch5
write_ch1       write     DONE      1    research_ch1                  519     ...
write_ch2       write     BLOCKED   1    research_ch2,review_ch1       -       上游失败: research_ch2
write_ch3       write     BLOCKED   1    research_ch3,review_ch2       -       上游失败: review_ch2
write_ch4       write     BLOCKED   1    research_ch4,review_ch3       -       上游失败: review_ch3
write_ch5       write     BLOCKED   1    research_ch5,review_ch4       -       上游失败: review_ch4
合计 7/16 完成 | DONE=7  FAILED=1  BLOCKED=8
```

**判读**

- 第 2 章 `research_ch2` 用尽 1 次重试（`ATT=2`）后进入终态 `FAILED`；
- 其下游 8 个节点全部 `BLOCKED`，且**一次都没被执行**（`执行节点=9` 正好等于 planner + 5 research + write/review_ch1 + 重试 1 次 = 9）；
- 兄弟章节不受影响：`research_ch1/3/4/5` 与 `review_ch1` 正常 DONE；
- 任务终态 `PARTIAL`（保留已完成章节），不是 `FAILED`——失败被局部化，这是长任务该有的行为。

---

## 场景三：进程重启后已完成节点不重跑

**Step 1 — 模拟崩溃**（`--crash-before` 让节点置 RUNNING 后直接退出进程）：

```bash
$PY -m app.cli run --topic "储能（崩溃演示）" --sections 5 \
    --task-id demo-crash --crash-before write_ch3
# exit code = 137（进程被杀）
```

```
wave-05 结束 | DONE=9 RUNNING=0 PENDING=7 RETRYING=0 BLOCKED=0 FAILED=0
wave-06 结束 | DONE=10 RUNNING=0 PENDING=6 RETRYING=0 BLOCKED=0 FAILED=0
ERROR  模拟进程崩溃：节点 write_ch3 已置 RUNNING 后立即退出
```

**Step 2 — 查看崩溃现场**：

```bash
$PY -m app.cli status --task-id demo-crash
```

```
status    : RUNNING            ← 进程死了，任务还停在 RUNNING（孤儿）
...
write_ch2       write     DONE      1    research_ch2,review_ch1       587     ...
write_ch3       write     RUNNING   1    research_ch3,review_ch2       -       ← 崩溃瞬间正在跑
write_ch4       write     PENDING   1    research_ch4,review_ch3       -
合计 10/16 完成 | PENDING=5  RUNNING=1  DONE=10
```

**Step 3 — 续跑**：

```bash
$PY -m app.cli resume --task-id demo-crash
# exit code = 0
```

```
WARNING 恢复 1 个孤儿节点（原进程已退出，重置为 PENDING）
INFO    断点续跑：10 个节点已是 DONE，本轮不再执行
INFO    wave-01 并发启动 1 个节点: write_ch3
INFO    START   write_ch3      attempt=1/2 timeout=120.0s
INFO    wave-01 结束 | DONE=11 RUNNING=0 PENDING=5
...
INFO    任务结束 status=DONE 用时=2.67s 执行节点=6 跳过(已完成)=10
```

```
合计 16/16 完成 | DONE=16
结果：DONE —— 全部节点成功
```

**判读**

| 验收点 | 证据 |
|---|---|
| 已完成节点不重跑 | `跳过(已完成)=10`，本轮 `执行节点=6`（write_ch3 + review_ch3 + write/review_ch4 + write/review_ch5）；单测 `test_crash_then_resume_does_not_rerun_done` 用"执行记录集合与崩溃前 DONE 集合取交集必须为空"断言 |
| 从断点继续 | 孤儿 `write_ch3`（RUNNING）被重置为 PENDING 并**沿用 attempt=1**，因此幂等键不变 |
| 产出不丢 | 单测 `test_outputs_survive_restart` 断言重启前后 16 个节点 output 完全一致 |

---

## 复现全部三条命令（干净环境）

```bash
cd backend
rm -rf ../data                      # 清空 SQLite（可选）

$PY -m app.cli run   --topic "2026 年中国工商业储能行业" --sections 5 --task-id demo-parallel
$PY -m app.cli run   --topic "储能（第2章故障注入）" --sections 5 --task-id demo-fail --fail-node research_ch2
$PY -m app.cli run   --topic "储能（崩溃演示）" --sections 5 --task-id demo-crash --crash-before write_ch3
$PY -m app.cli status  --task-id demo-crash
$PY -m app.cli resume  --task-id demo-crash
$PY -m app.cli ls
```

退出码约定：`0` = DONE，`2` = PARTIAL，`137` = 被注入的崩溃（非 0 即异常，便于脚本判定）。

---

## 阶段一遗留（明确不做，留到后续阶段）

- 节点并发是"波次"推进（一波跑完再判下一批），一个慢节点会拖住同波的其他节点释放槽位；
  真实 LLM 延迟方差大时可改为完成即补位（`asyncio.wait(FIRST_COMPLETED)`）。
- 无取消/超时熔断（文档 §6.2 的三个非正常结束），阶段四接 API 时统一做。
- `PARTIAL` 任务重跑需显式 `--reset-failed`；没有自动"只补跑失败分支"。
- MockAgent 的 review 恒 pass，真实打回逻辑在阶段二。
