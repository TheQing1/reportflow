"""LangGraph 图构建：把「写一份 N 章研报」翻译成 StateGraph。

执行拓扑（serial_tail=True，with_review=True）：

    planner ──Send──> research_ch1..N            （并行，受并发闸约束）
                          │  （barrier：全部完成才放行）
                          ▼
    write_ch1 → review_ch1 ─pass→ write_ch2 → review_ch2 ─pass→ … → review_chN → END
        ▲            │                  ▲            │
        └───revise───┘                  └───revise───┘

三条必须靠「对的图原语」而不是想当然的边来实现的规则：

1. **汇合必须用 barrier 边 `add_edge([...], target)`。**
   LangGraph 的普通边是 OR 触发：一个节点有多条入边时，任意一条上游完成就会把它
   放出去，另一条还没好也照跑。第一版想用"节点体里检查依赖、没齐就让路"来补，
   结果是让路本身也会继续往下触发边，最后整张图可能在真正的节点还是 PENDING 时
   就走到 END（验收里表现为"任务 DONE 但有一半章节没写"）。
   barrier 边是 LangGraph 原生的 wait-for-all，用它把 N 个 research 收到一点，
   之后全部走严格单触发链，就没有任何"靠运气"的顺序问题了。

2. **打回闭环用条件边回边，不复用旧实现的 attempt hack。**
   旧调度器把 review 打回翻译成"重置节点 + attempt+1"再靠 DAG 重算，是因为
   DAG 不能有环。StateGraph 允许成环，直接 review_ch{i} → write_ch{i} 即可；
   attempt 只用来区分"第几次尝试"，不再承担控制流的职责。

3. **TaskStore 仍是唯一事实来源。**
   图的 state 只做推进与路由用的簿记，节点的 status/output/attempt/review_round
   一律在每次状态变化时写回 nodes 表——前端 SSE、exporter、spans 成本归因全部
   继续读库，换编排框架不改变这些消费方的行为。

为什么 state 里绝不放 retriever / gateway / registry：
checkpoint 要 JSON 序列化落盘，把这类对象放进 state 会在序列化时炸，或者更糟——
反序列化出一个"看起来能用但连接已死"的副本。它们一律用闭包传（见
`compile_report_graph` 的参数），state 只放可 JSON 化的纯数据。
"""

from __future__ import annotations

import asyncio
import json
import operator
import re
import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, Send

from ..contracts.agent import AgentContext, AgentRegistry
from ..contracts.nodes import FAILURE_STATES, Graph, NodeSpec, NodeStatus, NodeType
from ..contracts.task import NodeState, TaskStore
from ..contracts.tracing import span_context
from ..core.logging import get_logger
from ..core.timeutil import utcnow

_CH_SUFFIX = re.compile(r"_ch(\d+)$")

#: 依赖还在这些状态里 = 还没结论，本节点应当让路
_WAITING = frozenset({NodeStatus.PENDING, NodeStatus.RUNNING, NodeStatus.RETRYING})

#: 打回上限用尽仍未通过的章节。只作为"降级依据"传给 runner，
#: 真正的落库仍然写在 nodes 表的 review_round 与任务终态上。
_UNRESOLVED_KEY = "unresolved"


class SimulatedCrash(BaseException):
    """断点续跑验收用的"断电"信号。

    刻意继承 BaseException 而不是 Exception：它是**进程级**事故，不是节点失败，
    绝不能被节点内的重试循环或 RetryPolicy 当成可重试错误吞掉。
    也不用 SystemExit：SystemExit 会被 asyncio 特殊处理（任务被取消，
    冒出来的是 CancelledError），调用方拿到的类型会变得不可预测。
    """

    exit_code = 137


def _merge_dict(left: dict[Any, Any], right: dict[Any, Any]) -> dict[Any, Any]:
    """并行分支各写各的章，必须按键合并而不是整表覆盖。"""
    return {**left, **right}


