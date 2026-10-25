"""脚本化 LLM 替身：按 tag 返回预置文本。

用途有两个，都**不涉及网络**：
1. 单测 Agent 的 prompt 组装与结构化输出解析；
2. 离线演示（没有 API Key 时也能跑完整流水线）。

它不模拟"模型能力"，只模拟"模型返回了什么文本"——
所以不能用它证明生成质量，只能证明**解析、校验、状态机**是对的。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from ..contracts.llm import LLMRequest, LLMResponse, LLMUsage

Handler = str | Callable[[LLMRequest], str]


class ScriptedLLM:
    def __init__(
        self,
        responses: Mapping[str, Handler],
        *,
        default: Handler | None = None,
        model: str = "scripted-llm",
    ) -> None:
        self._responses = dict(responses)
        self._default = default
        self._model = model
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        handler = self._responses.get(req.tag, self._default)
        if handler is None:
            raise KeyError(f"ScriptedLLM 没有为 tag={req.tag!r} 准备响应")
        text = handler(req) if callable(handler) else handler
        prompt_chars = sum(len(m.content) for m in req.messages)
        return LLMResponse(
            text=text,
            model=self._model,
            usage=LLMUsage(
                prompt_tokens=prompt_chars // 2,  # 粗略折算，只用于验证计量链路
                completion_tokens=len(text) // 2,
            ),
            latency_ms=1,
        )


def json_handler(payload: Any) -> Handler:
    """把 dict 包装成"返回其 JSON 文本"的 handler。"""
    text = json.dumps(payload, ensure_ascii=False)
    return lambda _req: text
