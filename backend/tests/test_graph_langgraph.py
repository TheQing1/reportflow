"""LangGraph 编排的行为验证：并行 / 串行 / 失败阻塞 / 断点续跑 / 打回闭环。

这里用"记录 Agent 的起止时刻"来计算真实并发重叠度，而不是相信日志——
日志可能骗人，时间区间不会。

与旧 test_scheduler.py 的关系：断言强度刻意保持不变（那些是项目的核心卖点），
换框架不该让任何一条验收口径变松。新增的部分是 LangGraph 特有的语义：
checkpoint 落盘、thread_id 映射、图本身的拓扑形状。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from app.agents import MockAgentConfig, build_mock_registry
from app.assembler import build_runtime
from app.contracts.agent import AgentContext, AgentRegistry
from app.contracts.nodes import NodeStatus, NodeType
from app.contracts.task import TaskStatus
from app.core.config import Settings
from app.orchestrator.graph_langgraph import ReportState, build_report_graph

FAST_LATENCY = {
    NodeType.PLANNER: 0.01,
    NodeType.RESEARCH: 0.12,
    NodeType.WRITE: 0.06,
    NodeType.REVIEW: 0.04,
}


class RecordingAgent:
    """包一层，记录每个节点的执行区间；不改变被包 Agent 的行为。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.node_type = inner.node_type
        self.intervals: list[tuple[str, float, float]] = []

    async def execute(self, ctx: AgentContext) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            return await self.inner.execute(ctx)
        finally:
            self.intervals.append((ctx.node.id, t0, time.monotonic()))

    def executed(self) -> list[str]:
        return [nid for nid, _, _ in self.intervals]

    def max_overlap(self, prefix: str) -> int:
        spans = sorted(
            [(s, e) for nid, s, e in self.intervals if nid.startswith(prefix)]
        )
        best = cur = 0
        edges = [(s, 1) for s, _ in spans] + [(e, -1) for _, e in spans]
        for _, delta in sorted(edges, key=lambda x: (x[0], x[1])):
            cur += delta
            best = max(best, cur)
        return best


def make_registry(
    fail_nodes: set[str] | None = None,
) -> tuple[Any, dict[NodeType, RecordingAgent]]:
    cfg = MockAgentConfig(
        latency_s=FAST_LATENCY, fail_nodes=frozenset(fail_nodes or ()), jitter_s=0.01
    )
    base = build_mock_registry(cfg)
    rec: dict[NodeType, RecordingAgent] = {
        t: RecordingAgent(base.get(t)) for t in NodeType
    }
    return AgentRegistry(rec), rec


class GraphTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="reportflow-graph-"))
        self.db_url = f"sqlite:///{(self.tmpdir / 'test.db').as_posix()}"
        self.settings = self._settings()

    def _settings(self, **overrides: Any) -> Settings:
        base = Settings(
            database_url=self.db_url,
            max_concurrency=3,
            node_timeout_s=5,
            max_retries=1,
            retry_backoff_s=0.01,
            poll_interval_s=0.01,
            # 这组用例验证的是带 Reviewer 的完整串行尾部；默认流水线已关掉 review
            review_in_pipeline=True,
        )
        return base.with_overrides(**overrides)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def runtime(self, fail_nodes: set[str] | None = None, settings: Settings | None = None):
        rt = build_runtime(settings or self.settings, profile="mock", announce=False)
        registry, rec = make_registry(fail_nodes)
        rt.registry = registry
        rt.runner.registry = registry
        return rt, rec


# ---------------------------------------------------------------- 图构建本身


