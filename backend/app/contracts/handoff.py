"""Agent 之间的结构化交接协议（文档 §4 的落点）。

三条硬规则的实现：

1. `summary` 有长度上限：超了**压缩**而不是截断——截断会留半句话，
   下游据此写正文会写出残缺结论。这里用压缩（取首句 + 省略号 + 末句的简化实现），
   并在校验结果里标记，便于统计"上游不守规矩"的比例。
2. `quote` 必须原样保留、且有长度上限。压到 300 字以内，否则一条长段落
   就能把"不传对话历史"省下来的 token 全吃回去。
3. 下游只拿本结构，拿不到上游 messages —— 由 AgentContext 的字段类型保证。

另外这里承担"防幻觉第二道防线"的**结构侧**校验：fact 必须带 source_id 与 page。
语义侧校验（quote 是否真的出现在原文里）在 Researcher 里做，因为只有它持有原文。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

SUMMARY_MAX_CHARS = 200
QUOTE_MAX_CHARS = 300


class HandoffError(ValueError):
    """交接结构不合法。不合法就意味着下游必然产出不可信的正文，必须抛错而不是兜底。"""


def _clip_quote(text: str, limit: int = QUOTE_MAX_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2 - 3) :]
    return f"{head}……{tail}"


def _compress_summary(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    # 先按句号切，保留能被完整表达的部分；实在不行才做首尾拼接
    for sep in ("。", "；", ". "):
        head = text.split(sep)[0]
        if 0 < len(head) <= limit - 1:
            return head + sep.strip()
    return text[: limit - 1] + "…"


@dataclass(frozen=True)
class Fact:
    """一条可溯源的事实。字段名与前端角标、Reviewer 校验共用，不要随意改。"""

    claim: str
    source_id: str
    page: int | None
    quote: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Fact:
        claim = str(raw.get("claim", "")).strip()
        source_id = str(raw.get("source_id", "")).strip()
        quote = str(raw.get("quote", "")).strip()
        if not claim:
            raise HandoffError("fact.claim 为空")
        if not source_id:
            raise HandoffError(f"fact 缺少 source_id: {claim[:30]}")
        if not quote:
            raise HandoffError(f"fact 缺少 quote（引用必须有原文）: {claim[:30]}")
        page_raw = raw.get("page")
        page: int | None
        try:
            page = int(page_raw) if page_raw not in (None, "") else None
        except (TypeError, ValueError):
            page = None
        return cls(
            claim=claim, source_id=source_id, page=page, quote=_clip_quote(quote)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "source_id": self.source_id,
            "page": self.page,
            "quote": self.quote,
        }


@dataclass(frozen=True)
class ResearchPacket:
    """Researcher 的产出，也是 Writer 的唯一输入。"""

    chapter: int
    summary: str
    key_facts: tuple[Fact, ...] = ()
    open_questions: tuple[str, ...] = ()
    #: 程序化校验丢弃的 fact 数（quote 在原文里找不到），用于统计编造率
    dropped_facts: int = 0

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any], *, chapter: int | None = None
    ) -> ResearchPacket:
        ch = int(raw.get("chapter", chapter or 0))
        summary = _compress_summary(str(raw.get("summary", "")))
        facts_raw = raw.get("key_facts") or []
        if not isinstance(facts_raw, list):
            raise HandoffError("key_facts 必须是数组")

        facts: list[Fact] = []
        for item in facts_raw:
            if not isinstance(item, Mapping):
                continue
            facts.append(Fact.from_dict(item))

        questions = tuple(
            str(q).strip() for q in (raw.get("open_questions") or []) if str(q).strip()
        )
        return cls(
            chapter=ch,
            summary=summary,
            key_facts=tuple(facts),
            open_questions=questions,
            dropped_facts=int(raw.get("dropped_facts", 0) or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter": self.chapter,
            "summary": self.summary,
            "key_facts": [f.to_dict() for f in self.key_facts],
            "open_questions": list(self.open_questions),
            "dropped_facts": self.dropped_facts,
        }

    @property
    def is_empty(self) -> bool:
        return not self.key_facts

    def facts_by_source(self) -> dict[str, list[Fact]]:
        grouped: dict[str, list[Fact]] = {}
        for f in self.key_facts:
            grouped.setdefault(f.source_id, []).append(f)
        return grouped


@dataclass(frozen=True)
class ChapterDraft:
    chapter: int
    markdown: str
    citation_map: Mapping[str, int | None] = field(default_factory=dict)
    #: Writer 自己报告"素材不足、按资料未涉及处理"的要点
    gaps: tuple[str, ...] = ()
    #: 本章实际引用的 fact（正文角标命中的那些）。
    #: 带上它是为了让 Reviewer 只看「断言 + 原文」这一对输入，
    #: 而不需要读 ResearchPacket —— 否则 review 节点就得依赖 research，
    #: 多一条 DAG 边，也让 Reviewer 有机会被上游措辞带偏。
    used_facts: tuple[Fact, ...] = ()
    #: Writer 是否已声明"本章素材不足"（正文写了「资料未涉及」）。
    #: Reviewer 看到这个标记就不该再打回 research：素材本来就是空的，
    #: 再检一次也不会有，只会白白烧钱（文档 §5 那个典型死循环）。
    insufficient_evidence: bool = False

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any], *, chapter: int | None = None
    ) -> ChapterDraft:
        ch = int(raw.get("chapter", chapter or 0))
        markdown = str(raw.get("markdown", "")).strip()
        if not markdown:
            raise HandoffError("chapter.markdown 为空")
        cmap = raw.get("citation_map") or {}
        if not isinstance(cmap, Mapping):
            raise HandoffError("citation_map 必须是对象")
        used: list[Fact] = []
        for item in raw.get("used_facts") or []:
            if isinstance(item, Mapping):
                try:
                    used.append(Fact.from_dict(item))
                except HandoffError:
                    continue
        return cls(
            chapter=ch,
            markdown=markdown,
            citation_map={
                str(k): (int(v) if v not in (None, "") else None) for k, v in cmap.items()
            },
            gaps=tuple(str(g).strip() for g in (raw.get("gaps") or []) if str(g).strip()),
            used_facts=tuple(used),
            insufficient_evidence=bool(raw.get("insufficient_evidence", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter": self.chapter,
            "markdown": self.markdown,
            "citation_map": dict(self.citation_map),
            "gaps": list(self.gaps),
            "used_facts": [f.to_dict() for f in self.used_facts],
            "insufficient_evidence": self.insufficient_evidence,
        }

    def cited_sources(self) -> set[str]:
        """从正文角标 `[^doc_07]` 里抽出被引用的 source_id。"""
        found: set[str] = set()
        cursor = 0
        while True:
            start = self.markdown.find("[^", cursor)
            if start < 0:
                break
            end = self.markdown.find("]", start)
            if end < 0:
                break
            token = self.markdown[start + 2 : end].strip()
            if token:
                found.add(token)
            cursor = end + 1
        return found


class DefectType(str, Enum):
    ASSERTION_MISMATCH = "assertion_mismatch"  # 正文断言与所引原文不一致 → 打回 write
    MISSING_EVIDENCE = "missing_evidence"  # 核心要点没有任何素材支撑 → 打回 research
    MISSING_CITATION = "missing_citation"  # 有数据但没有角标


class FixTarget(str, Enum):
    WRITE = "write"
    RESEARCH = "research"


@dataclass(frozen=True)
class Defect:
    type: str
    claim: str
    reason: str
    fix_target: str = "write"

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Defect:
        dtype = str(raw.get("type", DefectType.ASSERTION_MISMATCH.value))
        target = str(raw.get("fix_target", "")).strip()
        if dtype == DefectType.MISSING_EVIDENCE.value:
            # 素材缺失一律打回 research，不采信模型自己给的 fix_target：
            # 让 Writer 重写它写不出来的内容，是这条流水线最典型的死循环
            target = FixTarget.RESEARCH.value
        elif target not in {t.value for t in FixTarget}:
            target = FixTarget.WRITE.value
        return cls(
            type=dtype,
            claim=str(raw.get("claim", "")).strip(),
            reason=str(raw.get("reason", "")).strip(),
            fix_target=target,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "claim": self.claim,
            "reason": self.reason,
            "fix_target": self.fix_target,
        }


@dataclass(frozen=True)
class ReviewVerdict:
    chapter: int
    verdict: str  # pass / revise
    defects: tuple[Defect, ...] = ()
    #: 程序侧计算的一致性指标（不由模型自评）
    citation_precision: float | None = None

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    @property
    def fix_target(self) -> str:
        """一章里只要有一条缺陷指向 research，就先修检索——否则重写也是白写。"""
        if any(d.fix_target == FixTarget.RESEARCH.value for d in self.defects):
            return FixTarget.RESEARCH.value
        return FixTarget.WRITE.value

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any], *, chapter: int | None = None
    ) -> ReviewVerdict:
        ch = int(raw.get("chapter", chapter or 0))
        verdict = str(raw.get("verdict", "")).strip().lower()
        if verdict not in {"pass", "revise"}:
            raise HandoffError(f"verdict 取值非法: {verdict!r}（只接受 pass/revise）")
        defects = tuple(
            Defect.from_dict(d)
            for d in (raw.get("defects") or [])
            if isinstance(d, Mapping)
        )
        if verdict == "revise" and not defects:
            # 只说"不行"不说哪里不行，下游无法修 —— 视为模型输出不合格
            raise HandoffError("verdict=revise 但没有给出任何 defect")
        precision = raw.get("citation_precision")
        return cls(
            chapter=ch,
            verdict=verdict,
            defects=defects,
            citation_precision=float(precision) if precision is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter": self.chapter,
            "verdict": self.verdict,
            "defects": [d.to_dict() for d in self.defects],
            "citation_precision": self.citation_precision,
            "fix_target": self.fix_target,
        }


def defects_to_hint(defects: Iterable[Defect]) -> str:
    """把缺陷整理成给下一轮 Agent 看的修复清单（不传完整对话历史）。"""
    lines = []
    for i, d in enumerate(defects, 1):
        lines.append(f"{i}. [{d.type}] 断言：{d.claim}\n   问题：{d.reason}")
    return "\n".join(lines)
