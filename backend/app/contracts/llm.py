"""LLM 调用契约。

网关、Agent、测试替身都只认这里的类型，不认识 httpx 或任何厂商 SDK。
换厂商（OpenAI 兼容 / 自建 vLLM / 别的协议）只需换 transport 实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


class LLMError(RuntimeError):
    """LLM 调用的基类异常。"""


class LLMTransient(LLMError):
    """可重试：超时、连接失败、5xx。"""


class LLMRateLimit(LLMTransient):
    """429 / 配额限流。退避后重试，或直接降级。"""


class LLMPermanent(LLMError):
    """不可重试：鉴权失败、参数错误、模型不存在。重试只会浪费钱。"""


class LLMBadResponse(LLMError):
    """返回了 200，但内容不可用（空 choices、非法 JSON）。"""


class LLMUnavailable(LLMError):
    """所有模型、所有重试都失败了。上层据此判定节点失败。"""


@dataclass(frozen=True)
class LLMMessage:
    role: str  # system / user / assistant
    content: str


@dataclass(frozen=True)
class LLMRequest:
    messages: Sequence[LLMMessage]
    model: str
    temperature: float = 0.2
    max_tokens: int | None = None
    #: 要求模型返回 JSON 对象（OpenAI 的 response_format）
    json_mode: bool = False
    timeout_s: float = 60.0
    #: 仅用于日志与计费归因，不参与请求
    tag: str = ""

    def with_model(self, model: str) -> "LLMRequest":
        return LLMRequest(
            messages=self.messages,
            model=model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            json_mode=self.json_mode,
            timeout_s=self.timeout_s,
            tag=self.tag,
        )


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: int = 0
    #: 是否由降级模型产出。用于日志与成本分析，不影响调用方
    degraded: bool = False
    #: 是否来自本地回放缓存。命中缓存不产生任何费用，span 成本记 0
    cached: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMTransport(Protocol):
    """一层薄传输：只负责"把请求发出去、把文本和 usage 拿回来"。"""

    async def send(self, req: LLMRequest) -> LLMResponse: ...


@runtime_checkable
class LLMClient(Protocol):
    """Agent 看到的接口。网关在 transport 之上加了分级、重试、计量。"""

    async def complete(self, req: LLMRequest) -> LLMResponse: ...


@dataclass(frozen=True)
class ModelPrice:
    """每百万 token 的价格（元）。用于成本归因，不做账务。"""

    input_per_m: float
    output_per_m: float

    def cost(self, usage: LLMUsage) -> float:
        return (
            usage.prompt_tokens / 1_000_000 * self.input_per_m
            + usage.completion_tokens / 1_000_000 * self.output_per_m
        )