class TestBuildReportGraph(unittest.TestCase):
    """图是持久化契约：nodes 表的 deps 被前端分列、被阻塞判定使用。"""

    def test_shape_serial_tail(self) -> None:
        g = build_report_graph(topic="储能", sections=5)
        self.assertEqual(len(g), 1 + 5 + 5 + 5)

        by_id = g.by_id
        # research 全部只依赖 planner —— 这是并行的前提
        for i in range(1, 6):
            self.assertEqual(by_id[f"research_ch{i}"].deps, ("planner",))
        # 尾部串行：write_ch2 要等 review_ch1
        self.assertEqual(by_id["write_ch1"].deps, ("research_ch1",))
        self.assertEqual(by_id["write_ch2"].deps, ("research_ch2", "review_ch1"))
        self.assertEqual(by_id["review_ch2"].deps, ("write_ch2",))

    def test_node_names_are_stable(self) -> None:
        """节点命名是跨系统的硬契约：spans 归因、前端分列、exporter 取正文都靠它。"""
        g = build_report_graph(topic="x", sections=4)
        ids = {n.id for n in g}
        for i in range(1, 5):
            for prefix in ("research", "write", "review"):
                self.assertIn(f"{prefix}_ch{i}", ids)
        self.assertIn("planner", ids)

    def test_pipeline_mode_available(self) -> None:
        g = build_report_graph(topic="储能", sections=3, serial_tail=False)
        by_id = g.by_id
        self.assertEqual(by_id["write_ch2"].deps, ("research_ch2",))
        self.assertEqual(by_id["write_ch3"].deps, ("research_ch3",))

    def test_planner_is_root(self) -> None:
        g = build_report_graph(topic="储能", sections=2)
        roots = [n.id for n in g if not n.deps]
        self.assertEqual(roots, ["planner"])

    def test_topological_order_respects_deps(self) -> None:
        g = build_report_graph(topic="储能", sections=3)
        order = [n.id for n in g.topological_order()]
        pos = {nid: i for i, nid in enumerate(order)}
        self.assertLess(pos["planner"], pos["research_ch1"])
        self.assertLess(pos["research_ch3"], pos["write_ch3"])
        self.assertLess(pos["write_ch2"], pos["review_ch2"])
        self.assertLess(pos["review_ch1"], pos["write_ch2"])

    def test_rejects_zero_sections(self) -> None:
        with self.assertRaises(ValueError):
            build_report_graph(topic="x", sections=0)

    def test_review_free_arm_has_no_review_nodes(self) -> None:
        """评测 B 组（多 Agent 无 Reviewer）：只有 write，串行链挂在 write 上。"""
        g = build_report_graph(topic="x", sections=3, with_review=False)
        self.assertNotIn(NodeType.REVIEW, {n.type for n in g})
        self.assertEqual(g.get("write_ch2").deps, ("research_ch2", "write_ch1"))

    def test_state_declares_reducers_for_parallel_fields(self) -> None:
        """并行写入的字段必须带 reducer，否则同一超步的并发更新会互相覆盖。"""
        from typing import get_type_hints

        # TypedDict 开着 `from __future__ import annotations`，__annotations__ 拿到的是
        # 字符串，必须用 get_type_hints(include_extras=True) 才能取回 Annotated 元数据
        hints = get_type_hints(ReportState, include_extras=True)
        for field in ("findings", "chapters", "reviews", "review_round", "reopened"):
            self.assertIn(field, hints)
            meta = getattr(hints[field], "__metadata__", ())
            self.assertTrue(meta, f"{field} 缺少 Annotated reducer")


# ---------------------------------------------------------------- 并行 / 串行


