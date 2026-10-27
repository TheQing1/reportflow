"""Writer：只用素材包里的 fact 写正文，并保证角标可映射。

两条程序侧硬约束（不依赖模型自觉）：
1. 正文里的 `[^doc_07]` 必须命中素材包里真实存在的 source_id，否则**剥掉这个角标**
   并计入 `invalid_citations`——留着假引用比没有引用更糟，它会让读者以为有出处。
2. `citation_map` 由程序根据 fact 的 page 重新生成，不用模型返回的那份。
   模型经常把页码抄错，而页码是"可溯源"的最后一环。
"""

from __future__ import annotations

import re
from typing import Any

from ..contracts.agent import AgentContext
from ..contracts.handoff import ChapterDraft, HandoffError, ResearchPacket
from ..contracts.nodes import NodeType
from .base import JsonAgent

_CITE = re.compile(r"\[\^([^\]]+)\]")

_SYSTEM = """[task:writer] 你是研报撰写者。你的任务是把给定的素材改写成一段连贯的章节正文。

铁律：
1. 只能使用素材里的 fact，不得引入任何外部知识，不得补充素材中没有的数字、年份、公司名。
2. 每使用一条 fact，就在句末标注角标 [^source_id]，source_id 必须照抄素材里的值。
3. 素材不足以支撑某个论点时，用**一句话**在正文末尾概括"资料未涉及"的部分，
   不要逐条罗列缺口清单，更不要把缺口写成正文段落的主体——
   读者要看的是已确认的结论，不是缺什么。
4. 不要写"根据素材""如上所述"这类元话语，输出就是正文本身。
5. 400-600 字，markdown 格式，用 "## 标题" 起头。

只输出 JSON：
{"chapter":1,"markdown":"## 标题\\n\\n正文……","gaps":["缺少什么"]}
注意：gaps 字段是给下游的结构化信息，正文里不要复述它。"""


class WriterAgent(JsonAgent):
    node_type = NodeType.WRITE

    async def execute(self, ctx: AgentContext) -> dict[str, Any]:
        chapter = int(ctx.node.payload.get("chapter", 0))
        packet = ResearchPacket.from_dict(ctx.upstream_of(f"research_ch{chapter}"), chapter=chapter)

        if packet.is_empty:
            return self._insufficient(chapter, packet)

        data, _ = await self.ask_json(
            ctx.node.id,
            _SYSTEM,
            self._build_user(chapter, packet, ctx.hint),
            timeout_s=self.timeout_for(ctx.node.timeout_s),
        )
        draft = ChapterDraft.from_dict(data, chapter=chapter)
        draft, invalid = self._enforce_citations(draft, packet)
        if invalid:
            self._log.warning("[%s] 剥掉 %d 个无法映射到素材的角标", ctx.node.id, invalid)
        self._log.info(
            "[%s] 正文完成：%d 字，引用 %d 条 fact",
            ctx.node.id,
            len(draft.markdown),
            len(draft.used_facts),
        )
        return {**draft.to_dict(), "invalid_citations": invalid}

    # ---------------------------------------------------------------- 内部

    def _build_user(
        self,
        chapter: int,
        packet: ResearchPacket,
        hint: str | None,
    ) -> str:
        facts = "\n".join(
            f"{i}. claim: {f.claim}\n   source_id: {f.source_id}\n"
            f"   原文: {f.quote}\n"
            f"   {'页码: ' + str(f.page) if f.page else '无页码'}"
            for i, f in enumerate(packet.key_facts, 1)
        )
        parts = [
            f"第 {chapter} 章素材（共 {len(packet.key_facts)} 条）：\n\n{facts}",
            f"素材摘要：{packet.summary or '（无）'}",
        ]
        if packet.open_questions:
            parts.append("已知缺口：\n" + "\n".join(f"- {q}" for q in packet.open_questions))
        if hint:
            parts.append(f"上一轮审稿意见（本轮必须修正）：\n{hint}")
        parts.append(f"请输出第 {chapter} 章的 JSON。")
        return "\n\n".join(parts)

    def _enforce_citations(
        self, draft: ChapterDraft, packet: ResearchPacket
    ) -> tuple[ChapterDraft, int]:
        # 同一篇文档往往产出多条 fact，这里必须按 source_id 分组保存全部，
        # 不能 `{f.source_id: f}`——那样同源只剩最后一条，
        # 正文引用了第 1 条的数字，下游拿第 3 条的原文去核对，必然判成"编造"。
        allowed: dict[str, list] = {}
        for fact in packet.key_facts:
            allowed.setdefault(fact.source_id, []).append(fact)

        markdown = draft.markdown
        invalid = 0

        def _strip(match: re.Match[str]) -> str:
            nonlocal invalid
            token = match.group(1).strip()
            if token in allowed:
                return match.group(0)
            invalid += 1
            return ""

        cleaned = _CITE.sub(_strip, markdown)
        cited = set(_CITE.findall(cleaned))
        used = tuple(fact for src in sorted(cited) if src in allowed for fact in allowed[src])

        if not cleaned.lstrip().startswith("#"):
            cleaned = f"## 第 {draft.chapter} 章\n\n{cleaned}"

        # 页码以素材为准，不采信模型输出；同源多条取首条的页码
        citation_map = {src: allowed[src][0].page for src in sorted(cited) if src in allowed}
        return (
            ChapterDraft(
                chapter=draft.chapter,
                markdown=cleaned,
                citation_map=citation_map,
                gaps=draft.gaps,
                used_facts=used,
            ),
            invalid,
        )

    def _insufficient(self, chapter: int, packet: ResearchPacket) -> dict[str, Any]:
        """素材为空时不调用模型：让模型对着空素材写，它一定会编。"""
        gaps = list(packet.open_questions) or ["知识库中无相关材料"]
        markdown = (
            f"## 第 {chapter} 章\n\n"
            "资料未涉及。本章所需的支撑材料未能在知识库中检索到，"
            "为避免给出无出处的结论，此处留空待补。\n"
        )
        self._log.warning("[ch%d] 素材为空，直接输出「资料未涉及」", chapter)
        return {
            "chapter": chapter,
            "markdown": markdown,
            "citation_map": {},
            "gaps": gaps,
            "used_facts": [],
            "invalid_citations": 0,
            "insufficient_evidence": True,
        }


__all__ = ["WriterAgent", "HandoffError"]
