"""调度循环：拓扑推进 + 并发受限执行 + 失败阻塞传播。

三条在实现上必须小心的规则（也是这套引擎最容易写错的地方）：

1. **阻塞传播要到不动点**。串行链上 review_ch1 被 BLOCKED 后，write_ch2 才
   变成可阻塞；一轮遍历受顺序影响，必须反复扫描直到没有新变化，否则下游会
   永远停在 PENDING，任务看起来"卡住"却已经死了。
2. **状态迁移一律带条件**（PENDING->RUNNING 用 rowcount 判定），
   保证同一 (node_id, attempt) 只被执行一次——这是断点续跑与幂等的基础。
3. **没有 ready 也没有 running 时必须收敛退出**，而不是继续轮询。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Mapping

from ..contracts.agent import AgentContext, AgentRegistry
from ..contracts.nodes import FAILURE_STATES, Graph, NodeStatus, NodeType, is_terminal
from ..contracts.task import NodeState, TaskStatus, TaskStore
from ..contracts.tracing import span_context
from ..core.logging import get_logger
from ..core.timeutil import utcnow

#: 这些状态下节点仍"可能被执行"，因此可以被上游失败判为 BLOCKED
_BLOCKABLE = frozenset({NodeStatus.PENDING, NodeStatus.RETRYING})


@dataclass
class SchedulerOutcome:
    task_id: str
    status: TaskStatus
    waves: int
    executed: int
    skipped_done: int
    elapsed_s: float
    counts: dict[NodeStatus, int]


class Scheduler:
    """单任务调度器。

    单实例假设：一个 task 同时只由一个 Scheduler 驱动。多副本部署必须另加
    task 级分布式锁，否则两个进程会重复调用 Agent（成本翻倍）。
    """

    def __init__(
        self,
        *,
        task_id: str,
        store: TaskStore,
        registry: AgentRegistry,
        concurrency: int = 3,
        poll_interval_s: float = 0.05,
        retry_backoff_s: float = 1.0,
        owner: str | None = None,
        crash_before: str | None = None,
        max_review_rounds: int = 2,
    ) -> None:
        self.task_id = task_id
        self.store = store
        self.registry = registry
        self.concurrency = max(1, concurrency)
        self.poll_interval_s = poll_interval_s
        self.retry_backoff_s = retry_backoff_s
        self.owner = owner or f"orc-{uuid.uuid4().hex[:8]}"
        #: 仅用于验收演示：在该节点"启动后"直接退出进程，模拟断电
        self.crash_before = crash_before
        self.max_review_rounds = max(0, max_review_rounds)
        self._tid = f"{task_id[:8]}"
        self._log = get_logger("scheduler")
        #: 打回次数已用尽、仍未通过审查的章节（任务终态要降级为 PARTIAL）
        self._unresolved: set[str] = set()
        self._cap_logged: set[str] = set()

    # ------------------------------------------------ 主循环

    async def run(self) -> SchedulerOutcome:
        started = time.monotonic()
        self.store.set_task_status(self.task_id, TaskStatus.RUNNING)

        first = self.store.list_nodes(self.task_id)
        skipped = sum(1 for n in first if n.status is NodeStatus.DONE)
        if skipped:
            self._log.info(
                "[%s] 断点续跑：%d 个节点已是 DONE，本轮不再执行",
                self._tid,
                skipped,
            )

        waves = 0
        executed = 0
        while True:
            # RETRYING 到期 -> PENDING（退避时间到了才允许重跑）
            self.store.promote_due_retries(task_id=self.task_id)
            nodes = self.store.list_nodes(self.task_id)
            by_id = {n.node_id: n for n in nodes}

            # Review 打回：必须在算 ready 之前处理，
            # 否则 review_ch1 判不通过的同时 write_ch2 就被放出去写了
            if self._apply_revisions(nodes, by_id):
                nodes = self.store.list_nodes(self.task_id)
                by_id = {n.node_id: n for n in nodes}

            if self._propagate_blockers(nodes, by_id):
                nodes = self.store.list_nodes(self.task_id)
                by_id = {n.node_id: n for n in nodes}

            counts = Counter(n.status for n in nodes)
            if all(is_terminal(n.status) for n in nodes):
                break

            ready = [
                n
                for n in nodes
                if n.status is NodeStatus.PENDING
                and all(by_id[d].status is NodeStatus.DONE for d in n.deps)
            ]
            running = counts.get(NodeStatus.RUNNING, 0)
            capacity = self.concurrency - running

            if ready and capacity > 0:
                batch = ready[:capacity]
                waves += 1
                self._log.info(
                    "[%s] wave-%02d 并发启动 %d 个节点: %s",
                    self._tid,
                    waves,
                    len(batch),
                    ", ".join(n.node_id for n in batch),
                )
                await asyncio.gather(
                    *(self._execute(n, by_id) for n in batch), return_exceptions=False
                )
                executed += len(batch)
                after = Counter(
                    n.status for n in self.store.list_nodes(self.task_id)
                )
                self._log.info(
                    "[%s] wave-%02d 结束 | DONE=%d RUNNING=%d PENDING=%d "
                    "RETRYING=%d BLOCKED=%d FAILED=%d",
                    self._tid,
                    waves,
                    after.get(NodeStatus.DONE, 0),
                    after.get(NodeStatus.RUNNING, 0),
                    after.get(NodeStatus.PENDING, 0),
                    after.get(NodeStatus.RETRYING, 0),
                    after.get(NodeStatus.BLOCKED, 0),
                    after.get(NodeStatus.FAILED, 0),
                )
                continue

            if running > 0:
                await asyncio.sleep(self.poll_interval_s)
                continue

            waiting_retry = [n for n in nodes if n.status is NodeStatus.RETRYING]
            if waiting_retry:
                await asyncio.sleep(self._retry_sleep(waiting_retry))
                continue

            # 既没有可执行的，也没有在跑或等待重试的 —— 说明剩下的节点互锁
            stalled = [n.node_id for n in nodes if not is_terminal(n.status)]
            self._log.error(
                "[%s] 调度停滞，未达终态的节点: %s（存在未识别的依赖问题）",
                self._tid,
                ", ".join(stalled) or "-",
            )
            break

        final_nodes = self.store.list_nodes(self.task_id)
        counts = Counter(n.status for n in final_nodes)
        status = self._final_task_status(counts, len(final_nodes))
        self.store.set_task_status(self.task_id, status)
        elapsed = time.monotonic() - started
        self._log.info(
            "[%s] 任务结束 status=%s 用时=%.2fs 执行节点=%d 跳过(已完成)=%d",
            self._tid,
            status.value,
            elapsed,
            executed,
            skipped,
        )
        return SchedulerOutcome(
            task_id=self.task_id,
            status=status,
            waves=waves,
            executed=executed,
            skipped_done=skipped,
            elapsed_s=elapsed,
            counts=dict(counts),
        )

    # ------------------------------------------------ 打回重写

    def _apply_revisions(
        self, nodes: list[NodeState], by_id: dict[str, NodeState]
    ) -> bool:
        """Reviewer 判不通过时，把对应章节的节点重置，形成"写-审-改"闭环。

        不加 DAG 回边（那会成环），而是复用 attempt 机制：重置时 attempt+1，
        幂等键随之变化，重写才不会被"已完成就跳过"挡住。
        """
        changed = False
        for node in nodes:
            if node.type is not NodeType.REVIEW or node.status is not NodeStatus.DONE:
                continue
            output = dict(node.output or {})
            if str(output.get("verdict", "")).lower() != "revise":
                continue

            chapter = int(node.payload.get("chapter", 0))
            if node.review_round >= self.max_review_rounds:
                if node.node_id not in self._cap_logged:
                    self._cap_logged.add(node.node_id)
                    self._unresolved.add(node.node_id)
                    self._log.error(
                        "[%s] %s 已打回 %d 轮仍未通过，接受当前稿（任务终态将标记 PARTIAL）",
                        self._tid,
                        node.node_id,
                        node.review_round,
                    )
                continue

            target = str(output.get("fix_target") or "write")
            targets = [f"write_ch{chapter}", node.node_id]
            if target == "research":
                targets.insert(0, f"research_ch{chapter}")
            targets = [t for t in targets if t in by_id]

            hint = str(output.get("hint") or "").strip()
            reopened = self.store.reopen_for_revision(
                task_id=self.task_id,
                review_node_id=node.node_id,
                targets=targets,
                hint=hint,
            )
            if not reopened:
                continue
            self._log.warning(
                "[%s] 打回 #%d ← %s：重置 %s（fix_target=%s）",
                self._tid,
                node.review_round + 1,
                node.node_id,
                ", ".join(targets),
                target,
            )
            for t in targets:
                current = by_id.get(t)
                if current is not None:
                    by_id[t] = replace(
                        current, status=NodeStatus.PENDING, attempt=current.attempt + 1
                    )
            by_id[node.node_id] = replace(
                node, review_round=node.review_round + 1, status=NodeStatus.PENDING
            )
            changed = True
        return changed

    # ------------------------------------------------ 阻塞传播

    def _propagate_blockers(
        self, nodes: list[NodeState], by_id: dict[str, NodeState]
    ) -> bool:
        changed_any = False
        progressed = True
        while progressed:
            progressed = False
            for node in nodes:
                current = by_id[node.node_id]
                if current.status not in _BLOCKABLE:
                    continue
                failed_deps = [
                    d for d in node.deps if by_id[d].status in FAILURE_STATES
                ]
                if not failed_deps:
                    continue
                reason = f"上游失败: {', '.join(failed_deps)}"
                self.store.block_node(
                    task_id=self.task_id, node_id=node.node_id, reason=reason
                )
                by_id[node.node_id] = replace(
                    current, status=NodeStatus.BLOCKED, error=reason
                )
                self._log.warning(
                    "[%s] BLOCKED %-14s ← %s", self._tid, node.node_id, reason
                )
                changed_any = progressed = True
        return changed_any

    # ------------------------------------------------ 单节点执行

    async def _execute(self, state: NodeState, by_id: Mapping[str, NodeState]) -> None:
        node_id = state.node_id

        if self.crash_before == node_id:
            # 先把节点标成 RUNNING 再退出：库里留下"孤儿 RUNNING"，
            # 下次启动由 claim_orphans 重置，正好验证这条恢复路径。
            self.store.start_node(
                task_id=self.task_id,
                node_id=node_id,
                attempt=state.attempt,
                owner=self.owner,
            )
            self._log.error(
                "[%s] 模拟进程崩溃：节点 %s 已置 RUNNING 后立即退出", self._tid, node_id
            )
            raise SystemExit(137)

        claimed = self.store.start_node(
            task_id=self.task_id,
            node_id=node_id,
            attempt=state.attempt,
            owner=self.owner,
        )
        if not claimed:
            self._log.debug("[%s] 节点 %s 未被抢占（已被处理）", self._tid, node_id)
            return

        spec = state.to_spec()
        upstream: dict[str, Any] = {
            dep: dict(by_id[dep].output or {}) for dep in state.deps if dep in by_id
        }
        ctx = AgentContext(
            task_id=self.task_id,
            node=spec,
            attempt=state.attempt,
            upstream=upstream,
            hint=state.revision_hint,
        )
        self._log.info(
            "[%s] START   %-14s attempt=%d/%d timeout=%ss",
            self._tid,
            node_id,
            state.attempt,
            spec.max_attempts,
            spec.timeout_s,
        )
        t0 = time.monotonic()
        try:
            agent = self.registry.get(spec.type)
            # span_context 让网关知道"这次调用属于哪个任务/节点/轮次"，
            # Agent 完全不需要感知追踪的存在
            with span_context(self.task_id, node_id, state.attempt):
                data = await asyncio.wait_for(agent.execute(ctx), timeout=spec.timeout_s)
            self._assert_json(data, node_id)
            self.store.finish_node(
                task_id=self.task_id,
                node_id=node_id,
                attempt=state.attempt,
                output=data,
            )
            self._log.info(
                "[%s] DONE    %-14s %dms", self._tid, node_id, (time.monotonic() - t0) * 1000
            )
        except (SystemExit, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except BaseException as exc:  # noqa: BLE001 - 任何异常都必须落到状态机里
            message = f"{type(exc).__name__}: {exc}"
            # 有些失败重试再多次也不会变：回放缓存缺失就是确定性缺失。
            # 用异常自带的 no_retry 标记判定，调度层不必认识任何具体异常类型。
            permanent = getattr(exc, "no_retry", False) is True
            retryable = state.attempt < spec.max_attempts and not permanent
            retry_at = None
            if retryable:
                delay = self.retry_backoff_s * (2 ** (state.attempt - 1))
                retry_at = utcnow() + timedelta(seconds=delay)
            self.store.fail_node(
                task_id=self.task_id,
                node_id=node_id,
                attempt=state.attempt,
                error=message,
                retry_at=retry_at,
            )
            if retryable:
                self._log.warning(
                    "[%s] FAILED  %-14s attempt=%d %dms → RETRYING(attempt=%d, +%.1fs) | %s",
                    self._tid,
                    node_id,
                    state.attempt,
                    (time.monotonic() - t0) * 1000,
                    state.attempt + 1,
                    self.retry_backoff_s * (2 ** (state.attempt - 1)),
                    message,
                )
            elif permanent:
                self._log.error(
                    "[%s] FAILED  %-14s 不可重试 → FAILED | %s",
                    self._tid,
                    node_id,
                    message,
                )
            else:
                self._log.error(
                    "[%s] FAILED  %-14s attempt=%d 重试已耗尽 → FAILED | %s",
                    self._tid,
                    node_id,
                    state.attempt,
                    message,
                )

    # ------------------------------------------------ 工具

    def _retry_sleep(self, waiting: list[NodeState]) -> float:
        now = utcnow()
        deltas = [
            max(0.0, (n.retry_at - now).total_seconds())
            for n in waiting
            if n.retry_at is not None
        ]
        return min(min(deltas), 1.0) if deltas else self.poll_interval_s

    @staticmethod
    def _assert_json(data: Any, node_id: str) -> None:
        """Agent 输出必须是 JSON 可序列化的 dict —— 契约错误要在落地前发现。"""
        if not isinstance(data, Mapping):
            raise TypeError(f"{node_id} 的 Agent 输出了非 Mapping 结构: {type(data)!r}")
        try:
            json.dumps(dict(data))
        except (TypeError, ValueError) as exc:
            raise TypeError(f"{node_id} 的 Agent 输出不可 JSON 序列化: {exc}") from exc

    def _final_task_status(self, counts: Counter[NodeStatus], total: int) -> TaskStatus:
        if total == 0:
            return TaskStatus.FAILED
        done = counts.get(NodeStatus.DONE, 0)
        if done == total:
            # 节点都跑完了，但如果有章节打回次数用尽仍未通过，不能算 DONE
            return TaskStatus.PARTIAL if self._unresolved else TaskStatus.DONE
        if done == 0:
            return TaskStatus.FAILED
        return TaskStatus.PARTIAL


def describe_graph(graph: Graph) -> str:
    return graph.describe()
