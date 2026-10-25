"""从任务产出里抽取"报告全文"与程序可算的指标。

程序指标优先的原则同样适用于评测：角标有效性、来源重复度这些不需要花钱问模型，
而且模型判这些还不如正则准。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..contracts.task import NodeState, TaskStore

_CITE = re.compile(r"\[\^([^\]]+)\]")
# 论述片段的长度。取 12 字：短了会把「储能行业」「同比增长」这类通用短语算成重复，
# 长了会漏掉换了几个字的复述。6~20 字区间测得 7~12%，结论对 n 不敏感。
_SHINGLE_N = 12
_SENT = re.compile(r"[^。！？\n]{12,}")


def _strip_cites(text: str) -> str:
    """去掉角标：论述重复看的是「写了什么」，不是「引了谁」。"""
    return _CITE.sub("", text)


def _shingles(text: str) -> set[str]:
    body = re.sub(r"\s+", "", _strip_cites(text))
    out: set[str] = set()
    for sentence in _SENT.findall(body):
        s = re.sub(r"\s+", "", sentence)
        for i in range(len(s) - _SHINGLE_N + 1):
            out.add(s[i : i + _SHINGLE_N])
    return out


@dataclass
class ReportArtifact:
    text: str
    chapter_texts: list[str] = field(default_factory=list)
    #: 每个来源被哪些章引用（章节号从 1 开始）
    source_chapters: dict[str, set[int]] = field(default_factory=dict)
    char_count: int = 0

    @property
    def citations_total(self) -> int:
        return len(_CITE.findall(self.text))

    @property
    def distinct_sources(self) -> int:
        return len(self.source_chapters)

    @property
    def repeat_rate(self) -> float:
        """被 2 章以上重复引用的**来源**占比。

        注意它衡量的是「共享引用」，不是「重复论述」——两者差 4 倍，
        不要拿这个数说「五章在说同一件事」。真实研报里一份统计年鉴被多章引用
        是完全正常的。要看读者感知的复读，用 `claim_repeat`。
        """
        if not self.source_chapters:
            return 0.0
        dup = sum(1 for chapters in self.source_chapters.values() if len(chapters) >= 2)
        return dup / len(self.source_chapters)

    @property
    def claim_repeat(self) -> float:
        """跨章重复的**论述片段**占比 —— 这才是读者读到的「复读机」。

        为什么另开一个指标：实测来源复用率 43% 时，论述重复只有 9%。
        前者把「两章引用同一份年鉴但讲不同数据」也算成重复，严重夸大问题。
        本项目前两轮去重尝试（prompt 层 / 检索层预算）都是以来源复用率为目标，
        结果两次都失败——其中一个原因是目标本身就没对准读者感知。

        算法：去掉角标（只看论述不看出处），按句切 n=12 字片段，
        统计出现在 ≥2 章里的片段占全部片段的比例。
        对 n 不敏感（6~20 字测得 7~12%），所以结论不是调参调出来的。
        """
        if len(self.chapter_texts) < 2:
            return 0.0
        sets = [_shingles(_strip_cites(ch)) for ch in self.chapter_texts]
        union: set[str] = set().union(*sets)
        if not union:
            return 0.0
        seen: dict[str, int] = {}
        for s in sets:
            for g in s:
                seen[g] = seen.get(g, 0) + 1
        dup = sum(1 for n in seen.values() if n >= 2)
        return dup / len(union)


def _register(text: str, chapter: int, artifact: ReportArtifact) -> None:
    for token in _CITE.findall(text):
        artifact.source_chapters.setdefault(token.strip(), set()).add(chapter)


def collect_from_task(store: TaskStore, task_id: str, sections: int) -> ReportArtifact:
    """把 write_chN 的正文按章节顺序拼成全文。"""
    nodes: dict[str, NodeState] = {n.node_id: n for n in store.list_nodes(task_id)}
    artifact = ReportArtifact(text="", chapter_texts=[])
    parts: list[str] = []
    for i in range(1, sections + 1):
        node = nodes.get(f"write_ch{i}")
        body = str((node.output or {}).get("markdown", "")).strip() if node else ""
        if not body:
            body = f"## 第 {i} 章\n\n（未生成）"
        parts.append(body)
        _register(body, i, artifact)
        artifact.chapter_texts.append(body)
    artifact.text = "\n\n".join(parts)
    artifact.char_count = len(re.sub(r"\s", "", artifact.text))
    return artifact


def collect_from_text(text: str) -> ReportArtifact:
    """A 组（单 Agent 直出）用：按 `## ` 切章，再从正文抽角标。"""
    artifact = ReportArtifact(text=text, chapter_texts=[])
    chunks = re.split(r"\n(?=##\s)", text.strip())
    for idx, chunk in enumerate(chunks, start=1):
        if not chunk.strip():
            continue
        artifact.chapter_texts.append(chunk.strip())
        _register(chunk, idx, artifact)
    artifact.char_count = len(re.sub(r"\s", "", text))
    return artifact


def citation_validity(artifact: ReportArtifact, valid_sources: set[str]) -> tuple[int, int]:
    """返回 (有效角标数, 角标总数)。角标指向不存在的来源 = 假引用。"""
    tokens = [t.strip() for t in _CITE.findall(artifact.text)]
    if not tokens:
        return 0, 0
    valid = sum(1 for t in tokens if t in valid_sources)
    return valid, len(tokens)


def must_cite_hit(artifact: ReportArtifact, must_cite: tuple[str, ...]) -> float:
    if not must_cite:
        return 1.0
    hit = sum(1 for d in must_cite if d in artifact.source_chapters)
    return hit / len(must_cite)
