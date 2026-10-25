# 阶段二验收记录

环境：Windows 11 / Python 3.12.10 / SQLite（WAL）/ 单进程
运行目录：`backend/`，`$PY` 代表 Python 解释器路径

阶段二交付范围：**LLM 网关 + 四个真实 Agent + 交接协议校验 + Reviewer 打回重写 + Span 成本归因**。
MockAgent 保留，`--profile mock|real` 可切换；调度器代码一行未改。

---

## 0. 两种验证方式

阶段二的验证分两条线，**两条都跑过**：

| 方式 | 目的 | 是否花钱 | 见 |
|---|---|---|---|
| 本地假 LLM（`scripts/fake_openai_server.py`） | 验证**链路与故障处理**：重试、降级、429、打回闭环 | 否 | §2–§6 |
| 真实模型（DeepSeek） | 验证**真实产出质量**：引用是否可溯源、大纲是否合理 | 是（约 ¥0.03/5 章） | §7 |

假服务的价值在故障注入：真实模型不会主动返回 429 或 500，而这些分支恰恰最容易写错。
真实模型的价值在产出质量：假服务返回的是脚本，无法回答"模型会不会编造"。

假服务按 system prompt 的 `[task:xxx]` 标记路由，并按 prompt 里给的原文片段构造 JSON，
所以 Researcher 的「quote 必须能在原文中找到」校验是真会被执行的。

---

## 1. 单元测试

```bash
$PY -m unittest discover -s tests -t .
# Ran 50 tests in 5.4s — OK
```

阶段二新增 24 个用例，重点覆盖：

| 覆盖点 | 用例 |
|---|---|
| 引用核验（防幻觉核心） | `test_drops_fabricated_quote`：模型返回 8.4GWh 的正确引用 + 9.1GWh 的编造引用 → 后者被丢弃，`dropped_facts=1` |
| 角标越界 | `test_strips_unknown_citation_and_rebuilds_map`：正文引用 `[^doc_99]` → 角标被剥掉，页码以素材为准 |
| 空素材不乱写 | `test_empty_packet_skips_llm`：素材为空时**不调用模型**，直接输出「资料未涉及」 |
| 程序优先 | `test_numeric_mismatch_caught_without_llm`：数字对不上 → 直接判 revise，`llm.requests == []` |
| 序号不算数据 | `test_chapter_ordinal_not_treated_as_data`：「第 3 章」的 3 不得触发数字不一致 |
| 分流规则不被模型篡改 | `test_defect_fix_target_is_derived_not_trusted`：`missing_evidence` 一律指向 research |
| 网关重试/降级 | `test_retries_transient_then_succeeds` / `test_falls_back_when_primary_exhausted` / `test_raises_when_all_models_fail` |
| 打回闭环收敛 | `test_revise_triggers_rewrite_and_converges` / `test_gives_up_at_cap_and_marks_partial` |

---

## 2. 场景 A：mock profile 回归（阶段一行为不变）

```bash
$PY -m app.cli run --profile mock --topic "储能（mock 回归）" --sections 5 --task-id mock-regress
```

```
profile   : mock（生成这份结果所用的 Agent 组合）
wave-02 并发启动 3 个节点: research_ch1, research_ch2, research_ch3
wave-04 并发启动 1 个节点: review_ch1
任务结束 status=DONE 用时=5.70s 执行节点=16 跳过(已完成)=0
结果：DONE —— 全部节点成功
```

判定：调度行为与阶段一完全一致（3 并行 + 尾部串行），说明引入 LLM 层没有污染编排层。

---

## 3. 场景 B：real profile 端到端 5 章

```bash
# 先灌知识库（3 篇构造文档，24→约 15 个片段）
$PY -m scripts.seed_corpus
# 再起假 LLM（另开一个终端；真实模型跳过这步）
$PY -m scripts.fake_openai_server --port 8999

$PY -m app.cli run --profile real --llm-base-url http://127.0.0.1:8999/v1 \
    --topic "2026 年中国工商业储能行业" --sections 5 --task-id demo-real
```