class TestParallelAndSerial(GraphTestBase):
    def test_research_parallel_tail_serial(self) -> None:
        rt, rec = self.runtime()
        task_id, _ = rt.runner.submit(topic="储能", sections=5)
        result = asyncio.run(rt.runner.run(task_id))

        self.assertIs(result.task_status, TaskStatus.DONE)
        all_executed = [nid for r in rec.values() for nid in r.executed()]
        self.assertEqual(len(all_executed), 16)

        research_overlap = max(r.max_overlap("research") for r in rec.values())
        write_overlap = max(r.max_overlap("write") for r in rec.values())
        review_overlap = max(r.max_overlap("review") for r in rec.values())

        # 5 个 research 节点，并发上限 3 → 观察到的最大重叠必须正好是 3
        self.assertEqual(research_overlap, 3)
        # write / review 串行：任何时刻只有一个在跑
        self.assertEqual(write_overlap, 1)
        self.assertEqual(review_overlap, 1)

    def test_concurrency_limit_is_respected(self) -> None:
        settings = self._settings(max_concurrency=2)
        rt, rec = self.runtime(settings=settings)
        task_id, _ = rt.runner.submit(topic="储能", sections=5)
        asyncio.run(rt.runner.run(task_id))
        self.assertLessEqual(max(r.max_overlap("research") for r in rec.values()), 2)

    def test_serial_tail_orders_chapters(self) -> None:
        """第 N 章 review 必须在第 N+1 章 write 之前完成（跨章反馈的前提）。"""
        rt, rec = self.runtime()
        task_id, _ = rt.runner.submit(topic="储能", sections=3)
        asyncio.run(rt.runner.run(task_id))

        events: list[tuple[float, str]] = []
        for r in rec.values():
            for nid, start, end in r.intervals:
                events.append((start, f"{nid}:start"))
                events.append((end, f"{nid}:end"))
        events.sort()

        def position(label: str) -> int:
            return next(i for i, (_, lab) in enumerate(events) if lab == label)

        for i in range(1, 3):
            self.assertLess(
                position(f"review_ch{i}:end"),
                position(f"write_ch{i + 1}:start"),
                f"review_ch{i} 必须先于 write_ch{i + 1} 完成",
            )

    def test_research_can_read_planner_outline(self) -> None:
        """research 通过 nodes 表拿到 planner 产出，而不是依赖 state 的全局字段。"""
        rt, _ = self.runtime()
        task_id, _ = rt.runner.submit(topic="储能", sections=2)
        asyncio.run(rt.runner.run(task_id))
        nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        self.assertTrue(nodes["planner"].output["outline"])
        self.assertTrue(nodes["research_ch1"].output["key_facts"])

    def test_without_review_still_completes(self) -> None:
        """with_review=False（评测 B 组）：没有 Reviewer 也要能跑完。"""
        rt, rec = self.runtime()
        task_id, _ = rt.runner.submit(topic="储能", sections=3, with_review=False)
        result = asyncio.run(rt.runner.run(task_id))
        self.assertIs(result.task_status, TaskStatus.DONE)
        nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        self.assertNotIn("review_ch1", nodes)
        self.assertIs(nodes["write_ch3"].status, NodeStatus.DONE)


# ---------------------------------------------------------------- 失败与阻塞