class ReportState(TypedDict, total=False):
    """图的 state。

    只放可 JSON 序列化的簿记数据。真正的产出以 nodes 表为准；
    这里的字段是给条件边判断"这一章审过了没有、要不要回写"用的。
    """

    topic: str
    sections: int
    outline: list[dict[str, Any]]
    #: 章号(str) -> research 产出。并行写入，必须带 reducer
    findings: Annotated[dict[str, dict[str, Any]], _merge_dict]
    #: 章号(str) -> write 产出
    chapters: Annotated[dict[str, dict[str, Any]], _merge_dict]
    #: 章号(str) -> review 产出
    reviews: Annotated[dict[str, dict[str, Any]], _merge_dict]
    #: 章号(str) -> 已打回轮数
    review_round: Annotated[dict[str, int], _merge_dict]
    #: 章号(str) -> 本次 review 是否刚打回（条件边据此决定回写还是进下一章）
    reopened: Annotated[dict[str, bool], _merge_dict]
    #: 打回次数用尽仍未通过的章节（任务终态要降级为 PARTIAL）
    unresolved: Annotated[list[str], operator.add]
    #: 节点最终失败的原因，便于事后归因
    errors: Annotated[list[str], operator.add]


def _retryable(exc: BaseException) -> bool:
    """哪些异常值得重试。

    默认全重试，只有两类例外：
    1. `no_retry = True` 的异常（见 llm/cache.py 的 ReplayMiss）：缓存缺失是
       确定性缺失，重试只会白等退避时间；
    2. BaseException 家族（SimulatedCrash / SystemExit / CancelledError）：
       那是进程级事故，重试等于把"断电"演成"卡住"。
    """
    if getattr(exc, "no_retry", False) is True:
        return False
    return isinstance(exc, Exception)


# --------------------------------------------------------------------------- 节点清单


def build_report_graph(
    *,
    topic: str,
    sections: int,
    max_retries: int = 1,
    timeout_s: float = 120.0,
    serial_tail: bool = True,
    with_review: bool = True,
    doc_budgets: dict[int, list[str]] | None = None,
) -> Graph:
    """产出落库用的节点清单（NodeSpec 列表）。

    这份 Graph 是**持久化契约**：nodes 表的 deps 列被前端 DagView 用来分列、
    被失败阻塞判定用来找下游，所以它的形状必须与历史版本一致。
    真正的执行拓扑由 `compile_report_graph` 从同一份清单推导，两者不会漂移。
    """
    if sections < 1:
        raise ValueError("sections 至少为 1")

    nodes: list[NodeSpec] = [
        NodeSpec(
            id="planner",
            type=NodeType.PLANNER,
            payload={"topic": topic, "sections": sections},
            max_retries=max_retries,
            timeout_s=timeout_s,
        )
    ]

    for i in range(1, sections + 1):
        nodes.append(
            NodeSpec(
                id=f"research_ch{i}",
                type=NodeType.RESEARCH,
                deps=("planner",),
                payload={
                    "chapter": i,
                    "title": f"第 {i} 部分",
                    "queries": [],
                    # 每章的文档预算：让五章的素材尽量不重叠（见 rag/budget.py）
                    "doc_budget": list((doc_budgets or {}).get(i) or []),
                },
                max_retries=max_retries,
                timeout_s=timeout_s,
            )
        )

    prev_tail: str | None = None
    for i in range(1, sections + 1):
        write_deps = [f"research_ch{i}"]
        if serial_tail and prev_tail:
            write_deps.append(prev_tail)
        nodes.append(
            NodeSpec(
                id=f"write_ch{i}",
                type=NodeType.WRITE,
                deps=tuple(write_deps),
                payload={"chapter": i},
                max_retries=max_retries,
                timeout_s=timeout_s,
            )
        )
        if with_review:
            nodes.append(
                NodeSpec(
                    id=f"review_ch{i}",
                    type=NodeType.REVIEW,
                    deps=(f"write_ch{i}",),
                    payload={"chapter": i},
                    max_retries=max_retries,
                    timeout_s=timeout_s,
                )
            )
            prev_tail = f"review_ch{i}"
        else:
            # 评测 B 组（多 Agent 无 Reviewer）：串行链改挂在 write 自己身上，
            # 保证与 C 组只有"有没有 review"这一个变量不同
            prev_tail = f"write_ch{i}"

    graph = Graph.of(nodes)
    graph.validate()
    return graph


# --------------------------------------------------------------------------- 节点执行器


