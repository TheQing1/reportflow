"""Agent 公共基类：调 LLM 拿 JSON + 解析兜底。

三个刻意的设计：

1. **模型名不由 Agent 决定**。Agent 传 `model=""`，由网关按分级策略填。
   这样"换模型/加降级"不需要改任何 Agent。
2. **解析失败重试一次，且把错误原因回灌给模型**。结构化输出失败在真实项目里
   高频（多一个逗号、包一层 ```json），直接失败会浪费掉已经花掉的 prefill。
3. **解析失败不允许"猜"**。正则兜底只做"从混合文本里抠出 JSON 对象"，
   不做字段修复——字段缺失必须暴露成 HandoffError，让上层看到问题。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..contracts.llm import (
    LLMBadResponse,
    LLMClient,
    LLMMessage,
    LLMRequest,
    LLMResponse,
)
from ..contracts.nodes import NodeType
from ..core.logging import get_logger

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_object(text: str) -> dict[str, Any]:
    """从模型输出里取出一个 JSON 对象。

    容忍：前后说明文字、```json 围栏、BOM。
    不容忍：数组顶层（要求对象，避免下游到处判断类型）。
    """
    raw = text.strip().lstrip("\ufeff")
    fenced = _FENCE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()

    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise LLMBadResponse(f"输出里找不到 JSON 对象: {text[:160]!r}")
        raw = raw[start : end + 1]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMBadResponse(f"JSON 解析失败（{exc.msg} @ {exc.pos}）") from exc

    if not isinstance(data, dict):
        raise LLMBadResponse(f"顶层必须是 JSON 对象，实际是 {type(data).__name__}")
    return data


class JsonAgent:
    """所有真实 Agent 的基类：负责"调一次、解析、失败再调一次"。"""

    node_type: NodeType

    def __init__(
        self,
        llm: LLMClient,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> None:
        self._llm = llm
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._log = get_logger(f"agent.{self.__class__.__name__.lower()}")

    # ---------------------------------------------------------------- 调用

    async def ask_json(
        self,
        ctx_tag: str,
        system: str,
        user: str,
        *,
        timeout_s: float = 60.0,
    ) -> tuple[dict[str, Any], LLMResponse]:
        messages = [
            LLMMessage(role="system", content=system),
            LLMMessage(role="user", content=user),
        ]
        last_error: Exception | None = None

        for attempt in (1, 2):
            req = LLMRequest(
                messages=messages,
                model="",  # 由网关注入，Agent 不关心具体模型
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                json_mode=True,
                timeout_s=timeout_s,
                tag=ctx_tag,
            )
            resp = await self._llm.complete(req)
            try:
                return parse_json_object(resp.text), resp
            except LLMBadResponse as exc:
                last_error = exc
                self._log.warning(
                    "[%s] 第 %d 次结构化输出解析失败: %s | 原文片段: %r",
                    ctx_tag,
                    attempt,
                    exc,
                    resp.text[:200],
                )
                if attempt == 1:
                    # 只把"错在哪"回灌，不重发整段上下文
                    messages = [
                        *messages,
                        LLMMessage(role="assistant", content=resp.text[:4000]),
                        LLMMessage(
                            role="user",
                            content=(
                                f"你上一次的输出无法解析为 JSON（{exc}）。"
                                "请只输出一个合法的 JSON 对象，不要任何解释文字或代码围栏。"
                            ),
                        ),
                    ]

        raise LLMBadResponse(f"两次尝试都未能得到合法 JSON: {last_error}") from last_error

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def timeout_for(node_timeout: float, *, reserve: float = 5.0) -> float:
        """LLM 请求超时要早于节点超时，否则调度器先杀掉节点，
        我们就拿不到网关的重试与降级机会。"""
        return max(5.0, node_timeout - reserve)