class TestBlockPropagation(GraphTestBase):
    def test_failed_node_blocks_all_downstream(self) -> None:
        rt, rec = self.runtime(fail_nodes={"research_ch2"})
        task_id, _ = rt.runner.submit(topic="储能", sections=5)
        result = asyncio.run(rt.runner.run(task_id))

        nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        self.assertIs(nodes["research_ch2"].status, NodeStatus.FAILED)
        for nid in [
            "write_ch2",
            "review_ch2",
            "write_ch3",
            "review_ch3",
            "write_ch5",
            "review_ch5",
        ]:
            self.assertIs(nodes[nid].status, NodeStatus.BLOCKED, nid)
        # 上游失败不影响同层兄弟：ch1 仍然完成
        self.assertIs(nodes["research_ch1"].status, NodeStatus.DONE)
        self.assertIs(nodes["review_ch1"].status, NodeStatus.DONE)
        # 任务保留已完成章节
        self.assertIs(result.task_status, TaskStatus.PARTIAL)
        # BLOCKED 的节点一次都没被执行
        executed = {nid for r in rec.values() for nid in r.executed()}
        self.assertNotIn("write_ch2", executed)
        self.assertNotIn("review_ch3", executed)

    def test_retry_exhausted_marks_failed(self) -> None:
        rt, _ = self.runtime(fail_nodes={"research_ch1"})
        task_id, _ = rt.runner.submit(topic="储能", sections=2)
        asyncio.run(rt.runner.run(task_id))
        node = {n.node_id: n for n in rt.store.list_nodes(task_id)}["research_ch1"]
        self.assertIs(node.status, NodeStatus.FAILED)
        self.assertEqual(node.attempt, 2)  # max_retries=1 → 共 2 次尝试
        self.assertIn("injected failure", node.error or "")

    def test_no_retry_exception_is_not_retried(self) -> None:
        """带 no_retry 标记的异常（如 ReplayMiss）必须一次就判死，不进退避重试。"""
        rt, _ = self.runtime()
        inner = {t: rt.registry.get(t) for t in NodeType}
        calls = {"n": 0}

        class _Permanent:
            node_type = NodeType.WRITE

            async def execute(self, ctx: AgentContext) -> dict[str, Any]:
                calls["n"] += 1

                class _Boom(RuntimeError):
                    no_retry = True

                raise _Boom("确定性失败")

        rt.runner.registry = AgentRegistry({**inner, NodeType.WRITE: _Permanent()})
        task_id, _ = rt.runner.submit(topic="x", sections=1)
        asyncio.run(rt.runner.run(task_id))

        node = {n.node_id: n for n in rt.store.list_nodes(task_id)}["write_ch1"]
        self.assertIs(node.status, NodeStatus.FAILED)
        self.assertEqual(node.attempt, 1)
        self.assertEqual(calls["n"], 1)


# ---------------------------------------------------------------- 断点续跑


class TestResume(GraphTestBase):
    def test_crash_then_resume_does_not_rerun_done(self) -> None:
        rt, rec1 = self.runtime()
        task_id, _ = rt.runner.submit(topic="储能", sections=5)

        with self.assertRaises(SystemExit):
            asyncio.run(rt.runner.run(task_id, crash_before="write_ch3"))

        nodes_after_crash = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        done_before = {
            nid for nid, n in nodes_after_crash.items() if n.status is NodeStatus.DONE
        }
        self.assertTrue(done_before, "崩溃前应至少完成一些节点")
        self.assertIs(nodes_after_crash["write_ch3"].status, NodeStatus.RUNNING)

        # 新进程：全新 runtime + 全新记录器，指向同一个数据库
        rt2, rec2 = self.runtime()
        result = asyncio.run(rt2.runner.run(task_id))

        executed_again = set(rec2[NodeType.PLANNER].executed())
        for r in rec2.values():
            executed_again |= set(r.executed())

        self.assertFalse(
            done_before & executed_again,
            f"已完成的节点被重跑: {sorted(done_before & executed_again)}",
        )
        self.assertIn("write_ch3", executed_again)  # 孤儿节点被重置并重跑
        self.assertIs(result.task_status, TaskStatus.DONE)

    def test_outputs_survive_restart(self) -> None:
        rt, _ = self.runtime()
        task_id, _ = rt.runner.submit(topic="储能", sections=2)
        asyncio.run(rt.runner.run(task_id))
        before = {n.node_id: dict(n.output or {}) for n in rt.store.list_nodes(task_id)}

        rt2, _ = self.runtime()
        result = asyncio.run(rt2.runner.run(task_id))
        after = {n.node_id: dict(n.output or {}) for n in rt2.store.list_nodes(task_id)}
        self.assertEqual(before, after)
        self.assertEqual(result.note, "already done")

    def test_reset_failed_allows_recovery(self) -> None:
        rt, _ = self.runtime(fail_nodes={"research_ch2"})
        task_id, _ = rt.runner.submit(topic="储能", sections=3)
        asyncio.run(rt.runner.run(task_id))

        rt2, _ = self.runtime()  # 故障已移除
        result = asyncio.run(rt2.runner.run(task_id, reset_failed=True))
        self.assertIs(result.task_status, TaskStatus.DONE)

    def test_checkpoint_file_is_created(self) -> None:
        """checkpoint 与任务库分离存放：LangGraph 自己建表，不污染 nodes 表。"""
        rt, _ = self.runtime()
        task_id, _ = rt.runner.submit(topic="储能", sections=2)
        asyncio.run(rt.runner.run(task_id))
        from app.orchestrator.runner import checkpoint_path_for

        path = checkpoint_path_for(self.settings, task_id)
        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(path.is_file(), "checkpoint 文件应已落盘")

    def test_thread_id_is_deterministic(self) -> None:
        from app.orchestrator.runner import TaskRunner

        self.assertEqual(TaskRunner._thread_id("abc"), TaskRunner._thread_id("abc"))
        self.assertNotEqual(TaskRunner._thread_id("abc"), TaskRunner._thread_id("abd"))


