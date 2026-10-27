"""Planner：把主题拆成章节大纲 + 检索式。

约束落地：
- 章节数必须与请求一致（少一章就少一个 write 节点可用，DAG 是预先建好的）；
- query 必须是可检索的名词短语，不能是问句——关键词检索对问句的命中率很差，
  而且 LLM 很容易把"为什么储能增长这么快"当成 query 交上来。
"""

from __future__ import annotations

from typing import Any

from ..contracts.agent import AgentContext
from ..contracts.handoff import HandoffError
from ..contracts.nodes import NodeType
from .base import JsonAgent

_SYSTEM = """[task:planner] 你是资深行业研究员，负责把研究主题拆成章节大纲，并为每章设计检索式。

硬性要求：
1. 章节数量必须等于用户指定数量，chapter 从 1 连续编号。
2. **每章必须聚焦一个互不重叠的分析维度**，例如：市场规模 / 区域分布 / 竞争格局 /
   驱动因素 / 风险与展望。禁止两章同时以"规模"或"区域"为主线——
   重复的章节等于让读者读两遍同一段内容。
3. 每章给出 2-3 条检索式（queries），必须是**名词短语**，用于关键词检索；
   不要用问句，不要带问号，不要写成"分析XX的原因"这类动词句式。
   不同章节的 queries 要有明显差异，指向不同的数据和角度。
4. 章节之间要有递进关系，不要同义反复。

只输出 JSON，格式：
{"chapters":[{"chapter":1,"title":"章节标题","queries":["检索式1","检索式2"]}]}"""


class PlannerAgent(JsonAgent):
    node_type = NodeType.PLANNER

    async def execute(self, ctx: AgentContext) -> dict[str, Any]:
        topic = str(ctx.node.payload.get("topic", "")).strip()
        sections = int(ctx.node.payload.get("sections", 5))
        if not topic:
            raise HandoffError("planner 缺少 topic")

        user = f"研究主题：{topic}\n章节数量：{sections}"
        data, _resp = await self.ask_json(
            ctx.node.id,
            _SYSTEM,
            user,
            timeout_s=self.timeout_for(ctx.node.timeout_s),
        )
        outline = self._normalize(data, sections=sections, topic=topic)
        self._log.info(
            "[%s] 大纲完成：%d 章 | %s",
            ctx.node.id,
            len(outline),
            " / ".join(c["title"] for c in outline),
        )
        return {"topic": topic, "sections": sections, "outline": outline}

    @staticmethod
    def _normalize(data: dict[str, Any], *, sections: int, topic: str) -> list[dict[str, Any]]:
        chapters = data.get("chapters")
        if not isinstance(chapters, list) or not chapters:
            raise HandoffError("planner 输出缺少 chapters 数组")
        if len(chapters) != sections:
            raise HandoffError(
                f"章节数不符：要求 {sections}，实际 {len(chapters)}（DAG 已按 {sections} 建好）"
            )

        outline: list[dict[str, Any]] = []
        for idx, item in enumerate(chapters, start=1):
            if not isinstance(item, dict):
                raise HandoffError(f"第 {idx} 章格式非法")
            title = str(item.get("title", "")).strip() or f"{topic} · 第 {idx} 部分"
            queries = []
            for q in item.get("queries") or []:
                text = str(q).strip().rstrip("？?").strip()
                if text:
                    queries.append(text)
            if not queries:
                # 不是硬失败：退回章节标题当检索式，比整章中断划算
                queries = [title]
            outline.append({"chapter": idx, "title": title, "queries": queries[:4]})
        return outline
