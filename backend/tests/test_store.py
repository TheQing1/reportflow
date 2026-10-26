"""持久化层：状态迁移的原子性与幂等。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app.core.timeutil import utcnow
from app.orchestrator.dag import build_report_graph
from app.persistence import SqlTaskStore, build_engine, build_session_factory, create_schema
from app.persistence.store import TaskExistsError


class TestSqlTaskStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="reportflow-store-"))
        engine = build_engine(f"sqlite:///{(self.tmpdir / 's.db').as_posix()}")
        create_schema(engine)
        self.store = SqlTaskStore(build_session_factory(engine))
        self.graph = build_report_graph(topic="储能", sections=3)
        self.store.create_task(
            task_id="t1", topic="储能", sections=3, graph=self.graph
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_duplicate_create_rejected(self) -> None:
        with self.assertRaises(TaskExistsError):
            self.store.create_task(
                task_id="t1", topic="储能", sections=3, graph=self.graph
            )

    def test_load_graph_roundtrip(self) -> None:
        loaded = self.store.load_graph("t1")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(
            {n.id: n.deps for n in loaded}, {n.id: n.deps for n in self.graph}
        )
        self.assertEqual(loaded.get("write_ch2").deps, ("research_ch2", "review_ch1"))

    def test_start_node_is_atomic(self) -> None:
        ok1 = self.store.start_node(
            task_id="t1", node_id="planner", attempt=1, owner="a"
        )
        ok2 = self.store.start_node(
            task_id="t1", node_id="planner", attempt=1, owner="b"
        )
        self.assertTrue(ok1)
        self.assertFalse(ok2, "同一 (node, attempt) 只能被抢一次")

    def test_finish_requires_running(self) -> None:
        ok = self.store.finish_node(
            task_id="t1", node_id="planner", attempt=1, output={"a": 1}
        )
        self.assertFalse(ok, "未抢占的节点不允许直接置 DONE")

    def test_fail_then_retry_increments_attempt(self) -> None:
        self.store.start_node(task_id="t1", node_id="planner", attempt=1, owner="a")
        self.store.fail_node(
            task_id="t1",
            node_id="planner",
            attempt=1,
            error="boom",
            retry_at=utcnow() + timedelta(seconds=30),
        )
        node = {n.node_id: n for n in self.store.list_nodes("t1")}["planner"]
        self.assertIs(node.status.value, "RETRYING")
        self.assertEqual(node.attempt, 2)

        promoted = self.store.promote_due_retries(task_id="t1")
        self.assertEqual(promoted, 0, "退避未到期不应被提前放行")

    def test_claim_orphans_resets_running(self) -> None:
        self.store.start_node(task_id="t1", node_id="planner", attempt=1, owner="dead")
        n = self.store.claim_orphans(task_id="t1", owner="alive")
        self.assertEqual(n, 1)
        node = {x.node_id: x for x in self.store.list_nodes("t1")}["planner"]
        self.assertIs(node.status.value, "PENDING")
        self.assertEqual(node.attempt, 1, "孤儿重置不应改变幂等键")

    def test_block_does_not_touch_done(self) -> None:
        self.store.start_node(task_id="t1", node_id="planner", attempt=1, owner="a")
        self.store.finish_node(
            task_id="t1", node_id="planner", attempt=1, output={"ok": True}
        )
        self.assertFalse(
            self.store.block_node(task_id="t1", node_id="planner", reason="x")
        )
        node = {n.node_id: n for n in self.store.list_nodes("t1")}["planner"]
        self.assertIs(node.status.value, "DONE")


if __name__ == "__main__":
    unittest.main()
