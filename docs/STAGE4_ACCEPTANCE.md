# 阶段四验收记录：HTTP 接口 + 前端

环境：Windows 11 / Python 3.12.10 / FastAPI 0.104.1 + uvicorn 0.24.0 / Vue 3.5 + Vite 6.4
后端 `http://127.0.0.1:8000`，前端 `http://localhost:5173`（vite 代理 `/api`）

---

## 0. 范围

阶段四只做两件事：**把能力通过 HTTP 暴露出去**，以及**做一个能证明"引用可溯源"的界面**。

刻意没做的：通用仪表盘、任务管理（删除/重命名）、多用户、鉴权。
这些都跟"多 Agent 值不值"这个核心问题无关，做了只是把界面变大。

---

## 1. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 模式、判官、知识库片段数、review 循环开关 |
| POST | `/api/tasks` | 建任务 `{topic, sections, task_id?, review?}` |
| POST | `/api/tasks/{id}/run` | 后台执行（幂等：已 DONE 的节点不重跑） |
| GET | `/api/tasks` | 任务列表 |
| GET | `/api/tasks/{id}` | 任务 + 节点 + 成本概览 |
| GET | `/api/tasks/{id}/stream` | SSE：`task` / `node` / `done` |
| GET | `/api/tasks/{id}/export` | 拼好的 markdown |
| GET | `/api/tasks/{id}/trace` | 每章真实用到的素材（溯源面板的数据） |
| GET | `/api/tasks/{id}/check` | 发布前体检结果 |
| GET | `/api/tasks/{id}/cost` | 成本归因（总计 + 按节点 + 回放命中数） |
| GET | `/api/corpus/{doc_id}` | 一篇文档的全文 |

### SSE 为什么用轮询

不是给调度器加回调，而是**每 300ms 读一次数据库，只在有变化时推事件**。

理由是崩溃语义：调度器完全不知道 HTTP 的存在，进程被杀后前端重新连上就能继续看到进度，
不需要任何"事件重放"或"订阅恢复"。代价是 300ms 粒度——对进度条完全够用。

### 回放命中数为什么从 spans 算

第一版读网关内存的 `cached_calls`，结果**换一次进程就归零**：
一个明明全靠回放跑出来的任务，重启服务后显示"缓存命中 0 次、成本 ¥0"，
看起来像"真实调用但没花钱"。改成从 spans 统计（回放命中的特征很硬：延迟 0 但有 token 用量），
跨进程、跨重启都正确。

---

## 2. 界面

三个视图，只服务一个点：**每个角标都能点开看到原文**。

- **正文**：`[^doc_04]` 渲染成可点角标；体检发现问题的角标标红
- **引用溯源**：列出每章真实用到的素材原文，可逐条"看原文"
- **体检**：哪条断言在所引原文里找不到，并提示"其他含这些数字的文档"
- **成本面板**（本轮新增）：总调用/总成本/降级次数 + 按节点归因 + 回放命中数

> 没有引入图布局库：DAG 形状固定（planner → 并行 research → 串行 write），
> 三行坐标自己算就行，为 20 个方块装一个库不值得。
> 成本面板刻意显示"回放命中 N 次"——在 `LLM_MODE=auto` 下大部分调用不花钱，
> 只显示 ¥0.0000 会让人以为"这套系统免费"，其实是缓存命中。数字小可以，口径不能骗人。

---

## 3. 复现

```bash
# 后端
cd backend
python -m app.api --port 8000          # 或 --reload

# 前端
cd frontend
npm install
npm run dev                            # http://localhost:5173
npm run build                          # 产物 dist/，189 KB（gzip 80 KB）
```

接口自测（本机有代理，需要绕过）：

```python
import httpx
c = httpx.Client(trust_env=False)      # 关键：否则 POST 会被本机代理拦成 502
B = "http://127.0.0.1:8000/api"
tid = c.post(f"{B}/tasks", json={"topic": "工商业储能", "sections": 3}).json()["task_id"]
c.post(f"{B}/tasks/{tid}/run")
```

---

## 4. 实测输出

一次完整链路（3 章，主题命中录制缓存，因此成本 ¥0）：

```
create  → {"task_id": "a307aadc43d7", "created": true}
run     → {"started": true}
status  → DONE，7 个节点
export  → 3100 字
trace   → 每章素材数 [6, 7, 7]
corpus  → doc_03_scale_overall，261 字
check   → 18 条断言，支持 18，不支持 0，clean
cost    → 7 次调用，¥0.0000，缓存命中 7 次，降级 0
```

真实调用任务的归因（对照）：

| 任务 | 调用 | 成本 | 回放命中 | 最贵节点 |
|---|---|---|---|---|
| `dev-norev`（3 章，无审稿） | 8 | ¥0.0171 | 0 | write_ch2 ¥0.0047 |
| `dev-rev`（3 章，含审稿） | 10 | ¥0.0213 | 0 | research_ch3 ¥0.0033 |
| `evalC-top-manufacturers`（5 章 + 打回） | 18 | ¥0.0327 | 0 | write_ch3 ¥0.0089 |

---

## 5. 本轮改掉的三个问题

| 问题 | 症状 | 处理 |
|---|---|---|
| `uvicorn.run` 的 factory 参数反了 | 服务起不来：`missing 3 required positional arguments` | 只有 `--reload` 时才用字符串 + factory |
| TestClient 未用上下文管理器 | `/run` 起的后台任务在请求返回时就没了，状态永远 RUNNING | 用 `with TestClient(...)`，让事件循环活着 |
| 本机 HTTP 代理拦截本机请求 | POST 返回 502 `upstream connect failed` | 调本机服务用 `trust_env=False` / `--noproxy '*'`，并写进文档 |

另有一处顺手做的重构：markdown 拼装抽到 `app/exporter.py`，CLI 与 HTTP 共用。
分开实现必然在某次改动后悄悄分叉——用户会看到两份不同的稿子。

---

## 6. 测试

接口测试 11 条（`tests/test_api.py`），全量 **114 条通过**。

覆盖：健康检查、建任务/幂等、`--review` 开关是否真的加了 review 节点、
404 路径、空 topic 拒绝、导出、溯源、成本端点结构、SSE 三类事件序列。

SSE 用例特意加了行数上限防御：万一 `done` 事件不来，测试要失败而不是挂死——
流式接口最容易以"卡住"而不是"报错"的形式暴露问题。

---

## 7. 已知局限

- **单实例假设**：`running` 字典只防同一个进程内的并发驱动，多副本部署仍会重复调度同一任务
- **无鉴权**：接口裸奔，只适合本机/内网使用
- **前端只做了必要的一屏**：没有历史对比、没有多任务并行视图、没有导出为文件
- **成本面板在 `auto` 模式下显示 ¥0**：这是回放不是免费，界面已注明，但仍需用户自己注意
