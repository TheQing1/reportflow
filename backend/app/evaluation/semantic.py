"""语义支持度审计：正文断言是否真的被所引原文支持。

为什么必须单独有这个指标：

- 「引用有效率」只校验角标指向的来源是否存在，**恒为 100%**，没有区分度；
- 「要点覆盖率」看的是有没有写到，不看写得对不对；
- 而 Reviewer 的全部工作（判断言 vs 原文）恰好落在这个空档里——
  用覆盖率衡量它，必然得出"Reviewer 没用"的错误结论。

判定分三档，而不是二值：

| verdict | 含义 | 计分 |
|---|---|---|
| `supported` | 原文明确支持该断言（数值、结论一致） | 1.0 |
| `weak` | 部分支持，但口径/程度有出入（原文是区间、正文写成确定值） | 0.5 |
| `unsupported` | 原文不支持，或与原文矛盾 | 0 |

`unsupported` 单独再统计一次（矛盾数），因为它和"没写全"不是一类问题：
前者是错误信息，后者是信息缺失。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..agents.base import parse_json_object
from ..contracts.llm import LLMClient, LLMMessage, LLMRequest

#: 「一句话 + 它的角标」。
#: 上限从 120 放到 260：单 Agent 直出的段落句子常超过 120 字，
#: 一旦超限整条被丢掉，A 组的语义样本直接变成 0 条、支持率记 0%——
#: 那不是"A 组更差"，是**指标把长句判成了不存在**。
#: 超过 _MAX_SENTENCE 的句子截尾保留（贴着角标的那半句才是断言所在）。
_ASSERTION = re.compile(r"([^。！？\n\[\]]{6,260}?)\[\^([^\]\s]+)\]")
_MAX_SENTENCE = 120

#: 推断/因果词。这些词后面往往是模型自己补的结论，是"含义被放大"的高发区。
_INFERENCE = re.compile(
    r"意味着|因此|因而|表明|说明|可见|反映|体现|带动|推动|从而|进而|"
    r"由于|得益于|势必|将|有望|成为|决定|依赖于|导致"
)

#: 带量纲的数字。区间写成确定值、预测写成已实现，都藏在这里。
_UNIT_NUMBER = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|GWh|MWh|kWh|GW|MW|亿元|亿|万元|万|元|倍|个百分点|家|台)"
)

#: 单条原文给 judge 的上限。整篇通常 300~600 字，这个上限只防极端情况。
_MAX_SOURCE_CHARS = 600

#: 命中数字前后各取多少字作为上下文窗口
_WINDOW = 500

#: 不带量纲的纯数字，作为 _UNIT_NUMBER 的补充
_PLAIN_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _relevant_window(sentence: str, source: str) -> str:
    """从原文里切出**与这条断言相关**的一段，而不是开头的固定长度。

    一开始直接取 `source[:600]`，在长文档上会系统性误判：
    实测一篇 16947 字的发布会实录，断言里的数字出现在第 3761~3881 字，
    前 600 字只有开场白，judge 必然判「原文未提及」——
    于是**模型明明引对了，体检却报编造**。这种误报比漏报更伤，
    它会让人以为整个引用体系不可信。

    做法：用断言里的数字在原文里定位，取命中位置前后的窗口。
    定位不到（比如断言没有数字）才退回开头。
    """
    if len(source) <= _MAX_SOURCE_CHARS * 2:
        return source
    flat_idx: dict[int, int] = {}
    # 去空白后的下标与原串下标不必一一对应，这里按「累积非空白计数」还原
    pos, counter = 0, 0
    mapping = []
    for ch in source:
        if not ch.isspace():
            mapping.append(pos)
            counter += 1
        pos += 1
    flat = re.sub(r"\s+", "", source)
    tokens = [m.group(0) for m in _UNIT_NUMBER.finditer(sentence)]
    tokens += [m.group(0) for m in _PLAIN_NUMBER.finditer(sentence)]
    for num in tokens[:8]:
        j = flat.find(re.sub(r"\s+", "", num))
        if j >= 0:
            start = max(0, j - _WINDOW)
            end = min(len(flat), j + _WINDOW)
            flat_idx[start] = end
    if not flat_idx:
        return source[: _MAX_SOURCE_CHARS * 2]
    start = min(flat_idx)
    end = max(flat_idx.values())
    # flat 的下标映射回原串
    lo = mapping[start] if start < len(mapping) else 0
    hi = mapping[min(end, len(mapping) - 1)]
    snippet = source[lo:hi]
    return snippet or source[: _MAX_SOURCE_CHARS * 2]

_SYSTEM = """[task:audit] 你是引用核查员。任务：判断正文里的每一句断言，是否被它所引用的原文支持。