```
runtime ready profile=real agents={planner=PlannerAgent, research=ResearcherAgent,
  write=WriterAgent, review=ReviewerAgent} concurrency=3 语料=21 片段

[planner] 大纲完成：5 章 | …市场规模与增长 / …区域分布特征 / …竞争格局与集中度 / …
[research_ch1] 素材包：命中 3 片段，保留 2 条 fact，丢弃 0 条（引用无法在原文中找到）
…（5 章同）
任务结束 status=DONE 用时=0.06s 执行节点=16

合计 16/16 完成 | DONE=16
成本归因（spans）:
  LLM 调用 16 次 | prompt 6178 tok | completion 1939 tok | 均延迟 3ms
  估算成本 ¥0.0120 | 降级调用 0 次 | 失败调用 0 次
结果：DONE —— 全部节点成功
```

节点状态（`status --task-id demo-real`，节选）：

```
NODE            TYPE      STATUS    ATT  DEPS                          ms      NOTE
planner         planner   DONE      1    -                             9       output keys: outline,sections,topic
research_ch1    research  DONE      1    planner                       5       output keys: chapter,dropped_facts,key_facts,open_questions
review_ch1      review    DONE      1    write_ch1                     2       verdict=pass round=0
write_ch1       write     DONE      1    research_ch1                  4       output keys: chapter,citation_map,gaps,insufficient_evidence
合计 16/16 完成 | DONE=16
```

导出初稿：

```bash
$PY -m app.cli export --task-id demo-real
# 已导出：data/output/demo-real.md
# 章节 5 段 | 引用来源 2 条
```

```markdown
## 第 1 章 章节正文

- 预计 2026 年工商业储能新增装机将达到 13GWh 左右 [^doc_12_association_report]
- 2025 年我国工商业储能新增装机容量约 8.4GWh [^doc_07_white_paper]

## 引用来源
[^doc_07_white_paper]: doc_07_white_paper P.15 — 2025 年我国工商业储能新增装机容量约 8.4GWh，同比增长 62%…
```

> 内容本身由假服务生成，**不能用它评价生成质量**；能证明的是：检索→引用→角标→页码→导出全链路打通。

---

## 4. 场景 C：Reviewer 打回重写（质量闭环）

假服务开 `FAKE_REVISE_ONCE=1`：每章第一次 review 返回 `revise`，之后 `pass`。

```bash
$PY -m app.cli run --profile real --llm-base-url http://127.0.0.1:8996/v1 \
    --topic "储能（打回重写）" --sections 2 --task-id demo-revise
```

```
START   write_ch1      attempt=1/2
START   review_ch1     attempt=1/2
打回 #1 ← review_ch1：重置 write_ch1, review_ch1（fix_target=write）
START   write_ch1      attempt=2/2          ← 带审稿意见重写
START   review_ch1     attempt=2/2
打回 #1 ← review_ch2：重置 write_ch2, review_ch2（fix_target=write）
START   write_ch2      attempt=2/2
任务结束 status=DONE 用时=0.06s 执行节点=11

review_ch1      review    DONE      2    write_ch1     1    verdict=pass round=1
review_ch2      review    DONE      2    write_ch2     1    verdict=pass round=1
```

落库证据（`revision_hint` 就是传给下一轮的"审稿意见"，不是整段对话历史）：

```
write_ch1  DONE attempt=2  hint: 1. [assertion_mismatch] 断言：…新增装机容量约 8.4GWh
                                 问题：原文只给出区间描述，正文写成了确定值（模拟）
```

**实现要点**：打回不靠给 DAG 加回边（那会成环），而是重置目标节点并让 `attempt+1` ——
幂等键 `(node_id, attempt)` 随之变化，阶段一的"已完成就跳过"保护才不会把重写挡住。
超过 `MAX_REVIEW_ROUNDS` 仍不通过则接受当前稿，任务终态降级为 `PARTIAL`（有用例覆盖）。

---

## 5. 场景 D：主模型故障 → 降级到备用模型

假服务开 `FAKE_FAIL_MODEL=deepseek-chat`（该模型恒定 500）。

