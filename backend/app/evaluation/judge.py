"""LLM judge：只判两件程序判不了的事。

1. must_cover 里的要点，正文有没有**主张**过（不是关键词匹配）；
2. forbidden 里的错误说法，正文有没有**主张**过（否定语境不算）。

为什么不能关键词匹配：错误说法可能出现在否定句里——
「有观点认为 CR5 超过 80%，但实际为 38%」，关键词匹配会把正确表述判成错误。
这条判断只有语义模型能做，所以必须校准，而不是直接信任。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..contracts.eval import CoverageResult, TopicCase
from ..contracts.llm import LLMClient, LLMMessage, LLMRequest

_SYSTEM = """[task:judge] 你是严格的事实核查员，只做两项判定，不要评价文笔。

判定 1（覆盖率）：对每个「要求要点」，判断正文是否**明确主张**了这个说法。
- 只要求语义等价，不要求字面一致；
- 正文只是模糊提及、或只说"可能"、"据报道"而没有给出该结论，判 false；
- 数字必须对得上，8.4GWh 不能算作 8GWh。

判定 2（错误说法）：对每个「错误说法」，判断正文是否**主张**了它。
- 若正文只是在否定它、或引用他人观点后反驳，判 false；
- 若正文把该错误说法当作事实陈述，判 true。

只输出 JSON：
{"coverage":[{"item":"要点的前20字","covered":true,"evidence":"正文中的原句"}],
 "forbidden":[{"item":"错误说法前20字","asserted":false,"evidence":"正文中的原句或空"}]}"""

_MAX_TEXT_CHARS = 12000


@dataclass
class JudgeResult:
    coverage: list[CoverageResult]
    forbidden_hits: list[str]
    raw_cost_tokens: int = 0


class Judge:
    def __init__(self, llm: LLMClient, *, temperature: float = 0.0) -> None:
        self._llm = llm
        self._temperature = temperature

    async def assess(self, case: TopicCase, text: str) -> JudgeResult:
        if not case.must_cover and not case.forbidden:
            return JudgeResult(coverage=[], forbidden_hits=[])

        items = "\n".join(f"{i}. {c}" for i, c in enumerate(case.must_cover, 1))
        forb = "\n".join(f"{i}. {f}" for i, f in enumerate(case.forbidden, 1)) or "（无）"
        body = text[:_MAX_TEXT_CHARS]

        user = (
            f"要求要点（共 {len(case.must_cover)} 条）：\n{items}\n\n"
            f"错误说法（共 {len(case.forbidden)} 条）：\n{forb}\n\n"
            f"待判定正文：\n{body}"
        )
        # 解析失败时重试一次，并把 max_tokens 翻倍。
        # 推理模型会把预算先花在思维链上，剩下的 JSON 被截断——网关不会重试这种情况
        # （HTTP 200、内容非空、只是 JSON 不完整），只能在这里兜。
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
                temperature=self._temperature,
                # 判官可能被配成推理模型（如 deepseek-v4-pro），思维链会吃掉大量 completion token。
                # 给 1500 时出现过"推理写满、正文为空"，judge 直接解析失败——那会被误记成评测错误。
                max_tokens=budget,
                json_mode=True,
                timeout_s=180.0,
                tag="judge",
            )
            resp = await self._llm.complete(req)
            try:
                data = _parse(resp.text)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                budget *= 2
        if data is None:
            raise RuntimeError(f"判官返回无法解析（已重试）: {last_exc}") from last_exc

        covered_map = {
            _key(str(x.get("item", ""))): bool(x.get("covered"))
            for x in (data.get("coverage") or [])
            if isinstance(x, dict)
        }
        evidence_map = {
            _key(str(x.get("item", ""))): str(x.get("evidence", ""))
            for x in (data.get("coverage") or [])
            if isinstance(x, dict)
        }
        coverage = [
            CoverageResult(
                item=item,
                covered=covered_map.get(_key(item), False),
                evidence=evidence_map.get(_key(item), ""),
            )
            for item in case.must_cover
        ]

        hits: list[str] = []
        asserted = {
            _key(str(x.get("item", ""))): bool(x.get("asserted"))
            for x in (data.get("forbidden") or [])
            if isinstance(x, dict)
        }
        for bad in case.forbidden:
            if asserted.get(_key(bad)):
                hits.append(bad)

        return JudgeResult(
            coverage=coverage,
            forbidden_hits=hits,
            raw_cost_tokens=resp.usage.total_tokens,
        )


def _key(text: str) -> str:
    """judge 可能改写 item 文本，用前缀匹配容错（模型回抄前 20 字最稳定）。"""
    return "".join(text.split())[:18]


def _parse(text: str) -> dict[str, Any]:
    from ..agents.base import parse_json_object

    return parse_json_object(text)


__all__ = ["Judge", "JudgeResult"]
