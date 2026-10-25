"""评测：数据集、指标、judge、三档对比执行器。"""

from .dataset import DatasetError, load_case, load_dataset
from .judge import Judge, JudgeResult
from .report import (
    ReportArtifact,
    citation_validity,
    collect_from_task,
    collect_from_text,
    must_cite_hit,
)
from .runner import EvalRunner, render_markdown, summarize
from .semantic import Assertion, SupportAuditor, SupportReport, sample_assertions

__all__ = [
    "Assertion",
    "DatasetError",
    "EvalRunner",
    "Judge",
    "JudgeResult",
    "ReportArtifact",
    "SupportAuditor",
    "SupportReport",
    "citation_validity",
    "collect_from_task",
    "collect_from_text",
    "load_case",
    "load_dataset",
    "must_cite_hit",
    "render_markdown",
    "sample_assertions",
    "summarize",
]