```bash
LLM_BACKOFF=0.1 LLM_MAX_RETRIES=2 $PY -m app.cli run --profile real \
  --llm-base-url http://127.0.0.1:8998/v1 --llm-model deepseek-chat --llm-fallback qwen-plus \
  --topic "储能（降级演示）" --sections 1 --task-id demo-degrade
```

```
[planner] LLM 可重试错误 model=deepseek-chat attempt=1/2，0.1s 后重试: 服务端 500
[planner] LLM 重试耗尽 model=deepseek-chat: 服务端 500（降级到下一个模型）
[planner] 已降级到备用模型 qwen-plus
```

```
成本归因（spans）:
  LLM 调用 12 次 | 估算成本 ¥0.0032 | 降级调用 4 次 | 失败调用 8 次
```

判定：4 个节点 × (2 次主模型失败 + 1 次备用成功) = 12 次调用，`degraded=4` 与 `error=8`
全部计入 spans —— "哪次调用降级了、哪次失败了"是可查的，不是猜的。

---

## 6. 场景 E：429 限流 → 退避重试

假服务开 `FAKE_FLAKY_ONCE=1`（每个模型第一次请求返回 429）。

```bash
LLM_BACKOFF=0.1 $PY -m app.cli run --profile real --llm-base-url http://127.0.0.1:8997/v1 \
  --topic "储能（429 重试）" --sections 1 --task-id demo-429
```

```
[planner] LLM 可重试错误 model=deepseek-chat attempt=1/2，0.1s 后重试: 限流 429
成本归因: LLM 调用 5 次 | 降级调用 0 次 | 失败调用 1 次
结果：DONE
```

判定：429 被归类为可重试（`LLMRateLimit`），退避后成功，且**没有触发降级**——
限流不等于模型不可用，换模型只会把限流带到另一家。

---

## 7. 真实模型实跑（DeepSeek）

配置 `.env` 后先自检：

```bash
$PY -m app.cli doctor
```

```
llm_base_url: https://api.deepseek.com/v1
model       : deepseek-chat
llm_api_key : sk-571…a24c
knowledge   : data/docs  →  12 个片段
连通性测试：发送一次最小请求 ……
结果：成功 —— model=deepseek-flash 返回 'OK' (860ms, 9 tok)
```

> `doctor` 发的是真实请求。返回的 model 是 `deepseek-flash` 属正常——服务端把
> `deepseek-chat` 路由到了 flash 后端，不是配置错误。

### 7.1 单章：引用是否真的可溯源

```bash
$PY -m app.cli run --profile real --topic "2026 年中国工商业储能行业" --sections 1 --task-id real-live-01
```

```
任务结束 status=DONE 用时=7.53s 执行节点=4
  LLM 调用 4 次 | prompt 2932 tok | completion 1439 tok | 均耗时 1874ms
  估算成本 ¥0.0072 | 降级 0 次 | 失败 0 次
```

模型返回 10 条 fact，**dropped_facts=0**。为排除"自己的校验自己认"的可能，
用独立脚本拿每条 `quote` 回原文里搜：

```
独立核验：每条 quote 是否真能在原文中找到
  OK  doc_07_white_paper           page=None  2025年我国工商业储能新增装机容量约8.4GWh…
  OK  doc_07_white_paper           page=None  用户侧独立储能占比提升明显，占新增装机的41%。
  OK  doc_12_association_report    page=None  浙江一省占比约22%，广东约19%，江苏约14%。
  …（10/10 全部命中）
```

`page=None` 也是对的：我们喂的是 markdown，没有页码概念，模型没有编一个页码糊弄，
而是把它写进 `open_questions`（"片段未提供各来源的页码信息"）。

### 7.2 五章：完整流水线

```bash
$PY -m app.cli run --profile real --topic "2026 年中国工商业储能行业" --sections 5 --task-id real-live-06
```

