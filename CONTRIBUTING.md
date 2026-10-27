# 贡献指南

感谢你想改这个项目。下面几件事如果提前知道，能省掉大量来回沟通。

---

## 1. 起环境

```bash
make install-dev          # 后端依赖 + 前端依赖
cp .env.example .env      # 至少填 LLM_API_KEY
make dev                  # 后端 8000 + 前端 5173
```

没有 API Key 也能开发：`.env` 里设 `LLM_MODE=replay`，会回放 `data/llm_cache/` 里录制的响应，毫秒级、结果确定、不花钱。第一次真调后会自动录制。

验证环境：

```bash
cd backend && python -m app.cli doctor
```

---

## 2. 分层约束（最重要的一条）

```
contracts  ←  core  ←  {persistence, agents, rag, llm, evaluation}  ←  orchestrator  ←  {assembler, cli, api}
```

- `contracts/` 是**叶子层**：只定义跨层契约，零内部依赖。
- 每一层只能依赖它左边的层，**不能反向依赖，也不能跨层横向依赖**。
- `api/` 不含业务逻辑，只做协议转换。

这条约束由 `backend/tests/test_layering.py` 用 AST 强制校验，越界直接测试失败。

**为什么这么严**：这个项目最容易被破坏的性质是「可独立替换与评测」。一旦 Agent 直接 import 了接口层，评测就再也跑不起来，而这件事在代码评审时几乎看不出来。

---

## 3. 改代码前先知道的三条

| 约束 | 原因 |
|---|---|
| 引用必须能溯源 | Researcher 的 `quote` 必须能在原文逐字找到，否则丢弃。改检索逻辑不能绕过这条 |
| 程序校验优先于模型判断 | 能用代码判定的（数字是否在原文出现）就不要调 LLM —— 又快又准又免费 |
| 改 prompt 必须跑评测 | 主观感觉"变好了"不算数。40 主题评测集会给出覆盖率与置信区间 |

```bash
cd backend && python -m pytest -q              # 全量测试（用项目自带的 .venv）
cd eval && python run_eval.py --arm B          # 单档评测
```

---

## 4. 代码风格

- Python：ruff（`line-length = 100`，规则见 `backend/pyproject.toml`）
- 提交前：`pre-commit install`，之后每次 commit 自动格式化

```bash
make lint     # 只检查
make fmt      # 自动修复 + 格式化
```

测试约定：**测试函数是同步的**，内部用 `asyncio.run()` 驱动图执行（`runner.run()`）。因此不需要 pytest-asyncio，也请不要引入它。

---

## 5. Commit 规范

采用 Conventional Commits：

```
feat(researcher): 检索结果按文档去重
fix(orchestrator): barrier 汇合漏用导致半数章节未执行
docs(readme): 补充 Docker 启动方式
refactor(contracts): 抽出 NodeSpec
test(graph): 补断点续跑不重跑的用例
```

类型：`feat` / `fix` / `docs` / `refactor` / `test` / `chore` / `perf`

---

## 6. 新增一个 Agent

1. 在 `backend/app/agents/` 下实现，继承 `app/agents/base.py` 的基类
2. 输入输出类型定义在 `contracts/agent.py`（不要在实现里另立结构）
3. 在 `assembler.py` 的 `build_runtime` 里注册
4. 在 `orchestrator/graph_langgraph.py` 的 `build_report_graph`（节点清单）与 `compile_report_graph`
   （StateGraph 的节点与边）里各加一处：前者是落库契约，后者是执行拓扑，**两边不能只改一边**
5. 若节点会写入新字段，`ReportState` 上要加带 reducer 的声明（并行分支写同一字段时必须 `Annotated`，
   否则后写的覆盖先写的）
6. 补测试，并确认 `test_layering.py` 仍然通过

> 新增节点时**不要改已有节点的命名**。`planner` / `research_ch{i}` / `write_ch{i}` / `review_ch{i}` 是跨系统契约：
> spans 按 `node_id` 归因成本、前端 DagView 按 `_ch(\d+)$` 正则取列、exporter 按 `write_ch{i}` 取正文。
> 改一个名字要同时改三个消费方，而且评测脚本会静默失配。

> 也**不要把 retriever / gateway / registry 放进 state**。checkpoint 要 JSON 序列化落盘，
> 对象进 state 要么直接炸，要么反序列化出一个「看起来能用但连接已死」的副本。这类依赖一律用闭包传。

---

## 7. 提交 PR 前

- [ ] `make test` 通过
- [ ] `make lint` 通过
- [ ] 若改了 prompt / 检索 / 编排：附上评测结果对比
- [ ] 若改了接口：更新 README 的接口表
- [ ] 更新 `CHANGELOG.md` 的 Unreleased 段