class NodeRunner:
    """节点执行器：把"调 Agent + 同步 TaskStore + 重试"封装成可复用的闭包。

    闭包持有 store/registry/semaphore，**不进 state**——checkpoint 只会序列化 state，
    这些对象一旦进 state 就必然在落盘时炸。
    """

    def __init__(
        self,
        *,
        task_id: str,
        owner: str,
        store: TaskStore,
        registry: AgentRegistry,
        sem: asyncio.Semaphore,
        crash_before: str | None,
        retry_backoff_s: float,
        counters: dict[str, int],
        log: Any,
    ) -> None:
        self.task_id = task_id
        self.owner = owner
        self.store = store
        self.registry = registry
        self.sem = sem
        self.crash_before = crash_before
        self.retry_backoff_s = retry_backoff_s
        self.counters = counters
        self.log = log
        self._tid = task_id[:8]

    # ------------------------------------------------------------ 读库

    def _nodes(self) -> dict[str, NodeState]:
        return {n.node_id: n for n in self.store.list_nodes(self.task_id)}

    def _load(self, node_id: str) -> NodeState | None:
        return self._nodes().get(node_id)

    def _gate(self, spec: NodeSpec) -> tuple[str, str]:
        """执行前检查依赖：(动作, 原因)，动作为 run / skip / block。

        LangGraph 已经用 barrier 边保证了主要顺序，这里保留闸门的理由有两条：
        1. 断点续跑后依赖的真实状态只有库知道（图的 checkpoint 不记录这个）；
        2. 上游失败时下游不能被执行——阻塞传播必须在库里留痕（BLOCKED），
           否则前端会看到一堆永远 PENDING 的节点。

        为什么不做"依赖未就绪就永久放弃"：图的边语义与库里的 deps 不是一一对应的
        （比如 with_review=False 的 B 组，图上 write_ch{i} 的前驱是 write_ch{i-1}，
        而库里 deps 还列着 research_ch{i}；上游刚提交 finish_node 的瞬间，
        本超步可能已经触发了下游）。这种"库还没看到 DONE"是**暂时**的，
        等一小会儿就一致；如果直接 skip，LangGraph 不会再给第二次触发机会，
        整章就永久停在 PENDING 了（实测踩到：B 组 3 章只跑完 ch1）。
        """
        by_id = self._nodes()
        failed = [
            dep
            for dep in spec.deps
            if dep in by_id and by_id[dep].status in FAILURE_STATES
        ]
        if failed:
            return "block", f"上游失败: {', '.join(failed)}"
        waiting = [
            dep for dep in spec.deps if dep not in by_id or by_id[dep].status in _WAITING
        ]
        if waiting:
            return "skip", ",".join(waiting)
        return "run", ""

    async def run(self, spec: NodeSpec) -> dict[str, Any] | None:
        """执行一个节点。返回 None 表示"没产出"（让路 / 已完成 / 已阻塞）。"""
        node_id = spec.id
        snap = self._load(node_id)
        if snap is None:
            self.log.error("[%s] 节点 %s 不在 nodes 表里，跳过", self._tid, node_id)
            return None

        # 闸门是纯同步判定：图的 barrier/普通边已保证"上游完成才派发下游"，
        # 被派发时依赖理应是 DONE。若仍是 RUNNING/PENDING，说明这次触发不该发生
        # （同一节点被多条入边重复触发），此时立刻让路是正确的；原地 await 等待
        # 反而会把它挂在"正在执行"里拖住整个超步，把顺序问题演成随机超时。
        action, reason = self._gate(spec)
        if action == "skip":
            self.log.debug(
                "[%s] %s 让路：依赖未就绪(%s)", self._tid, node_id, reason
            )
            return None
        if action == "block":
            self.store.block_node(task_id=self.task_id, node_id=node_id, reason=reason)
            self.log.warning("[%s] BLOCKED %-14s ← %s", self._tid, node_id, reason)
            return None

        attempt = snap.attempt
        if not self.store.start_node(
            task_id=self.task_id, node_id=node_id, attempt=attempt, owner=self.owner
        ):
            # 已完成（DONE）、被阻塞、或同一 attempt 已被别的路径抢走 —— 一律让路
            self.log.debug("[%s] %s 未被抢占，跳过", self._tid, node_id)
            return None

        if self.crash_before == node_id:
            # 先落 RUNNING 再"断电"：库里留下孤儿 RUNNING，
            # 下次启动由 claim_orphans 重置，正好验证这条恢复路径
            self.log.error(
                "[%s] 模拟进程崩溃：节点 %s 已置 RUNNING 后立即退出", self._tid, node_id
            )
            raise SimulatedCrash(f"crash_before={node_id}")

        # 上游产出从库里取，不依赖图 state —— 这样重启后依然能复原交接内容
        upstream = self._upstream_of(spec)
        hint = snap.revision_hint
        max_attempts = spec.max_attempts

        while True:
            ctx = AgentContext(
                task_id=self.task_id,
                node=spec,
                attempt=attempt,
                upstream=upstream,
                hint=hint,
            )
            self.log.info(
                "[%s] START   %-14s attempt=%d/%d timeout=%ss",
                self._tid,
                node_id,
                attempt,
                max_attempts,
                spec.timeout_s,
            )
            started = time.monotonic()
            try:
                agent = self.registry.get(spec.type)
                async with self.sem:
                    # span_context 让网关知道"这次调用属于哪个任务/节点/轮次"，
                    # Agent 完全不需要感知追踪的存在
                    with span_context(self.task_id, node_id, attempt):
                        data = await asyncio.wait_for(
                            agent.execute(ctx), timeout=spec.timeout_s
                        )
                self._assert_json(data, node_id)
            except (SimulatedCrash, SystemExit, KeyboardInterrupt, asyncio.CancelledError):
                # 进程级事故：直接冒泡，节点保持 RUNNING 留给 claim_orphans 收尸
                raise
            except BaseException as exc:  # noqa: BLE001 - 任何失败都必须落到状态机里
                message = f"{type(exc).__name__}: {exc}"
                # 有些失败重试再多次也不会变：回放缓存缺失就是确定性缺失。
                # 用异常自带的 no_retry 标记判定，编排层不必认识任何具体异常类型。
                permanent = getattr(exc, "no_retry", False) is True
                retryable = attempt < max_attempts and not permanent
                delay = self.retry_backoff_s * (2 ** (attempt - 1))
                self.store.fail_node(
                    task_id=self.task_id,
                    node_id=node_id,
                    attempt=attempt,
                    error=message,
                    retry_at=(utcnow() + timedelta(seconds=delay)) if retryable else None,
                )
                if not retryable:
                    self.log.error(
                        "[%s] FAILED  %-14s attempt=%d 不可重试/重试耗尽 → FAILED | %s",
                        self._tid,
                        node_id,
                        attempt,
                        message,
                    )
                    self.counters["failed"] = self.counters.get("failed", 0) + 1
                    return None
                self.log.warning(
                    "[%s] RETRY   %-14s attempt=%d 失败，%.2fs 后重试 | %s",
                    self._tid,
                    node_id,
                    attempt,
                    delay,
                    message,
                )
                if not await self._await_retry_due(node_id, delay):
                    # 退避一直没到期（时钟/精度问题）：不能把节点丢在 RETRYING 里
                    # 让任务永远不终态，直接判死，交给阻塞传播处理下游。
                    self.log.error(
                        "[%s] FAILED  %-14s 退避到期判定失败，判为 FAILED", self._tid, node_id
                    )
                    self.store.fail_node(
                        task_id=self.task_id,
                        node_id=node_id,
                        attempt=attempt + 1,
                        error=message,
                        retry_at=None,
                    )
                    self.counters["failed"] = self.counters.get("failed", 0) + 1
                    return None
                attempt += 1
                if not self.store.start_node(
                    task_id=self.task_id, node_id=node_id, attempt=attempt, owner=self.owner
                ):
                    return None
                continue

            self.store.finish_node(
                task_id=self.task_id, node_id=node_id, attempt=attempt, output=data
            )
            self.counters["done"] = self.counters.get("done", 0) + 1
            self.log.info(
                "[%s] DONE    %-14s %dms",
                self._tid,
                node_id,
                (time.monotonic() - started) * 1000,
            )
            return dict(data)

    async def _await_retry_due(self, node_id: str, delay: float) -> bool:
        """等退避到期，并把 RETRYING 放回 PENDING。返回 False 表示没等到。

        为什么不能只 `sleep(delay)` 然后 `promote_due_retries`：
        两条时间线（本进程的 monotonic sleep 与入库的 utcnow 墙钟）不可能严格对齐，
        `retry_at <= now` 的比较偶尔会差几微秒，节点就永远停在 RETRYING——
        而 RETRYING 不是终态，任务会既不 DONE 也不 FAILED，看起来"卡住"。
        这里多等一点并轮询确认，把"偶尔差一点"变成"一定能推进"。
        """
        deadline = time.monotonic() + max(0.0, delay) + 0.05
        while True:
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            self.store.promote_due_retries(task_id=self.task_id)
            snap = self._load(node_id)
            if snap is not None and snap.status is NodeStatus.PENDING:
                return True
            if time.monotonic() > deadline + 2.0:
                return False

    def _upstream_of(self, spec: NodeSpec) -> dict[str, dict[str, Any]]:
        by_id = self._nodes()
        return {dep: dict(by_id[dep].output or {}) for dep in spec.deps if dep in by_id}

    @staticmethod
    def _assert_json(data: Any, node_id: str) -> None:
        """Agent 输出必须是 JSON 可序列化的 dict —— 契约错误要在这里发现。"""
        if not isinstance(data, Mapping):
            raise TypeError(f"{node_id} 的 Agent 输出了非 Mapping 结构: {type(data)!r}")
        try:
            json.dumps(dict(data))
        except (TypeError, ValueError) as exc:
            raise TypeError(f"{node_id} 的 Agent 输出不可 JSON 序列化: {exc}") from exc


