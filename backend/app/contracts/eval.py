"""评测契约。

指标定义必须写死在类型里，否则每次跑完都会临时想一个新口径，
最后"覆盖率 82%"说不清是按句子算还是按要点算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Arm(str, Enum):
    """三档对比。顺序即报告里的展示顺序。"""

    A_SINGLE = "A"  # 单 Agent 直出（同素材）
    B_MULTI = "B"  # 多 Agent，无 Reviewer
    C_MULTI_REVIEW = "C"  # 多 Agent + Reviewer


ARM_LABELS = {
    Arm.A_SINGLE: "A · 单 Agent 直出",
    Arm.B_MULTI: "B · 多 Agent",
    Arm.C_MULTI_REVIEW: "C · 多 Agent + Reviewer",
}


@dataclass(frozen=True)
class TopicCase:
    """一个标注主题。"""

    id: str
    topic: str
    sections: int = 5
    #: 必须覆盖的要点。写成可判定的陈述句，来自语料事实表
    must_cover: tuple[str, ...] = ()
    #: 必须出现的来源 doc_id
    must_cite: tuple[str, ...] = ()
    #: 常见错误说法，正文若"主张"了它就算事实错误
    forbidden: tuple[str, ...] = ()
    notes: str = ""


@dataclass
class CoverageResult:
    item: str
    covered: bool
    evidence: str = ""


@dataclass
class CaseMetrics:
    """一次生成的全部指标。程序能算的和 judge 算的分开记，便于排查。"""

    case_id: str
    arm: Arm
    task_id: str
    status: str
    # ---- judge 判定 ----
    coverage: float = 0.0
    covered: list[CoverageResult] = field(default_factory=list)
    forbidden_hits: list[str] = field(default_factory=list)
    # ---- 程序计算 ----
    citation_validity: float = 0.0
    total_citations: int = 0
    valid_citations: int = 0
    must_cite_hit: float = 0.0
    #: 来源复用率：被 ≥2 章引用的来源占比。**不是**读者感知的重复，见 claim_repeat
    repeat_rate: float = 0.0
    #: 论述重复率：跨章重复的论述片段占比。这才是「读起来像复读机」的度量
    claim_repeat: float = 0.0
    # ---- 语义支持度（judge 抽样判定） ----
    semantic_support: float = 0.0
    semantic_total: int = 0
    semantic_unsupported: int = 0
    semantic_weak: int = 0
    semantic_skipped: int = 0
    semantic_issues: list[dict] = field(default_factory=list)
    # ---- 成本与耗时 ----
    cost: float = 0.0
    llm_calls: int = 0
    elapsed_s: float = 0.0
    char_count: int = 0
    error: str | None = None
    #: 正文全文，落盘以便复查与指标重算（不参与报告展示）
    text: str = ""

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "arm": self.arm.value,
            "task_id": self.task_id,
            "status": self.status,
            "coverage": round(self.coverage, 3),
            "forbidden_hits": self.forbidden_hits,
            "citation_validity": round(self.citation_validity, 3),
            "total_citations": self.total_citations,
            "valid_citations": self.valid_citations,
            "must_cite_hit": round(self.must_cite_hit, 3),
            "repeat_rate": round(self.repeat_rate, 3),
            "claim_repeat": round(self.claim_repeat, 3),
            "semantic_support": round(self.semantic_support, 3),
            "semantic_total": self.semantic_total,
            "semantic_unsupported": self.semantic_unsupported,
            "semantic_weak": self.semantic_weak,
            "semantic_skipped": self.semantic_skipped,
            "semantic_issues": self.semantic_issues,
            "cost": round(self.cost, 5),
            "llm_calls": self.llm_calls,
            "elapsed_s": round(self.elapsed_s, 2),
            "char_count": self.char_count,
            "error": self.error,
            "text": self.text,
            "covered": [
                {"item": c.item, "covered": c.covered, "evidence": c.evidence}
                for c in self.covered
            ],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> CaseMetrics:
        """断点续跑用：从落盘的 JSON 还原。"""
        return cls(
            case_id=raw["case_id"],
            arm=Arm(raw["arm"]),
            task_id=raw.get("task_id", ""),
            status=raw.get("status", "DONE"),
            coverage=float(raw.get("coverage", 0.0)),
            covered=[
                CoverageResult(
                    item=c.get("item", ""),
                    covered=bool(c.get("covered")),
                    evidence=c.get("evidence", ""),
                )
                for c in raw.get("covered", [])
            ],
            forbidden_hits=list(raw.get("forbidden_hits", [])),
            citation_validity=float(raw.get("citation_validity", 0.0)),
            total_citations=int(raw.get("total_citations", 0)),
            valid_citations=int(raw.get("valid_citations", 0)),
            must_cite_hit=float(raw.get("must_cite_hit", 0.0)),
            repeat_rate=float(raw.get("repeat_rate", 0.0)),
            claim_repeat=float(raw.get("claim_repeat", 0.0)),
            semantic_support=float(raw.get("semantic_support", 0.0)),
            semantic_total=int(raw.get("semantic_total", 0)),
            semantic_unsupported=int(raw.get("semantic_unsupported", 0)),
            semantic_weak=int(raw.get("semantic_weak", 0)),
            semantic_skipped=int(raw.get("semantic_skipped", 0)),
            semantic_issues=list(raw.get("semantic_issues", [])),
            cost=float(raw.get("cost", 0.0)),
            llm_calls=int(raw.get("llm_calls", 0)),
            elapsed_s=float(raw.get("elapsed_s", 0.0)),
            char_count=int(raw.get("char_count", 0)),
            error=raw.get("error"),
            text=raw.get("text", ""),
        )


@dataclass
class ArmSummary:
    """一档方案在所有主题上的汇总。"""

    arm: Arm
    cases: int = 0
    coverage: float = 0.0
    citation_validity: float = 0.0
    must_cite_hit: float = 0.0
    repeat_rate: float = 0.0
    claim_repeat: float = 0.0
    semantic_support: float = 0.0
    #: 真正被审计到（抽样非空且 judge 有返回）的用例数。
    #: 为 0 时要显示"—"而不是 0%——"没测"和"全错"是两件事。
    semantic_audited: int = 0
    semantic_unsupported: int = 0
    forbidden_count: int = 0
    cost_total: float = 0.0
    cost_per_report: float = 0.0
    elapsed_avg_s: float = 0.0
    failures: int = 0
