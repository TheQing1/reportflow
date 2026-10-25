"""DAG 构建：把「写一份 N 章研报」翻译成节点图。

拓扑（stage-1 默认 serial_tail=True）：

    planner
      ├─ research_ch1 ─┐
      ├─ research_ch2 ─┤  （全部并行，受并发上限约束）
      ├─ ...           │
      └─ research_ch5 ─┘
    research_ch1 → write_ch1 → review_ch1 → write_ch2 → review_ch2 → ... → review_ch5

为什么尾部串行（而不是 write_chN 只依赖 research_chN）：
1. 阶段一验收要求 write/review 串行可观测；
2. 顺序写作让第 N 章的 review 反馈能约束第 N+1 章，跨章衔接更稳；
3. 代价是没有流水线重叠——research 早已跑完，尾部只能一章一章走。
   需要流水线时传 serial_tail=False，图会自动变成 write_chN 仅依赖 research_chN，
   这时并行度由并发上限决定，其余代码不变。
"""

from __future__ import annotations

from ..contracts.nodes import Graph, NodeSpec, NodeType


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

    prev_review: str | None = None
    for i in range(1, sections + 1):
        write_deps = [f"research_ch{i}"]
        if serial_tail and prev_review:
            write_deps.append(prev_review)
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
            prev_review = f"review_ch{i}"
        else:
            # 评测 B 组（多 Agent 无 Reviewer）：串行链改挂在 write 自己身上，
            # 保证与 C 组只有"有没有 review"这一个变量不同
            prev_review = f"write_ch{i}"

    graph = Graph.of(nodes)
    graph.validate()
    return graph