# ---------------------------------------------------------------- 打回闭环


class _ReviseAgents:
    """review 前 N 次判 revise，之后 pass。用来验证打回闭环能否收敛。"""

    def __init__(self, *, revise_times: int, fix_target: str = "write") -> None:
        self.revise_times = revise_times
        #: "write" 改稿 / "research" 重新检索。后者是踩过死循环的那条路径。
        self.fix_target = fix_target
        self.review_calls: dict[int, int] = {}
        self.write_calls: dict[int, int] = {}

    def registry(self) -> AgentRegistry:
        def planner(ctx: AgentContext) -> dict[str, Any]:
            return {
                "outline": [
                    {"chapter": i, "title": f"第{i}章", "queries": [f"q{i}"]}
                    for i in (1, 2)
                ]
            }

        def research(ctx: AgentContext) -> dict[str, Any]:
            ch = int(ctx.node.payload["chapter"])
            return {
                "chapter": ch,
                "summary": "s",
                "key_facts": [
                    {
                        "claim": f"第{ch}章要点",
                        "source_id": f"doc_{ch:02d}",
                        "page": 10,
                        "quote": f"第{ch}章原文出处",
                    }
                ],
                "open_questions": [],
                "dropped_facts": 0,
            }

        def write(ctx: AgentContext) -> dict[str, Any]:
            ch = int(ctx.node.payload["chapter"])
            self.write_calls[ch] = self.write_calls.get(ch, 0) + 1
            return {
                "chapter": ch,
                "markdown": f"## 第{ch}章\n\n正文[^doc_{ch:02d}]",
                "citation_map": {f"doc_{ch:02d}": 10},
                "used_facts": [],
            }

        def review(ctx: AgentContext) -> dict[str, Any]:
            ch = int(ctx.node.payload["chapter"])
            n = self.review_calls.get(ch, 0) + 1
            self.review_calls[ch] = n
            if n <= self.revise_times:
                return {
                    "chapter": ch,
                    "verdict": "revise",
                    "defects": [
                        {
                            "type": "assertion_mismatch",
                            "claim": f"第{ch}章要点",
                            "reason": "与原文不一致（模拟）",
                            "fix_target": self.fix_target,
                        }
                    ],
                    "fix_target": self.fix_target,
                    "hint": "请按原文修正数字",
                }
            return {"chapter": ch, "verdict": "pass", "defects": []}

        class _Node:
            def __init__(self, fn: Any, node_type: NodeType) -> None:
                self._fn = fn
                self.node_type = node_type

            async def execute(self, ctx: AgentContext) -> dict[str, Any]:
                return self._fn(ctx)

        return AgentRegistry(
            {
                NodeType.PLANNER: _Node(planner, NodeType.PLANNER),
                NodeType.RESEARCH: _Node(research, NodeType.RESEARCH),
                NodeType.WRITE: _Node(write, NodeType.WRITE),
                NodeType.REVIEW: _Node(review, NodeType.REVIEW),
            }
        )