```
大纲完成：5 章 | 行业定义、政策框架与市场边界 / 市场规模、装机增长与项目经济性 /
              区域分布与重点省份市场差异 / 竞争格局、商业模式与产业链主体 /
              驱动因素、风险挑战与 2026 年展望
素材包：命中 3~6 片段，保留 5~11 条 fact，丢弃 0 条
任务结束 status=DONE 用时=24.97s 执行节点=16
  LLM 调用 16 次 | prompt 12264 tok | completion 6251 tok | 均耗时 2289ms
  估算成本 ¥0.0310 | 降级 0 次 | 失败 0 次
review_ch1..5  verdict=pass round=0   ← 5 章全部一次通过
```

产物示例（节选，已验证每个角标都能在原文中找到对应片段）：

```markdown
## 工商业储能：收益结构、区域格局与增长节奏

工商业储能项目的经济性首先取决于收益来源的构成。峰谷套利是最主要的收益来源，
占项目收益的七成以上[^doc_07_white_paper]。这一收益结构决定了峰谷价差成为项目
经济性的核心变量[^doc_12_association_report]。……2025年浙江一般工业电价峰谷价差
约0.95元/kWh，广东约0.85元/kWh，江苏约0.72元/kWh[^doc_12_association_report]，
三省价差梯度与各自的装机规模形成了对应关系。
```

### 7.3 真实模型暴露的两个问题（已修 / 已记录）

**① Writer 把缺口清单塞进正文（已修）**

首轮 5 章的正文末尾出现："各片段对应的页码信息、2025年新增装机的绝对基数与 2026 年
预测之间的口径说明、浙江广东江苏以外其余地区的分布数据……资料未涉及。"
一句话罗列七个缺口，读起来像道歉信而不是研报。

修法：prompt 明确"用**一句话**概括缺口，不要把 gaps 复述进正文"。改后全文
"资料未涉及"从平均每章 2–3 处降到**每章 1 处**，且都在段末。

**② 跨章重复引用同一批数字（已记录，属数据规模问题）**

首轮 5 章里有 4 章重复引用 8.4GWh / 55% / 22% / 19% / 14%。
一开始怀疑是大纲重叠，于是加强 Planner 的"维度互斥"约束，第 2 次跑出来的大纲确实
更干净（定义 → 规模 → 区域 → 格局 → 展望），**但正文仍然重复**。

根因坐实：知识库只有 3 篇文档 ≈ 12 个可检索片段，撑不起 5 章各写不同角度。
换大纲没用，因为 Model 只能在同一个素材池里抓。

这不是编排缺陷，也不是 prompt 能修的。正确的处理是：**让评测把"章节间重复率"作为指标**
（阶段三），并用真实语料规模匹配章节数。当前已在 ARCHITECTURE §10 记为已知局限。

> 这也是一次有用的负面结果：如果只看"任务 DONE、引用全部可溯源"就收工，
> 会漏掉"读起来像复读机"这个致命体验问题。指标必须包含冗余度，不能只包含正确性。

---

## 复现全部命令

```bash
cd backend
rm -rf ../data/reportflow.db* ../data/output

$PY -m scripts.seed_corpus

# 四个假服务（不同故障模式），各占一个终端
$PY -m scripts.fake_openai_server --port 8999                          # 正常
FAKE_REVISE_ONCE=1 $PY -m scripts.fake_openai_server --port 8996       # 打回
FAKE_FAIL_MODEL=deepseek-chat $PY -m scripts.fake_openai_server --port 8998  # 降级
FAKE_FLAKY_ONCE=1 $PY -m scripts.fake_openai_server --port 8997        # 429

# 五个场景（命令见上文各节）
$PY -m app.cli ls
```

---

## 阶段二遗留（明确不做）

- **打回只重跑被影响的章节**，跨章一致性仍靠顺序写作（`serial_tail`）保证，没有 Editor 统稿节点。
- **`fix_target=research` 的处理是"换检索线索再检一次"**，没有实现 query 改写器；
  连续两次仍无素材时按"资料未涉及"交付。
- **检索是 BM25 关键词匹配**，同义改写召不回来。是否换向量检索，等阶段三评测数据说话。
- **成本单价是配置的示例值**（默认 1 元/3 元每百万 token），不是真实账单；接真实厂商后按价目表改 `.env`。
- 打回闭环的"质量提升幅度"无法在假服务上验证，必须等阶段三用真实模型 + 标注评测集。
