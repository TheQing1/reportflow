"""Reviewer：先做程序化校验，再让模型做语义校验。

顺序不能反。程序能确定的事情（角标能不能映射、claim 里的数字在不在原文里）
交给程序做，又快又准又免费；只有"这句话算不算被原文支持"这种语义判断才交给模型。

为什么要分成两类缺陷（`fix_target`）：
- 数字/表述与原文不符 → Writer 改写时跑偏了 → 打回 write；
- 整章没有任何素材支撑 → 素材本来就缺 → 打回 research 换检索式。
一律打回 Writer 会出现"怎么写都过不了"的死循环，这是多 Agent 系统的典型失败模式。
"""

from __future__ import annotations

import re
from typing import Any

from ..contracts.agent import AgentContext
from ..contracts.handoff import (
    ChapterDraft,
    Defect,
    DefectType,
    FixTarget,
    ReviewVerdict,
    defects_to_hint,
)
from ..contracts.nodes import NodeType
from .base import JsonAgent

_NON_DIGIT = re.compile(r"[,\s，]")
# 只核对「有量纲」的数字：小数、百分数、带单位的量。
# 一开始把所有数字都拿去比对，结果「第 3 章」里的 3 被判成"原文找不到"，
# 白白打回了两轮——序号、章节号不是数据，不该参与事实核对。
_NUM_WITH_UNIT = re.compile(
    r"(?<!第)(\d+(?:\.\d+)?)\s*(%|％|GWh|MWh|kWh|GW|MW|亿元|亿|万元|万|元|倍|家|台)"
)
_NUM_DECIMAL = re.compile(r"(?<!第)(\d+\.\d+)")
#: 从正文里抽「一句话 + 它的角标」。断言必须取自正文，
#: 不能拿上游 claim 代替——否则会出现「正文写对了但上游概括偏了」被判错，
#: 打回重写也改不掉（Writer 重写的是正文，不是 claim），最后白烧两轮。
_SENT_WITH_CITE = re.compile(r"([^。！？\n\[\]]{4,}?)\[\^([^\]\s]+)\]")

_SYSTEM = """[task:reviewer] 你是事实核查员。只判断一件事：正文里的每条断言，是否被它引用的原文支持。

规则：
1. 只看「断言 + 原文」这一对输入，不要考虑作者可能怎么推理，也不要因为语言流畅就放过。
2. 原文没有明确支持的数字、年份、排名、因果关系，都算不支持。
3. 什么都不支持比漏判更严重：宁可报一条误报，也不要放过编造。
4. 全部通过时 verdict=pass；有任何问题 verdict=revise，并且**每条问题都必须给出**
   type（assertion_mismatch / missing_evidence）、claim、reason。

只输出 JSON：
{"chapter":1,"verdict":"pass","defects":[]}"""