# --------------------------------------------------------------------------- 编译


def compile_report_graph(
    *,
    graph: Graph,
    store: TaskStore,
    registry: AgentRegistry,
    task_id: str,
    owner: str,
    checkpointer: BaseCheckpointSaver | None,
    sections: int,
    max_concurrency: int,
    max_retries: int,
    retry_backoff_s: float,
    max_review_rounds: int,
    serial_tail: bool = True,
    with_review: bool = True,
    crash_before: str | None = None,
    counters: dict[str, int] | None = None,
) -> Any:
    """把节点清单编译成可执行的 StateGraph。

    `graph` 是落库的那份清单（deps/payload/timeout 都以它为准），
    这里只负责套上 LangGraph 的执行语义：Send 扇出、barrier 汇合、条件回边、
    并发闸、重试策略。
    """
    specs = {n.id: n for n in graph}
    log = get_logger("graph")
    stats: dict[str, int] = counters if counters is not None else {}

    # 并发闸：LangGraph 的超步并行不受 max_concurrency 约束，必须自己包一层，
    # 否则 Send 扇出的 N 个 research 会一次性全放出去（评测与验收都要求 ≤ 上限）
    sem = asyncio.Semaphore(max(1, max_concurrency))
    runner = NodeRunner(
        task_id=task_id,
        owner=owner,
        store=store,
        registry=registry,
        sem=sem,
        crash_before=crash_before,
        retry_backoff_s=retry_backoff_s,
        counters=stats,
        log=log,
    )

    #: 每个节点都挂 RetryPolicy。真正的重试在节点体内完成（见 NodeRunner.run）：
    #: 节点体必须自己吞掉"最终失败"并返回空更新，否则 LangGraph 会中断整个超步，
    #: 同层兄弟章节会被连坐——而"单章失败不影响同层"是本项目的核心卖点。
    #: 这里的 RetryPolicy 是第二道防线：万一节点体外层抛了非预期异常，口径一致。
    policy = RetryPolicy(
        max_attempts=max(1, max_retries + 1),
        retry_on=_retryable,
        initial_interval=retry_backoff_s,
        backoff_factor=2.0,
    )

    def spec_of(node_id: str) -> NodeSpec:
        return specs[node_id]

    builder = StateGraph(ReportState)

    # ---------------------------------------------------------------- planner

    async def planner_node(_state: ReportState) -> dict[str, Any]:
        spec = spec_of("planner")
        data = await runner.run(spec)
        if data is None:
            return {}
        payload = spec.payload
        return {
            "topic": str(data.get("topic") or payload.get("topic", "")),
            "sections": int(payload.get("sections", sections)),
            "outline": list(data.get("outline") or []),
        }

    builder.add_node("planner", planner_node, retry_policy=policy)

    def fan_out(state: ReportState) -> list[Send]:
        """planner 之后动态扇出：章节数由 planner 的产出决定，不是写死的。

        Send 的 payload 是**分支节点自己的输入**，不是完整 state ——
        所以 research 节点绝不假设能读到 state 的全局字段，用到的上下文
        一律从 nodes 表的 payload 与上游产出里取。
        """
        count = int(state.get("sections") or sections)
        return [Send(f"research_ch{i}", {"chapter": i}) for i in range(1, count + 1)]

    # ---------------------------------------------------------------- research

    for i in range(1, sections + 1):
        node_id = f"research_ch{i}"

        def make_research(chapter: int, nid: str) -> Callable[..., Any]:
            async def research_node(_payload: Any = None) -> dict[str, Any]:
                data = await runner.run(spec_of(nid))
                if data is None:
                    return {}
                return {"findings": {str(chapter): data}}

            return research_node
        builder.add_node(node_id, make_research(i, node_id), retry_policy=policy)

    # ---------------------------------------------------------------- write

    for i in range(1, sections + 1):
        node_id = f"write_ch{i}"

        def make_write(chapter: int, nid: str) -> Callable[[ReportState], Any]:
            async def write_node(_state: ReportState) -> dict[str, Any]:
                data = await runner.run(spec_of(nid))
                if data is None:
                    return {}
                return {"chapters": {str(chapter): data}}

            return write_node

        builder.add_node(node_id, make_write(i, node_id), retry_policy=policy)

    # ---------------------------------------------------------------- review

    for i in range(1, sections + 1):
        node_id = f"review_ch{i}"

        def make_review(chapter: int, nid: str) -> Callable[[ReportState], Any]:
            async def review_node(_state: ReportState) -> dict[str, Any]:
                data = await runner.run(spec_of(nid))
                if data is None:
                    return {}
                verdict = str(data.get("verdict", "")).strip().lower()
                current = runner._load(nid)
                round_now = current.review_round if current else 0

                if verdict != "revise":
                    return {
                        "reviews": {str(chapter): data},
                        "review_round": {str(chapter): round_now},
                        "reopened": {str(chapter): False},
                    }

                if round_now >= max_review_rounds:
                    # 打回次数用尽仍未通过：接受当前稿，但记一笔 unresolved，
                    # 任务终态会因此降级为 PARTIAL
                    # （不能让"没通过审查"看起来像成功）
                    log.error(
                        "[%s] %s 已打回 %d 轮仍未通过，接受当前稿（任务终态将标 PARTIAL）",
                        task_id[:8],
                        nid,
                        round_now,
                    )
                    return {
                        "reviews": {str(chapter): data},
                        "review_round": {str(chapter): round_now},
                        "reopened": {str(chapter): False},
                        _UNRESOLVED_KEY: [nid],
                    }

                # 写-审-改闭环：把 target 节点与 review 自身一起重置。
                # 一次事务内完成"目标回 PENDING + attempt+1 + 轮数+1"，
                # 崩溃时不会留下"写了 PENDING 但没加 attempt"的半套状态。
                target = str(data.get("fix_target") or "write")
                targets = [f"write_ch{chapter}", nid]
                if target == "research":
                    targets.insert(0, f"research_ch{chapter}")
                targets = [t for t in targets if t in specs]
                hint = str(data.get("hint") or "").strip()
                changed = store.reopen_for_revision(
                    task_id=task_id,
                    review_node_id=nid,
                    targets=targets,
                    hint=hint,
                )
                reopened = bool(changed)
                if reopened:
                    log.warning(
                        "[%s] 打回 #%d ← %s：重置 %s（fix_target=%s）",
                        task_id[:8],
                        round_now + 1,
                        nid,
                        ", ".join(targets),
                        target,
                    )
                after = runner._load(nid)
                return {
                    "reviews": {str(chapter): data},
                    "review_round": {
                        str(chapter): after.review_round if after else round_now
                    },
                    "reopened": {str(chapter): reopened},
                }

            return review_node

        builder.add_node(node_id, make_review(i, node_id), retry_policy=policy)

    # ---------------------------------------------------------------- 流水线汇合点

    def make_join(count: int) -> Callable[[ReportState], dict[str, Any]]:
        """流水线模式的收尾节点：不做业务，只是给 N 个并行结尾一个统一出口。

        为什么必须有它：LangGraph 在**任一**分支到达 END 时就把整张图判为结束。
        流水线模式下各章 write/review 是彼此独立的并行分支，如果都直连 END，
        第一章一结束整张图就收工，后面几章永远不会执行（实测：3 章任务只跑了
        ch1，其余节点留在 PENDING，任务却报完成）。汇到一个 join 再连 END，
        才是"最后一章完成才收工"。
        """

        async def join_node(_state: ReportState) -> dict[str, Any]:
            # 不写任何 state：它的唯一职责是把控制流收拢。
            return {}

        return join_node

    # ---------------------------------------------------------------- 边

    def wire_chapter(chapter: int, forward: Any) -> None:
        """串一章的尾部：write → review →（打回则回 write / 否则 forward）。

        `forward` 是"本章通过后去哪"：串行尾部是下一章的 write，流水线模式是汇合点。
        """
        write_id = f"write_ch{chapter}"
        if not with_review:
            # 评测 B 组（多 Agent 无 Reviewer）：串行链直接挂在 write 上，
            # 保证与 C 组只有"有没有 review"这一个变量不同
            builder.add_edge(write_id, forward)
            return

        review_id = f"review_ch{chapter}"
        builder.add_edge(write_id, review_id)

        def route(state: ReportState) -> Any:
            if state.get("reopened", {}).get(str(chapter)):
                # 条件回边：打回就回到本章 write，形成写-审-改闭环
                return write_id
            return forward

        builder.add_conditional_edges(review_id, route, [write_id, forward])

    builder.add_edge(START, "planner")
    research_nodes = [f"research_ch{i}" for i in range(1, sections + 1)]
    builder.add_conditional_edges("planner", fan_out, research_nodes)

    if serial_tail:
        # 串行尾部：N 个 research 全部完成后，write_ch1 → (review_ch1) → write_ch2 → …
        #
        # 这里刻意**不用** `add_edge(research_nodes, "write_ch1")` 这种多源 barrier。
        # 实测它并不能表达"等 N 个 research 全部到齐"：LangGraph 会把它展开成
        # N 条独立的边，于是 write_ch1 被 N 个 research 各触发一次，而 write_ch2..N
        # 也会在 research 完成时被一并派发（日志里同一超步出现
        # branch:to:write_ch1 / write_ch2 / write_ch3 三个任务）。
        # 结果就是"写-审-改"的串行语义失效：write_ch2 在 write_ch1 还在跑时就开始，
        # 闸门看到依赖 RUNNING 只能让路，该章随后永久停在 PENDING。
        #
        # 正确做法：把"等齐 N 个 research"交给一个纯汇合节点（它只有一条出边，
        # 不存在被展开成多条的问题），之后整条 write/review 链严格单前驱。
        fanin_id = "research_join"

        async def research_join(_state: ReportState) -> dict[str, Any]:
            # 不写 state：唯一职责是把 N 路并行收成一路。
            return {}

        builder.add_node(fanin_id, research_join)
        # 每个 research 各自连向汇合点：这是 N 条边，但目标是同一个节点，
        # 该节点会在最后一路到达时执行一次（barrier 语义在这里成立）。
        for r in research_nodes:
            builder.add_edge(r, fanin_id)
        builder.add_edge(fanin_id, "write_ch1")
        for i in range(1, sections + 1):
            wire_chapter(i, f"write_ch{i + 1}" if i < sections else END)
    else:
        # 流水线模式：write_ch{i} 只等自己的 research，各章可重叠推进。
        #
        # 这里有个真实的坑（实测踩过）：如果各章 write/review 都直接连 END，
        # LangGraph 会在**第一个**到达 END 的分支把整张图判为结束，
        # 于是 "3 章任务只跑了 ch1，ch2/ch3 从未执行"——节点表里留着 4 个 PENDING，
        # 任务却被标记为完成。
        #
        # 正确做法是让各章先汇到一个 join 节点再统一结束：join 有 N 条入边，
        # 但它不参与业务，只做"最后一个人关门"的作用。
        join_id = "pipeline_join"
        builder.add_node(join_id, make_join(sections))
        for i in range(1, sections + 1):
            builder.add_edge(f"research_ch{i}", f"write_ch{i}")
            wire_chapter(i, join_id)
        builder.add_edge(join_id, END)

    compiled = builder.compile(checkpointer=checkpointer)
    #: runner 跑完后从这里取真实执行计数（与 state 无关，故挂在对象上）
    compiled.rf_stats = stats  # type: ignore[attr-defined]
    return compiled
