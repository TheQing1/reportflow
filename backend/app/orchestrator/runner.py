"""任务生命周期：建图、断点续跑、把 LangGraph 的推进同步回 TaskStore。

边界：runner 负责"任务级"的事（建图、孤儿回收、终态汇总、checkpoint 装配），
graph_langgraph 负责"节点级"的推进。分层理由没有变——调度语义可以脱离任务概念单测。

**双写是这里唯一真正需要小心的事。**
LangGraph 的 checkpoint 只服务于"图推进到哪了"，它既不是前端的数据源，
也不是 exporter 的数据源：api/app.py 轮询 store.list_nodes()、exporter 按
write_ch{i} 取正文、spans 按 node_id 归因成本。所以每个节点的状态变化都必须
在当次执行内写回 nodes 表（start_node / finish_node / fail_node / block_node），
checkpoint 与 nodes 表各自回答不同的问题，谁都不能替代谁。

断点续跑的两种入口必须区分清楚：
- **中途断电**：checkpoint 停在半个超步上，用 `invoke(None, cfg)` 从最后一个
  checkpoint 继续，已完成的节点不会再跑。
- **失败重置后重跑**（reset_failed）：图其实已经走到 END 了，checkpoint 没有
  "还得再跑"的节点；这时必须以初始 state 重新触发一次，靠 nodes 表的 DONE
  状态把已完成的节点全部跳过（start_node 的条件更新天然幂等）。
  这就是"图负责推进、库负责裁决"的分工。
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ..contracts.agent import AgentRegistry
from ..contracts.nodes import FAILURE_STATES, Graph, NodeStatus, NodeType
from ..contracts.task import NodeState, TaskStatus, TaskStore
from ..core.config import Settings
from ..core.logging import get_logger
from .graph_langgraph import (
    SimulatedCrash,
    build_report_graph,
    compile_report_graph,
)

#: 图跑一轮的步数上限。5 章 + 每章最多 2 轮打回 ≈ 32 步，留足余量。
#: 不设的话 LangGraph 默认 25，章节多或打回多时会撞 RecursionError。
_RECURSION_LIMIT = 200


@dataclass
class RunOutcome:
    """一次图执行的结果摘要。字段与旧的调度器 outcome 对齐，便于对照历史日志。"""

    task_id: str
    status: TaskStatus
    executed: int
    skipped_done: int
    elapsed_s: float
    counts: dict[NodeStatus, int]


@dataclass
class RunResult:
    task_id: str
    outcome: RunOutcome | None
    task_status: TaskStatus
    recovered: int
    note: str = ""


def checkpoint_path_for(settings: Settings, task_id: str) -> Path | None:
    """checkpoint 落在任务库旁边的独立 sqlite 文件。

    不复用任务库本身：LangGraph 会自己建表，与 TaskRow/NodeRow 混在一个文件里
    会让"哪个表是事实来源"这件事变得模糊。非 sqlite 的库（MySQL 等）暂时
    不落 checkpoint —— 那种部署本来就要换成 Redis/Postgres 的 saver。
    """
    url = settings.database_url
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    raw = url[len(prefix) :]
    base = Path(raw)
    return base.with_suffix(base.suffix + ".checkpoints")


class TaskRunner:
    def __init__(
        self,
        *,
        store: TaskStore,
        registry: AgentRegistry,
        settings: Settings,
        profile: str = "mock",
        budget_fn: Callable[[str, int], dict[int, list[str]]] | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.settings = settings
        self.profile = profile
        #: 注入的文档预算函数。注入而不是直接 import rag：
        #: 编排层不该知道检索层的存在，否则分层约束就破了。
        self._budget_fn = budget_fn
        self._log = get_logger("runner")

    # ---------------------------------------------------------------- 建任务

    def submit(
        self,
        *,
        topic: str,
        sections: int,
        task_id: str | None = None,
        with_review: bool | None = None,
        serial_tail: bool = True,
    ) -> tuple[str, bool]:
        """创建任务（幂等：task_id 已存在则不重建）。返回 (task_id, created)。

        `serial_tail=True`（默认）是"write/review 整章串行"：第 N 章的审稿意见
        能约束第 N+1 章，跨章衔接更稳，也是 40 主题评测的口径。
        传 False 走流水线模式：各章只等自己的 research，章与章可重叠推进。
        形态会被写进 nodes 表的 deps，后续 run() 从 deps 反推，不需要额外字段。
        """
        tid = task_id or uuid.uuid4().hex[:12]
        if self.store.get_task(tid) is not None:
            self._log.info("任务 %s 已存在，进入续跑模式（不重建节点）", tid)
            return tid, False

        # 默认不把 Reviewer 放进写作循环：评测显示它无收益却贵 49%
        review = self.settings.review_in_pipeline if with_review is None else with_review
        budgets = self._doc_budgets(topic=topic, sections=sections)
        graph = build_report_graph(
            topic=topic,
            sections=sections,
            max_retries=self.settings.max_retries,
            timeout_s=self.settings.node_timeout_s,
            serial_tail=serial_tail,
            with_review=review,
            doc_budgets=budgets,
        )
        self.store.create_task(
            task_id=tid,
            topic=topic,
            sections=sections,
            graph=graph,
            max_review_rounds=self.settings.max_review_rounds,
            profile=self.profile,
        )
        #: 不额外记录"有没有 Reviewer"：带 review 的任务必然存在 review_ch1 节点，
        #: 续跑时看节点类型就能复原同一个拓扑（见 run() 里的 with_review 推导）。
        #: 这样不必改动持久化契约。
        self._log.info(
            "任务已创建 task=%s sections=%d nodes=%d review=%s",
            tid,
            sections,
            len(graph),
            review,
        )
        return tid, True

    def _doc_budgets(self, *, topic: str, sections: int) -> dict[int, list[str]] | None:
        """提交前给每章分配文档预算（见 rag/budget.py）。

        放在提交时而不是 research 节点里，是为了**保住五章 research 的并行**：
        若让 research 串起来互相避让，会牺牲阶段一验收的并行结构；
        预算一次算完写进节点 payload，各章照样同时跑。
        """
        if not self.settings.doc_budget or self._budget_fn is None:
            return None
        try:
            budgets = self._budget_fn(topic, sections)
            if budgets:
                self._log.info("文档预算已分配：%d 章", len(budgets))
            return budgets or None
        except Exception as exc:  # noqa: BLE001 - 预算只是优化，失败不能挡住任务
            self._log.warning("文档预算分配失败，按无预算继续: %s", exc)
            return None

    # ---------------------------------------------------------------- 跑任务

    async def run(
        self,
        task_id: str,
        *,
        crash_before: str | None = None,
        reset_failed: bool = False,
        concurrency: int | None = None,
    ) -> RunResult:
        task = self.store.get_task(task_id)
        if task is None:
            raise LookupError(f"任务不存在: {task_id}")

        started = time.monotonic()
        has_failed = any(n.status in FAILURE_STATES for n in self.store.list_nodes(task_id))
        if task.status is TaskStatus.DONE and not (reset_failed and has_failed):
            # 只有 DONE 才短路：PARTIAL 任务重跑一遍是廉价的（所有节点都已终态，
            # 逐个被 start_node 挡掉），但能重新汇总出正确的终态——
            # 这对"修完根因再跑一次"的使用方式是必要的。
            self._log.info("任务 %s 已是 DONE，无需执行", task_id)
            return RunResult(task_id, None, task.status, 0, "already done")

        owner = f"orc-{uuid.uuid4().hex[:8]}"
        # 上个进程留下的 RUNNING 视为孤儿：进程已死，重置为 PENDING 重跑本次尝试
        recovered = self.store.claim_orphans(task_id=task_id, owner=owner)
        if recovered:
            self._log.warning("恢复 %d 个孤儿节点（原进程已退出，重置为 PENDING）", recovered)

        if reset_failed:
            n = self.store.reset_failed(task_id=task_id)
            if n:
                self._log.warning("重置 %d 个失败/阻塞节点，允许重新执行", n)

        graph = self.store.load_graph(task_id)
        if graph is None:
            raise LookupError(f"任务 {task_id} 没有节点，无法恢复")

        with_review = any(n.type is NodeType.REVIEW for n in graph)
        # 拓扑形态从落库的 deps 推导，而不是另存一个布尔字段：deps 就是权威记录，
        # 断点续跑/换进程都不用额外状态，也不会出现"库里的标记与实际 deps 不一致"。
        #
        # 判据：串行尾部下 write_ch{i} (i>1) 必然有**两个**依赖 —— 自己那章的
        # research，加上上一章的尾部（with_review 时是 review_ch{i-1}，
        # 评测 B 组无 Reviewer 时是 write_ch{i-1}）。
        # 流水线模式下它只有 research 一个依赖。
        #
        # 之前这里写成"deps 里是否含 review_ch"，在 B 组（无 Reviewer）会恒为 False，
        # 于是把串行尾部误判成流水线，图按流水线拓扑编译——下游章节被提前派发，
        # 闸门看到上游 RUNNING 只能让路，最后只生成第一章。这个 bug 只在
        # with_review=False 时出现，而默认配置恰好是 False，属于最容易上线才炸的类型。
        serial_tail = any(
            len(node.deps) > 1
            for node in graph
            if node.id.startswith("write_ch") and node.id != "write_ch1"
        )
        self.store.set_task_status(task_id, TaskStatus.RUNNING)

        counters: dict[str, int] = {}
        cfg = {
            "configurable": {"thread_id": self._thread_id(task_id)},
            "recursion_limit": _RECURSION_LIMIT,
        }
        checkpoint = checkpoint_path_for(self.settings, task_id)
        initial = {"topic": task.topic, "sections": task.sections}

        if checkpoint is not None:
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            async with AsyncSqliteSaver.from_conn_string(str(checkpoint)) as saver:
                await self._drive(
                    task_id=task_id,
                    owner=owner,
                    graph=graph,
                    cfg=cfg,
                    saver=saver,
                    initial=initial,
                    with_review=with_review,
                    serial_tail=serial_tail,
                    concurrency=concurrency,
                    crash_before=crash_before,
                    counters=counters,
                    replay=reset_failed,
                )
        else:
            # 非 sqlite 部署：没有 checkpoint 就只能整轮重放。
            # nodes 表的 DONE 状态保证已完成的节点不会被重复调用，
            # 失掉的是"半个超步"的中途续跑能力，日志里说明清楚。
            self._log.warning("当前数据库无 checkpoint 支持，本次以整轮重放方式执行")
            await self._drive(
                task_id=task_id,
                owner=owner,
                graph=graph,
                cfg=cfg,
                saver=None,
                initial=initial,
                with_review=with_review,
                serial_tail=serial_tail,
                concurrency=concurrency,
                crash_before=crash_before,
                counters=counters,
                replay=True,
            )

        final_nodes = self.store.list_nodes(task_id)
        counts = Counter(n.status for n in final_nodes)
        status = self._final_task_status(final_nodes, counts)
        self.store.set_task_status(task_id, status)
        skipped = sum(1 for n in final_nodes if n.status is NodeStatus.DONE)
        self._log.info(
            "[%s] 任务结束 status=%s 执行节点=%d DONE=%d",
            task_id[:8],
            status.value,
            counters.get("done", 0),
            skipped,
        )
        outcome = RunOutcome(
            task_id=task_id,
            status=status,
            executed=counters.get("done", 0),
            skipped_done=skipped,
            elapsed_s=time.monotonic() - started,
            counts=dict(counts),
        )
        return RunResult(task_id, outcome, status, recovered)

    async def _drive(
        self,
        *,
        task_id: str,
        owner: str,
        graph: Graph,
        cfg: dict,
        saver: AsyncSqliteSaver | None,
        initial: dict,
        with_review: bool,
        serial_tail: bool,
        concurrency: int | None,
        crash_before: str | None,
        counters: dict[str, int],
        replay: bool,
    ) -> None:
        """真正调用 LangGraph。崩溃信号在这里翻译回 SystemExit。

        SimulatedCrash 继承 BaseException 是为了躲开重试与 asyncio 的取消语义
        （SystemExit 在带 checkpointer 的图里会变成 CancelledError，把"断电"
        伪装成"任务被取消"）。但对外部调用方——CLI、旧测试——必须保持原来的
        契约：抛 SystemExit，进程以 137 退出。
        """
        app = compile_report_graph(
            graph=graph,
            store=self.store,
            registry=self.registry,
            task_id=task_id,
            owner=owner,
            checkpointer=saver,
            sections=len([n for n in graph if n.id.startswith("research_")]),
            max_concurrency=concurrency or self.settings.max_concurrency,
            max_retries=self.settings.max_retries,
            retry_backoff_s=self.settings.retry_backoff_s,
            max_review_rounds=self._max_review_rounds(task_id),
            with_review=with_review,
            serial_tail=serial_tail,
            crash_before=crash_before,
            counters=counters,
        )

        resumable = await self._resume_mode(app, cfg, replay=replay)
        try:
            if resumable:
                await app.ainvoke(None, cfg)
            else:
                await app.ainvoke(initial, cfg)
        except SimulatedCrash as exc:
            self._log.error("模拟崩溃：%s", exc)
            raise SystemExit(SimulatedCrash.exit_code) from None

    async def _resume_mode(self, app: object, cfg: dict, *, replay: bool) -> bool:
        """True = 用 invoke(None) 从中断处续跑；False = 以初始 state 重放。

        判据只有一个：checkpoint 里还有没有"待执行的节点"。
        已走到 END 的图没有 next，只能重放；此时 nodes 表的 DONE 状态负责跳过
        已完成的节点，所以重放是廉价且正确的。
        """
        if replay:
            return False
        try:
            snapshot = await app.aget_state(cfg)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 没有 checkpoint 时按新任务处理
            return False
        if snapshot.created_at is None:
            return False  # 全新 thread
        return bool(snapshot.next)

    @staticmethod
    def _thread_id(task_id: str) -> str:
        """task_id → LangGraph thread_id。

        一一对应且可逆：同一个任务在任何进程里都落到同一条 checkpoint 链上，
        这是"重启后继续跑"的前提。不做哈希/截断——哈希会带来碰撞风险，
        而 task_id 本身就是合法的 thread_id。
        """
        return f"reportflow:{task_id}"

    def _max_review_rounds(self, task_id: str) -> int:
        task = self.store.get_task(task_id)
        return task.max_review_rounds if task else self.settings.max_review_rounds

    # ---------------------------------------------------------------- 终态

    def _final_task_status(self, nodes: list[NodeState], counts: Counter[NodeStatus]) -> TaskStatus:
        total = len(nodes)
        if total == 0:
            return TaskStatus.FAILED
        done = counts.get(NodeStatus.DONE, 0)
        if done == total:
            # 节点都跑完了，但如果有章节打回次数用尽仍未通过，不能算 DONE。
            # 从库里推导而不是靠内存标记：重启后也要得出同样的结论。
            return TaskStatus.PARTIAL if self._has_unresolved(nodes) else TaskStatus.DONE
        if done == 0:
            return TaskStatus.FAILED
        return TaskStatus.PARTIAL

    @staticmethod
    def _has_unresolved(nodes: list[NodeState]) -> bool:
        """有没有"打回到上限仍未通过"的章节。

        判据落在 nodes 表上：review 节点的产出判了 revise，且轮数已经到顶。
        这样重启后重新汇总也能得到一致结论，不需要额外持久化字段。
        """
        for node in nodes:
            if node.type is not NodeType.REVIEW or not node.output:
                continue
            if str(node.output.get("verdict", "")).strip().lower() == "revise":
                return True
        return False
