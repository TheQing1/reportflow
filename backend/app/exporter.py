"""把节点产出拼成一份可读的 markdown 初稿。

CLI 和 HTTP 接口都要用同一份拼装逻辑——分开实现必然会在某次改动后悄悄分叉
（比如一边的引用来源去重规则改了，另一边没改），而用户看到的是两份不同的稿子。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .contracts.task import NodeState, TaskState


@dataclass(frozen=True)
class BuiltReport:
    markdown: str
    chapters: list[str]
    #: 引用来源 -> 脚注行
    references: dict[str, str]


def _node_map(nodes: Iterable[NodeState]) -> dict[str, NodeState]:
    return {n.node_id: n for n in nodes}


def build_report(task: TaskState, nodes: Iterable[NodeState]) -> BuiltReport:
    by_id = _node_map(nodes)
    chapters: list[str] = []
    refs: dict[str, str] = {}

    for i in range(1, task.sections + 1):
        node = by_id.get(f"write_ch{i}")
        if not node or not node.output:
            status = node.status.value if node else "缺失"
            chapters.append(f"## 第 {i} 章\n\n（未生成：{status}）\n")
            continue
        chapters.append(str(node.output.get("markdown", "")).strip() + "\n")

        research = by_id.get(f"research_ch{i}")
        facts: Iterable[Mapping] = (
            research.output.get("key_facts", []) if research and research.output else []
        )
        for fact in facts:
            src = str(fact.get("source_id"))
            page = f" P.{fact['page']}" if fact.get("page") else ""
            # 同一来源被多章引用时保留首次出现的页码，避免后写的覆盖前面的
            refs.setdefault(
                src, f"[^{src}]: {src}{page} — {str(fact.get('quote', ''))[:80]}"
            )

    body = [
        f"# {task.topic}",
        "",
        f"> 状态：{task.status.value} · 章节数：{task.sections} · 任务：`{task.id}`",
        "",
        "\n".join(chapters),
    ]
    if refs:
        body += ["", "## 引用来源", "", "\n".join(sorted(refs.values()))]
    return BuiltReport(markdown="\n".join(body), chapters=chapters, references=refs)


__all__ = ["BuiltReport", "build_report"]