判定口径（三选一）：
- supported：原文明确支持该断言，数值与结论都能对上；
- weak：原文部分支持，但存在口径或程度差异。例如原文写"约8.4GWh"，正文写"8.4GWh"可以算 supported；
  但原文写"55%以上"、正文写"55%"算 weak；原文只给区间、正文给确定值也算 weak；
- unsupported：原文不支持该断言，或与该断言矛盾（例如数字不同、因果反了、把预测写成已实现）。

注意：
1. 只依据给出的原文判断，不要用你自己的行业知识补全。
2. 编号必须与输入对应，每条都要给出 verdict。
3. reason **控制在 40 字以内**，只引用原文关键片段，不要展开论述。
   理由写太长会把输出撑到 token 上限而被截断，JSON 就此变成非法——
   那时整批判定全部作废，比分批多调几次糟糕得多。

只输出 JSON：
{"items":[{"idx":1,"verdict":"supported","reason":"原文明确写出该数字"}]}"""

#: 单次审计的断言上限。
#: 一次让判官判 30 条时，输出常逼近 max_tokens 被截断，JSON 变成非法，
#: 整批作废（实测过：body 被切一半，解析报 "Expecting ',' delimiter"）。
#: 分批后单次输出短、不易截断，且某一批失败只影响那一批。
AUDIT_BATCH = 10


@dataclass(frozen=True)
class Assertion:
    chapter: int
    sentence: str
    source_id: str
    #: 抽样时的风险分，仅用于排查"为什么抽到这句"
    risk: int = 0


@dataclass
class SupportItem:
    chapter: int
    sentence: str
    source_id: str
    verdict: str
    reason: str = ""


@dataclass
class SupportReport:
    total: int = 0
    supported: int = 0
    weak: int = 0
    unsupported: int = 0
    skipped: int = 0
    items: list[SupportItem] = field(default_factory=list)
    error: str | None = None

    @property
    def support_rate(self) -> float:
        if not self.total:
            return 0.0
        return (self.supported + 0.5 * self.weak) / self.total


def sample_assertions(
    chapter_texts: Sequence[str], *, per_chapter: int = 3
) -> list[Assertion]:
    """每章抽「最容易脱离原文」的 N 条断言，而不是前 N 条。

    第一版按顺序取前 2 条，结果是三档方案的语义支持率都在 96~97%，
    **完全没有区分度**——因为每章开头第一句通常就是最直白的引用，
    数字照抄、结论照抄，判它 supported 毫无信息量。

    真正会出问题的是另外两类句子：
    - 带推断词的（"意味着""因此""将成为"）——模型在这里自己补因果；
    - 带数字的（尤其是把区间写成确定值、把预测写成已实现）。

    所以按风险分排序取头部：推断词 +3，带单位数字 +2，长句 +1。
    这不是"更严格"，而是把采样对准错误高发区，让指标恢复区分度。
    代价是**与历史批次不可比**（v4 及之前的语义支持率换算法后不能直接对比）。
    """
    picked: list[Assertion] = []
    for idx, text in enumerate(chapter_texts, start=1):
        candidates: list[tuple[int, int, str, str]] = []
        seen: set[str] = set()
        for order, match in enumerate(_ASSERTION.finditer(text)):
            sentence = match.group(1).strip().lstrip("-*#> ").strip()
            source_id = match.group(2).strip()
            if len(sentence) > _MAX_SENTENCE:
                # 贴着角标的尾段才是这条断言真正在说的东西，保留它
                sentence = "…" + sentence[-_MAX_SENTENCE:]
            if not sentence or sentence in seen:
                continue
            seen.add(sentence)
            # 负号用于升序排序（风险高的排前面）；order 保证同分时保持原文顺序
            candidates.append((-risk_score(sentence), order, sentence, source_id))
        candidates.sort()
        for neg_score, _order, sentence, source_id in candidates[:per_chapter]:
            picked.append(
                Assertion(
                    chapter=idx,
                    sentence=sentence,
                    source_id=source_id,
                    risk=-neg_score,
                )
            )
    return picked


def risk_score(sentence: str) -> int:
    """这句话"脱离原文"的风险分。高分优先被抽检。"""
    score = 0
    if _INFERENCE.search(sentence):
        score += 3
    if _UNIT_NUMBER.search(sentence):
        score += 2
    if len(sentence) >= 40:
        score += 1
    return score


class SupportAuditor:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def audit(
        self,
        assertions: Sequence[Assertion],
        corpus_text: Callable[[str], str],
    ) -> SupportReport:
        """分批审计。

        一次性判 30 条时输出常逼近 token 上限被截断，JSON 变非法、整批作废
        （实测 body 被切一半，解析报 "Expecting ',' delimiter"）。
        分批后：单次输出短、不易截断；某一批失败也只损失那一批。
        """
        if not assertions:
            return SupportReport()

        merged = SupportReport()
        for start in range(0, len(assertions), AUDIT_BATCH):
            part = await self._audit_batch(
                list(assertions[start : start + AUDIT_BATCH]), corpus_text
            )
            if part.error and not merged.error:
                merged.error = part.error
            merged.items.extend(part.items)
            merged.total += part.total
            merged.supported += part.supported
            merged.weak += part.weak
            merged.unsupported += part.unsupported
            merged.skipped += part.skipped
        return merged

    async def _audit_batch(
        self,
        assertions: list[Assertion],
        corpus_text: Callable[[str], str],
    ) -> SupportReport:
        blocks = []
        for i, a in enumerate(assertions, 1):
            source = corpus_text(a.source_id) or "（知识库中找不到该来源）"
            blocks.append(
                f"[{i}] 断言：{a.sentence}\n"
                f"    所引来源：{a.source_id}\n"
                f"    原文：{_relevant_window(a.sentence, source)}"
            )
        user = "\n\n".join(blocks)

        # 与 Judge 同样的兜底：JSON 被思维链挤到截断时，翻倍重试一次。
        data: dict[str, Any] | None = None
        budget = 4000
        last_exc: Exception | None = None
        for _attempt in range(2):
            req = LLMRequest(
                messages=[
                    LLMMessage(role="system", content=_SYSTEM),
                    LLMMessage(role="user", content=user),
                ],
                model="",
                temperature=0.0,
                max_tokens=budget,  # 推理模型的思维链另算 token
                json_mode=True,
                timeout_s=180.0,
                tag="audit",
            )
            try:
                resp = await self._llm.complete(req)
                data = parse_json_object(resp.text)
                break
            except Exception as exc:  # noqa: BLE001 - 审计失败不能影响主指标
                last_exc = exc
                budget *= 2
        if data is None:
            return SupportReport(error=f"{type(last_exc).__name__}: {last_exc}")

        items: list[SupportItem] = []
        by_idx = {
            int(x["idx"]): (str(x.get("verdict", "")).lower(), str(x.get("reason", "")))
            for x in (data.get("items") or [])
            if isinstance(x, dict) and str(x.get("idx", "")).isdigit()
        }

        report = SupportReport()
        for i, a in enumerate(assertions, 1):
            entry = by_idx.get(i)
            if entry is None:
                # judge 漏项：计入 skipped，**不算作不支持**——否则模型少写一行就凭空拉低分数
                report.skipped += 1
                continue
            verdict, reason = entry
            if verdict not in {"supported", "weak", "unsupported"}:
                report.skipped += 1
                continue
            report.total += 1
            setattr(report, verdict, getattr(report, verdict) + 1)
            items.append(
                SupportItem(
                    chapter=a.chapter,
                    sentence=a.sentence,
                    source_id=a.source_id,
                    verdict=verdict,
                    reason=reason,
                )
            )
        report.items = items
        return report
