"""LLM 网关：模型分级、重试退避、降级、计量。

放在 transport 之上、Agent 之下，只做四件事，不含任何业务语义：

1. **重试**：同模型遇到超时/5xx/限流时指数退避重试。重试次数用完才换模型。
2. **降级**：主模型彻底不可用时切到备用模型（建议换厂商，避免同时不可用）。
3. **计量**：每次调用写一条 span（token / 延迟 / 成本 / 是否降级）。
4. **异常归一**：把各厂商五花八门的错误码收敛成 4 个异常类型，Agent 不必关心。

一个刻意的取舍：**鉴权/参数类错误（4xx）也换备用模型再试一次**。因为
"模型不存在"这类 404 换个模型确实能救回来；而真正的 401 会在备用模型上再失败一次，
代价是多一次无用的请求——比"主模型配错了就整条流水线挂掉"更可接受。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace

from ..contracts.llm import (
    LLMBadResponse,
    LLMError,
    LLMMessage,
    LLMPermanent,
    LLMRequest,
    LLMResponse,
    LLMTransient,
    LLMTransport,
    LLMUnavailable,
    LLMUsage,
    ModelPrice,
)
from ..contracts.tracing import NullRecorder, Span, SpanRecorder, current_span_context
from ..core.logging import get_logger
from ..core.timeutil import utcnow


class LLMGateway:
    def __init__(
        self,
        transport: LLMTransport,
        *,
        primary_model: str,
        fallback_model: str | None = None,
        max_retries: int = 2,
        backoff_s: float = 1.0,
        max_backoff_s: float = 8.0,
        prices: Mapping[str, ModelPrice] | None = None,
        recorder: SpanRecorder | None = None,
    ) -> None:
        self._transport = transport
        self._primary = primary_model
        self._fallback = fallback_model or None
        self._max_retries = max(1, max_retries)
        self._backoff_s = backoff_s
        self._max_backoff_s = max_backoff_s
        self._prices = dict(prices or {})
        self._recorder = recorder or NullRecorder()
        self._log = get_logger("llm.gateway")
        self.calls = 0
        self.degraded_calls = 0
        #: 命中本地回放缓存的次数（不计费、不计降级）
        self.cached_calls = 0

    @property
    def models(self) -> list[str]:
        return [self._primary] + ([self._fallback] if self._fallback else [])

    async def complete(self, req: LLMRequest) -> LLMResponse:
        models = self.models
        last_error: LLMError | None = None

        for idx, model in enumerate(models):
            degraded = idx > 0
            for attempt in range(1, self._max_retries + 1):
                try:
                    resp = await self._transport.send(req.with_model(model))
                except LLMPermanent as exc:
                    last_error = exc
                    self._record(req, model, attempt, None, exc, degraded)
                    self._log.error(
                        "[%s] LLM 永久错误 model=%s: %s（换下一个模型）", req.tag, model, exc
                    )
                    break  # 不可重试，直接换模型
                except LLMTransient as exc:
                    last_error = exc
                    self._record(req, model, attempt, None, exc, degraded)
                    if attempt < self._max_retries:
                        delay = min(
                            self._backoff_s * (2 ** (attempt - 1)), self._max_backoff_s
                        )
                        self._log.warning(
                            "[%s] LLM 可重试错误 model=%s attempt=%d/%d，%.1fs 后重试: %s",
                            req.tag,
                            model,
                            attempt,
                            self._max_retries,
                            delay,
                            exc,
                        )
                        await asyncio.sleep(delay)
                        continue
                    self._log.error(
                        "[%s] LLM 重试耗尽 model=%s: %s（降级到下一个模型）",
                        req.tag,
                        model,
                        exc,
                    )
                    break
                except LLMBadResponse as exc:
                    last_error = exc
                    self._record(req, model, attempt, None, exc, degraded)
                    if attempt < self._max_retries:
                        await asyncio.sleep(0)  # 内容问题等一等没意义，立即重试一次
                        continue
                    break

                if degraded:
                    self.degraded_calls += 1
                    self._log.warning(
                        "[%s] 已降级到备用模型 %s", req.tag, model
                    )
                self.calls += 1
                if resp.cached:
                    self.cached_calls += 1
                self._record(req, model, attempt, resp, None, degraded)
                return replace(resp, degraded=degraded)

        raise LLMUnavailable(
            f"所有模型均不可用（models={models}）: {last_error}"
        ) from last_error

    # ------------------------------------------------ 计量

    def _record(
        self,
        req: LLMRequest,
        model: str,
        attempt: int,
        resp: LLMResponse | None,
        error: LLMError | None,
        degraded: bool,
    ) -> None:
        ctx = current_span_context()
        usage = resp.usage if resp else LLMUsage()
        price = self._prices.get(model, ModelPrice(0.0, 0.0))
        # 回放命中没有花钱，就算账面上照实记 0。
        # 否则开发阶段的成本报表会显示"花了 ¥x"，而银行卡一分没动——那是假数据。
        cost = 0.0 if (resp and resp.cached) else price.cost(usage)
        self._recorder.record(
            Span(
                task_id=ctx.task_id if ctx else "",
                node_id=ctx.node_id if ctx else (req.tag or "unknown"),
                span_type="llm_call",
                model=model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                latency_ms=resp.latency_ms if resp else 0,
                cost=cost,
                attempt=ctx.attempt if ctx else attempt,
                degraded=degraded,
                error=str(error) if error else None,
                created_at=utcnow(),
            )
        )


def build_messages(system: str, user: str) -> list[LLMMessage]:
    return [LLMMessage(role="system", content=system), LLMMessage(role="user", content=user)]