class TestRevisionLoop(GraphTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = self._settings(max_concurrency=2, max_retries=0, max_review_rounds=2)

    def _run(self, agents: _ReviseAgents):
        rt = build_runtime(self.settings, profile="mock", announce=False)
        rt.runner.registry = agents.registry()
        task_id, _ = rt.runner.submit(topic="x", sections=2)
        result = asyncio.run(rt.runner.run(task_id))
        return rt, task_id, result

    def test_revise_triggers_rewrite_and_converges(self) -> None:
        agents = _ReviseAgents(revise_times=1)
        rt, task_id, result = self._run(agents)
        nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        self.assertEqual(nodes["write_ch1"].attempt, 2, "被打回后应重写一次")
        self.assertEqual(nodes["review_ch1"].review_round, 1)
        self.assertIn("按原文修正", nodes["write_ch1"].revision_hint or "")
        self.assertIs(result.task_status, TaskStatus.DONE)
        self.assertEqual(agents.write_calls[1], 2)

    def test_gives_up_at_cap_and_marks_partial(self) -> None:
        agents = _ReviseAgents(revise_times=99)
        rt, task_id, result = self._run(agents)
        nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        self.assertEqual(
            nodes["review_ch1"].review_round, 2, "打回轮数应停在 max_review_rounds"
        )
        self.assertIs(result.task_status, TaskStatus.PARTIAL)
        # 收敛性：每章最多 max_review_rounds+1 次 review
        self.assertLessEqual(agents.review_calls[1], 3)

    def test_revision_does_not_rerun_research(self) -> None:
        """fix_target=write 时不该把 research 一起重跑（否则白烧检索成本）。"""
        agents = _ReviseAgents(revise_times=1)
        rt, task_id, _ = self._run(agents)
        nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        self.assertEqual(nodes["research_ch1"].attempt, 1)

    def test_fix_target_research_terminates_instead_of_looping(self) -> None:
        """fix_target=research 必须能收敛，不能撞上 recursion_limit。

        这是评测 C 档实测暴露的真 bug：打回时把 `research_ch{i}` 重置成 PENDING，
        但条件边只回到 `write_ch{i}`——而 research 在图上唯一的入边来自 planner，
        planner 早已完成、不会再触发它。于是 research 永久 PENDING，write 的闸门
        一直等不到依赖、review 反复重试，跑满 200 步后抛 `GraphRecursionError`。
        表现是整条 C 档某主题直接 ERROR，且只有"建议重检"这一小部分样本会中招，
        很容易被当成偶发。

        修法：条件边记录"回到哪一层"，fix_target=research 时回到 research 节点。
        """
        agents = _ReviseAgents(revise_times=1, fix_target="research")
        rt, task_id, result = self._run(agents)

        nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        # 关键断言：所有节点都到达终态，没有卡在 PENDING
        pending = [k for k, n in nodes.items() if n.status is NodeStatus.PENDING]
        self.assertEqual(pending, [], f"仍有节点卡在 PENDING: {pending}")
        self.assertIs(result.task_status, TaskStatus.DONE)
        # research 确实被重跑过一次（说明重检路径真的走通了，而不是被跳过）
        self.assertEqual(nodes["research_ch1"].attempt, 2)
        # 且整条链收敛：review 调用次数有限（1 次 revise + 1 次 pass）
        self.assertEqual(agents.review_calls[1], 2)

    def test_fix_target_research_gives_up_at_cap(self) -> None:
        """一直判重检时也必须收敛到 PARTIAL，不能无限循环。"""
        agents = _ReviseAgents(revise_times=99, fix_target="research")
        rt, task_id, result = self._run(agents)
        nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        self.assertEqual(nodes["review_ch1"].review_round, 2)
        self.assertIs(result.task_status, TaskStatus.PARTIAL)
        pending = [k for k, n in nodes.items() if n.status is NodeStatus.PENDING]
        self.assertEqual(pending, [], f"仍有节点卡在 PENDING: {pending}")


class TestTopologyMatrix(GraphTestBase):
    """四种拓扑组合都必须把**每一章**跑完。

    这一组是回归测试，钉住一个真实踩过的严重 bug：

    `serial_tail=True + with_review=False`（评测 B 组的口径）下，
    `runner.py` 原本从落库的 deps 反推 `serial_tail`，判据写成"deps 里是否含 review_ch"。
    B 组没有 Reviewer 节点，那条依赖实际是 `write_ch{i-1}`，**判据恒为 False**，
    于是串行尾部被误判成流水线模式、图按流水线拓扑编译：下游章节被提前派发，
    闸门看到上游还在 RUNNING 只能让路，而 LangGraph 不会再给第二次触发机会，
    于是 write_ch2 及之后**永久停在 PENDING**，任务以 PARTIAL 收尾。
    实测现象：默认配置下 5 章任务只生成第 1 章，而且不报任何错。

    修法是改判据为"`write_ch{i}`(i>1) 的 deps 是否多于 1 个"——它对"有没有 Reviewer"
    不敏感。这个 bug 静默失败、不报错，只会让报告少几章，所以必须用测试锁住；
    四种 `serial_tail × with_review` 组合都要"每章跑完"才算过。
    """

    def _run(self, serial_tail: bool, with_review: bool, sections: int = 3):
        rt, _ = self.runtime()
        task_id, _ = rt.runner.submit(
            topic="储能",
            sections=sections,
            with_review=with_review,
            serial_tail=serial_tail,
        )
        result = asyncio.run(rt.runner.run(task_id))
        return rt, task_id, result

    def test_b_arm_serial_tail_completes_every_chapter(self) -> None:
        """评测 B 组口径（多 Agent、无 Reviewer、串行尾部）。"""
        rt, task_id, result = self._run(serial_tail=True, with_review=False)
        nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        self.assertIs(result.task_status, TaskStatus.DONE)
        # 1 planner + 3 research + 3 write
        self.assertEqual(len(nodes), 7)
        pending = [k for k, n in nodes.items() if n.status is not NodeStatus.DONE]
        self.assertEqual(pending, [], f"这些节点没跑完: {pending}")
        for i in (1, 2, 3):
            self.assertIn(f"write_ch{i}", nodes)

    def test_pipeline_with_review_completes_every_chapter(self) -> None:
        rt, task_id, result = self._run(serial_tail=False, with_review=True)
        nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        self.assertIs(result.task_status, TaskStatus.DONE)
        self.assertEqual(len(nodes), 10)
        pending = [k for k, n in nodes.items() if n.status is not NodeStatus.DONE]
        self.assertEqual(pending, [])

    def test_pipeline_without_review_completes_every_chapter(self) -> None:
        rt, task_id, result = self._run(serial_tail=False, with_review=False)
        nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        self.assertIs(result.task_status, TaskStatus.DONE)
        self.assertEqual(len(nodes), 7)
        pending = [k for k, n in nodes.items() if n.status is not NodeStatus.DONE]
        self.assertEqual(pending, [])

    def test_full_matrix_all_chapters_done(self) -> None:
        """四种组合逐一跑，任何一种少一章都算失败。"""
        for serial_tail in (True, False):
            for with_review in (True, False):
                with self.subTest(serial_tail=serial_tail, with_review=with_review):
                    rt, task_id, result = self._run(serial_tail, with_review)
                    nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
                    self.assertIs(result.task_status, TaskStatus.DONE)
                    bad = [
                        k for k, n in nodes.items() if n.status is not NodeStatus.DONE
                    ]
                    self.assertEqual(bad, [], f"未完成: {bad}")


if __name__ == "__main__":
    unittest.main()
