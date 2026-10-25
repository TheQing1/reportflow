"""DAG 构建与静态校验。"""

from __future__ import annotations

import unittest

from app.contracts.nodes import Graph, GraphError, NodeSpec, NodeType
from app.orchestrator.dag import build_report_graph


class TestBuildReportGraph(unittest.TestCase):
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


class TestGraphValidation(unittest.TestCase):
    def test_cycle_detected(self) -> None:
        a = NodeSpec(id="a", type=NodeType.WRITE, deps=("b",))
        b = NodeSpec(id="b", type=NodeType.WRITE, deps=("a",))
        with self.assertRaises(GraphError) as ctx:
            Graph.of([a, b]).validate()
        self.assertIn("环", str(ctx.exception))

    def test_unknown_dep(self) -> None:
        a = NodeSpec(id="a", type=NodeType.WRITE, deps=("ghost",))
        with self.assertRaises(GraphError):
            Graph.of([a]).validate()

    def test_duplicate_id(self) -> None:
        a = NodeSpec(id="a", type=NodeType.WRITE)
        b = NodeSpec(id="a", type=NodeType.REVIEW)
        with self.assertRaises(GraphError):
            Graph.of([a, b]).validate()

    def test_self_dependency_rejected(self) -> None:
        with self.assertRaises(ValueError):
            NodeSpec(id="a", type=NodeType.WRITE, deps=("a",))


if __name__ == "__main__":
    unittest.main()
