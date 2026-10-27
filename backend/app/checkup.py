"""发布前体检：只标记问题，不重写正文。

为什么不把 Reviewer 留在写作循环里：

评测结论（`eval/results/v6`）显示，B 档（无 Reviewer）的语义支持率已经 99.7%——
编造由 Researcher 的"quote 逐字可查"挡住，假角标由 Writer 的"映射不到就剥离"挡住，
剩下的 0.3% 才是 Reviewer 能修的。为这 0.3% 在每章多跑一次模型，换来 +49% 成本
和更大的产出方差（C 的 CI ±8.7pp vs B ±6.7pp），不划算。

所以它变成一次性的"体检"：导出前问一遍"哪句话的引用对不上"，由人决定改不改。
它不再改变正文，也就不再引入方差。

体检比评测多做一件事：**告诉你这条数据其实出自哪篇文档**。
单 Agent 最典型的错误就是把 A 文档的数字挂到 B 文档的角标上，
只说"不支持"帮助有限，指出"应该挂 doc_04"才可操作。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .contracts.llm import LLMClient
from .contracts.retrieval import Retriever
from .evaluation.semantic import SupportAuditor, sample_assertions

#: 带量纲的数字。定位"这段话的数据到底出自哪篇"就靠它。
_UNIT_NUMBER = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|GWh|MWh|kWh|GW|MW|亿元|亿|万元|万|元|倍|个百分点)"
)


@dataclass
class Issue:
    chapter: int
    sentence: str
    cited: str
    verdict: str
    reason: str = ""
    #: 这段话里的数字实际出现在哪几篇文档中（按命中数字个数排序）
    likely_docs: list[str] = field(default_factory=list)

    @property
    def likely_hint(self) -> str:
        if not self.likely_docs:
            return ""
        return "、".join(self.likely_docs)


@dataclass
class CheckReport:
    total: int = 0
    supported: int = 0
    weak: int = 0
    unsupported: int = 0
    skipped: int = 0
    issues: list[Issue] = field(default_factory=list)
    error: str | None = None

    @property
    def support_rate(self) -> float:
        if not self.total:
            return 0.0
        return (self.supported + 0.5 * self.weak) / self.total

    @property
    def clean(self) -> bool:
        return not self.unsupported and not self.error


def _numbers(text: str) -> list[str]:
    """抽出带量纲的数字，统一去空白（"8.4 GWh" 与 "8.4GWh" 视为同一个）。"""
    return [re.sub(r"\s+", "", m.group(0)) for m in _UNIT_NUMBER.finditer(text)]


def _overlap(sentence: str, body: str, n: int = 2) -> int:
    """句子与文档的字符二元组重合数。

    只用来在"数字命中数相同"的文档之间排序。同一个数字（如 55%）常出现在多篇里，
    光按命中和 doc_id 排序会给出无关的两篇；加上词面重合，才能把
    "增速回落至55%" 和 "三省合计占55%" 区分开。
    """
    compact = re.sub(r"[\s，。、；：（）()\[\]%％]", "", sentence)
    if len(compact) < n:
        return 0
    grams = {compact[i : i + n] for i in range(len(compact) - n + 1)}
    return sum(1 for g in grams if g in body)


def locate_citation(sentence: str, *, doc_ids: Sequence[str], full_text) -> list[str]:
    """找出该断言里的数字实际出现在哪几篇文档。

    先按"命中的数字个数"排，再按词面重合度排——数字是最硬的指纹，
    词面重合负责在多个候选之间挑更可能的那篇。
    匹配不到就返回空，不猜。
    """
    numbers = _numbers(sentence)[:4]
    if not numbers:
        return []
    scored: list[tuple[int, int, str]] = []
    for doc_id in doc_ids:
        body = re.sub(r"\s+", "", full_text(doc_id) or "")
        if not body:
            continue
        hit = sum(1 for num in numbers if num in body)
        if hit:
            scored.append((hit, _overlap(sentence, body), doc_id))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [doc_id for _, _, doc_id in scored[:2]]


async def check_report(
    *,
    chapters: Sequence[str],
    retriever: Retriever,
    llm: LLMClient,
    per_chapter: int = 6,
) -> CheckReport:
    """逐条核查正文断言与所引原文是否一致。"""
    assertions = sample_assertions(chapters, per_chapter=per_chapter)
    if not assertions:
        return CheckReport(error="正文里没有找到「句子[^来源]」形式的引用，无可核查")

    auditor = SupportAuditor(llm)
    audited = await auditor.audit(assertions, retriever.full_text)

    report = CheckReport(
        total=audited.total,
        supported=audited.supported,
        weak=audited.weak,
        unsupported=audited.unsupported,
        skipped=audited.skipped,
        error=audited.error,
    )
    if audited.error:
        return report

    doc_ids = list(retriever.doc_ids())
    for item in audited.items:
        if item.verdict == "supported":
            continue
        report.issues.append(
            Issue(
                chapter=item.chapter,
                sentence=item.sentence,
                cited=item.source_id,
                verdict=item.verdict,
                reason=item.reason,
                # 排除已经写对的那个来源：提示"别的文档也含这些数字"没有意义，
                # 要提示的是"除了你写的这篇，还有谁"——那才是可操作的改法。
                likely_docs=locate_citation(
                    item.sentence,
                    doc_ids=[d for d in doc_ids if d != item.source_id],
                    full_text=retriever.full_text,
                ),
            )
        )
    report.issues.sort(key=lambda i: (i.verdict != "unsupported", i.chapter))
    return report


def render_check(report: CheckReport, *, title: str = "发布前体检") -> list[str]:
    """把体检结果排成人能读的几行。"""
    lines = [f"{title}：核查 {report.total} 条引用断言", ""]
    if report.error:
        lines.append(f"  未完成：{report.error}")
        return lines

    lines.append(
        f"  支持 {report.supported} · 部分支持 {report.weak} · "
        f"不支持 {report.unsupported} · 判官漏判 {report.skipped}"
    )
    if report.clean:
        lines += ["", "  结论：没有发现引用对不上的地方。"]
        return lines

    lines += ["", "  有问题的句子："]
    for issue in report.issues[:12]:
        mark = "✗" if issue.verdict == "unsupported" else "△"
        lines.append(f"  {mark} 第 {issue.chapter} 章｜角标 {issue.cited}")
        lines.append(f"      {issue.sentence[:90]}")
        if issue.reason:
            lines.append(f"      原因：{issue.reason[:110]}")
        if issue.likely_docs:
            lines.append(f"      其他含这些数字的文档：{issue.likely_hint}")
    if len(report.issues) > 12:
        lines.append(f"  … 另有 {len(report.issues) - 12} 条，见 JSON 输出")
    return lines


__all__ = ["CheckReport", "Issue", "check_report", "locate_citation", "render_check"]
