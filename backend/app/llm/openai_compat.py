"""OpenAI 兼容传输层（httpx）。

只做三件事：拼请求、发出去、把响应/错误翻译成本项目的异常。
刻意不用官方 SDK：这里需要的只是 /chat/completions 一个接口，
自己发 HTTP 反而能把超时、错误分类、usage 解析这些细节握在手里。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..contracts.llm import (
    LLMBadResponse,
    LLMPermanent,
    LLMRateLimit,
    LLMRequest,
    LLMResponse,
    LLMTransient,
    LLMUsage,
)


class OpenAICompatibleTransport:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        client: httpx.AsyncClient | None = None,
        extra_headers: dict[str, str] | None = None,
        default_body: dict[str, Any] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        #: 厂商私有参数（如 enable_thinking / reasoning_effort）。
        #: 放传输层而不是请求契约里：不同厂商字段不同，不该污染 Agent 看到的接口。
        self._default_body = dict(default_body or {})
        self._headers = {
            "Content-Type": "application/json",
            **(extra_headers or {}),
        }
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        # 自建 vLLM / Ollama 常常不校验 key，所以缺 key 只提示不阻断
        self._client = client or httpx.AsyncClient(follow_redirects=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, req: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": req.model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "temperature": req.temperature,
        }
        if req.max_tokens:
            payload["max_tokens"] = req.max_tokens
        if req.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self._default_body:
            payload.update(self._default_body)

        started = time.monotonic()
        try:
            resp = await self._client.post(
                f"{self._base}/chat/completions",
                json=payload,
                headers=self._headers,
                timeout=req.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise LLMTransient(f"请求超时（{req.timeout_s}s）: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMTransient(f"连接失败: {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        status = resp.status_code

        if status == 429:
            raise LLMRateLimit(f"限流 429: {resp.text[:200]}")
        if 500 <= status < 600:
            raise LLMTransient(f"服务端 {status}: {resp.text[:200]}")
        if status >= 400:
            raise LLMPermanent(f"HTTP {status}: {resp.text[:300]}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMBadResponse(f"响应不是合法 JSON: {resp.text[:200]}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise LLMBadResponse(f"choices 为空: {str(data)[:200]}")
        content = (choices[0].get("message") or {}).get("content")
        if not content:
            raise LLMBadResponse("message.content 为空")

        usage_raw = data.get("usage") or {}
        usage = LLMUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
        )
        return LLMResponse(
            text=content,
            model=data.get("model") or req.model,
            usage=usage,
            latency_ms=latency_ms,
            raw={"id": data.get("id")},
        )
