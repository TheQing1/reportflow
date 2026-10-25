"""任务生命周期：创建、恢复、驱动 Scheduler。

边界：runner 负责"任务级"的事（建图、孤儿回收、终态汇总），
Scheduler 只负责"节点级"的推进。两者分开后，调度逻辑可以脱离任务概念单测。
"""

from __future__ import annotations

from typing import Any, Callable

import uuid
from dataclasses import dataclass

from ..contracts.agent import AgentRegistry
from ..contracts.task import TaskStatus, TaskStore
from ..core.config import Settings
from ..core.logging import get_logger
from .dag import build_report_graph
from .scheduler import Scheduler, SchedulerOutcome


@dataclass
class RunResult:
    task_id: str
    outcome: SchedulerOutcome | None
    task_status: TaskStatus
    recovered: int
    note: str = ""


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

    def submit(
        self,
        *,
        topic: str,
        sections: int,
        task_id: str | None = None,
        with_review: bool | None = None,
    ) -> tuple[str, bool]:
        """创建任务（幂等：task_id 已存在则不重建）。返回 (task_id, created)。"""
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
        self._log.info(
            "任务已创建 task=%s sections=%d nodes=%d", tid, sections, len(graph)
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

        if task.status is TaskStatus.DONE:
            self._log.info("任务 %s 已是 DONE，无需执行", task_id)
            return RunResult(task_id, None, task.status, 0, "already done")

        owner = f"orc-{uuid.uuid4().hex[:8]}"
        # 上个进程留下的 RUNNING 视为孤儿：进程已死，重置为 PENDING 重跑本次尝试
        recovered = self.store.claim_orphans(task_id=task_id, owner=owner)
        if recovered:
            self._log.warning(
                "恢复 %d 个孤儿节点（原进程已退出，重置为 PENDING）", recovered
            )

        if reset_failed:
            n = self.store.reset_failed(task_id=task_id)
            if n:
                self._log.warning("重置 %d 个失败/阻塞节点，允许重新执行", n)

        graph = self.store.load_graph(task_id)
        if graph is None:
            raise LookupError(f"任务 {task_id} 没有节点，无法恢复")

        scheduler = Scheduler(
            task_id=task_id,
            store=self.store,
            registry=self.registry,
            concurrency=concurrency or self.settings.max_concurrency,
            poll_interval_s=self.settings.poll_interval_s,
            retry_backoff_s=self.settings.retry_backoff_s,
            owner=owner,
            crash_before=crash_before,
            max_review_rounds=task.max_review_rounds,
        )
        outcome = await scheduler.run()
        return RunResult(task_id, outcome, outcome.status, recovered)