class ReviewerAgent(JsonAgent):
    node_type = NodeType.REVIEW

    async def execute(self, ctx: AgentContext) -> dict[str, Any]:
        chapter = int(ctx.node.payload.get("chapter", 0))
        draft = ChapterDraft.from_dict(
            ctx.upstream_of(f"write_ch{chapter}"), chapter=chapter
        )

        program_defects = self._program_check(draft)
        precision = self._citation_precision(draft)

        if not program_defects and draft.insufficient_evidence:
            # 没有素材可核，再问一次模型只是烧钱。直接放行，并留下可查的说明。
            note = "本章素材不足，Writer 已显式声明「资料未涉及」"
            verdict = ReviewVerdict(
                chapter=chapter,
                verdict="pass",
                defects=(),
                citation_precision=0.0,
            )
            self._log.info("[%s] %s，跳过 LLM 校验", ctx.node.id, note)
            return {
                **verdict.to_dict(),
                "reviewed_by": "program",
                "note": note,
                "hint": "",
            }

        if program_defects:
            verdict = ReviewVerdict(
                chapter=chapter,
                verdict="revise",
                defects=tuple(program_defects),
                citation_precision=precision,
            )
            self._log.warning(
                "[%s] 程序化校验不通过（%d 条），跳过 LLM 语义校验",
                ctx.node.id,
                len(program_defects),
            )
            return {
                **verdict.to_dict(),
                "reviewed_by": "program",
                "hint": defects_to_hint(verdict.defects),
            }

        data, _ = await self.ask_json(
            ctx.node.id,
            _SYSTEM,
            self._build_user(chapter, draft),
            timeout_s=self.timeout_for(ctx.node.timeout_s),
        )
        verdict = ReviewVerdict.from_dict(data, chapter=chapter)
        resolved = ReviewVerdict(
            chapter=chapter,
            verdict=verdict.verdict,
            defects=verdict.defects,
            citation_precision=precision,
        )
        self._log.info(
            "[%s] 语义校验：%s（%d 条缺陷，引用准确率 %.2f）",
            ctx.node.id,
            resolved.verdict,
            len(resolved.defects),
            precision,
        )
        return {
            **resolved.to_dict(),
            "reviewed_by": "llm",
            "hint": defects_to_hint(resolved.defects) if not resolved.passed else "",
        }

    # ---------------------------------------------------------------- 程序校验

    def _program_check(self, draft: ChapterDraft) -> list[Defect]:
        defects: list[Defect] = []

        assertions = self._assertions(draft)
        if not draft.used_facts:
            if draft.insufficient_evidence or draft.gaps:
                # 已经声明过"资料未涉及"，这是被允许的交付形态：
                # 打回 research 只会重检同样空的知识库，形成"永远打回"的死循环
                return []
            defects.append(
                Defect(
                    type=DefectType.MISSING_EVIDENCE.value,
                    claim=draft.markdown[:80],
                    reason="本章未引用任何素材，且未声明资料缺口",
                    fix_target=FixTarget.RESEARCH.value,
                )
            )
            return defects

        by_source = {}
        for fact in draft.used_facts:
            by_source.setdefault(fact.source_id, []).append(fact)

        if not assertions:
            # 有素材但正文一个角标都没打——要么忘了标，要么整段在自说自话
            defects.append(
                Defect(
                    type=DefectType.MISSING_CITATION.value,
                    claim=draft.markdown[:80],
                    reason="正文引用了素材但没有任何角标，无法核对",
                    fix_target=FixTarget.WRITE.value,
                )
            )
            return defects

        for sentence, source_id in assertions:
            quotes = " ".join(f.quote for f in by_source.get(source_id, []))
            if not quotes:
                continue
            quote_digits = _NON_DIGIT.sub("", quotes)
            numbers = {n for n, _unit in _NUM_WITH_UNIT.findall(sentence)}
            numbers |= set(_NUM_DECIMAL.findall(sentence))
            missing = sorted(
                n for n in numbers if _NON_DIGIT.sub("", n) not in quote_digits
            )
            if missing:
                defects.append(
                    Defect(
                        type=DefectType.ASSERTION_MISMATCH.value,
                        claim=sentence[:80],
                        reason=f"正文中的数字 {missing} 在所引原文中找不到",
                        fix_target=FixTarget.WRITE.value,
                    )
                )
        return defects

    @staticmethod
    def _assertions(draft: ChapterDraft) -> list[tuple[str, str]]:
        """从正文抽「断言句子 + 它标注的来源」。

        这是 Reviewer 的输入口径，必须是**正文里的句子**。
        之前用 used_facts.claim 代替，导致判定对象错位：
        正文写对了、但 Researcher 的概括超出原文时，会被判错，
        而 Writer 重写正文根本改不掉那个 claim，于是反复打回直到上限。
        """
        found: list[tuple[str, str]] = []
        seen: set[str] = set()
        for match in _SENT_WITH_CITE.finditer(draft.markdown):
            sentence = match.group(1).strip().lstrip("-*# ").strip()
            source_id = match.group(2).strip()
            if len(sentence) < 4 or sentence in seen:
                continue
            seen.add(sentence)
            found.append((sentence, source_id))
        return found

    @staticmethod
    def _citation_precision(draft: ChapterDraft) -> float:
        """有效引用数 / 总角标数。Writer 已剥掉无效角标，所以这里通常是 1.0；
        保留这个计算是为了在"角标指向了未使用的 fact"等情况下还有兜底判断。"""
        cited = draft.cited_sources()
        if not cited:
            return 0.0
        known = {f.source_id for f in draft.used_facts}
        return round(len(cited & known) / len(cited), 3)

    def _build_user(self, chapter: int, draft: ChapterDraft) -> str:
        by_source: dict[str, list] = {}
        for fact in draft.used_facts:
            by_source.setdefault(fact.source_id, []).append(fact)

        rows = []
        for i, (sentence, source_id) in enumerate(self._assertions(draft), 1):
            quotes = " ‖ ".join(f.quote for f in by_source.get(source_id, []))
            rows.append(
                f"[{i}] 正文断言：{sentence}\n"
                f"    所引原文：{quotes or '（无对应原文）'}\n"
                f"    出处：{source_id}"
            )
        pairs = "\n\n".join(rows) or "（正文没有可核对的带角标句子）"
        return (
            f"第 {chapter} 章正文：\n{draft.markdown}\n\n"
            f"该章正文中的断言与所引原文对照（共 {len(rows)} 条）：\n{pairs}\n\n"
            f"请输出第 {chapter} 章的核查 JSON。"
        )
