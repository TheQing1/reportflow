"""装配根（composition root）。

全项目唯一允许同时 import 具体实现（persistence / agents / llm / rag）的地方。
阶段二新增 `profile="real"`：把 LLM 网关 + 本地语料检索接到四个真实 Agent 上，
调度层一行都不用改——这是阶段一把契约层单独抽出来的直接回报。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .agents import MockAgentConfig, build_mock_registry, build_real_registry
from .contracts.agent import AgentRegistry
from .contracts.llm import LLMClient, ModelPrice
from .contracts.retrieval import Retriever
from .core.config import Settings
from .core.logging import get_logger
from .llm import CachedTransport, CacheMode, LLMGateway, OpenAICompatibleTransport
from .llm.cache import count_files
from .orchestrator.runner import TaskRunner
from .persistence import (
    SpanQuery,
    SqlSpanRecorder,
    SqlTaskStore,
    build_engine,
    build_session_factory,
    create_schema,
)
from .rag import LocalCorpusRetriever

SUPPORTED_PROFILES = ("mock", "real")


@dataclass
class Runtime:
    settings: Settings
    store: SqlTaskStore
    registry: AgentRegistry
    runner: TaskRunner
    profile: str
    spans: SpanQuery | None = None
    gateway: LLMGateway | None = None
    retriever: Retriever | None = None
    #: live / record / replay / auto（mock profile 恒为 live，不涉及模型）
    llm_mode: str = "live"
    extras: dict = field(default_factory=dict)


def build_runtime(
    settings: Settings | None = None,
    *,
    profile: str = "mock",
    mock_config: MockAgentConfig | None = None,
    llm: LLMClient | None = None,
    retriever: Retriever | None = None,
    ensure_schema: bool = True,
    announce: bool = True,
) -> Runtime:
    settings = settings or Settings.load()
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(f"未知的 agent profile: {profile!r}（可选 {SUPPORTED_PROFILES}）")

    log = get_logger("assembler")
    engine = build_engine(settings.database_url, echo=settings.db_echo)
    if ensure_schema:
        create_schema(engine)
    session_factory = build_session_factory(engine)
    store = SqlTaskStore(session_factory)
    spans = SpanQuery(session_factory)

    gateway: LLMGateway | None = None
    used_retriever: Retriever | None = None
    cache: CachedTransport | None = None
    judge_llm: LLMGateway | None = None

    if profile == "mock":
        registry = build_mock_registry(mock_config)
    else:
        if llm is None:
            mode = CacheMode.parse(settings.llm_mode)
            key = (settings.llm_api_key or "").strip()
            placeholder = not key or key.startswith("sk-your") or "your-key" in key
            # replay 不需要 key，这正是这个模式存在的理由之一，别在这里报错吓人
            if placeholder and mode is not CacheMode.REPLAY:
                # 不在这里抛错：本地 Ollama / vLLM 合法地不需要 key。
                # 但要明确说清"你现在的 key 是占位符"，否则用户只会看到一串 401。
                log.error(
                    "LLM_API_KEY 仍是占位符（%r）：real profile 会返回 401。"
                    "请在 .env 填写真实 key，或改用 --profile mock 做离线验证，"
                    "或设 LLM_MODE=replay 使用已录制的缓存。",
                    key or "<empty>",
                )
            transport: object = OpenAICompatibleTransport(
                settings.llm_base_url, settings.llm_api_key
            )
            if mode is not CacheMode.LIVE:
                cache = CachedTransport(
                    transport,  # type: ignore[arg-type]
                    mode=mode,
                    cache_dir=settings.llm_cache_dir,
                )
                transport = cache
                if mode is CacheMode.REPLAY:
                    log.info(
                        "LLM 回放模式：不联网，只读 %s（已录 %d 条）",
                        settings.llm_cache_dir,
                        count_files(settings.llm_cache_dir),
                    )
            gateway = LLMGateway(
                transport,  # type: ignore[arg-type]
                primary_model=settings.llm_model_primary,
                fallback_model=settings.llm_model_fallback or None,
                max_retries=settings.llm_max_retries,
                backoff_s=settings.llm_backoff_s,
                prices={
                    m: ModelPrice(
                        settings.llm_price_input_per_m, settings.llm_price_output_per_m
                    )
                    for m in (settings.llm_model_primary, settings.llm_model_fallback)
                    if m
                },
                recorder=SqlSpanRecorder(session_factory),
            )
            llm = gateway
            judge_llm = _build_judge_gateway(
                settings,
                mode=mode,
                session_factory=session_factory,
                primary=settings.llm_model_primary,
            )
            if judge_llm is not None:
                log.info("判官模型已独立：%s（生成用 %s）", settings.llm_judge_model, settings.llm_model_primary)
        used_retriever = retriever or LocalCorpusRetriever.from_directory(
            settings.docs_dir
        )
        if used_retriever.corpus_size() == 0:
            log.warning(
                "知识库为空（%s）：Researcher 会返回空素材包，正文将全部是「资料未涉及」",
                settings.docs_dir,
            )
        registry = build_real_registry(llm, used_retriever)

    budget_fn = None
    if used_retriever is not None:
        from .rag.budget import allocate_sync

        def budget_fn(topic: str, sections: int) -> dict:
            return allocate_sync(topic=topic, sections=sections, retriever=used_retriever)

    runner = TaskRunner(
        store=store,
        registry=registry,
        settings=settings,
        profile=profile,
        budget_fn=budget_fn,
    )
    if announce:
        log.info(
            "runtime ready profile=%s llm_mode=%s db=%s agents={%s} concurrency=%d 语料=%s",
            profile,
            settings.llm_mode if profile == "real" else "-",
            settings.database_url,
            registry.describe(),
            settings.max_concurrency,
            f"{used_retriever.corpus_size()} 片段" if used_retriever else "-",
        )
    return Runtime(
        settings=settings,
        store=store,
        registry=registry,
        runner=runner,
        profile=profile,
        spans=spans,
        gateway=gateway,
        retriever=used_retriever,
        llm_mode=settings.llm_mode if profile == "real" else "live",
        extras={"cache": cache, "judge_llm": judge_llm},
    )


def _build_judge_gateway(
    settings: Settings,
    *,
    mode: CacheMode,
    session_factory: object,
    primary: str,
) -> LLMGateway | None:
    """判官模型独立于生成模型。

    存在的理由：评测里 judge 和被测系统用同一个模型，就是自己给自己判卷。
    C 组（带 Reviewer）的"通过"可能是模型认同自己写的措辞，而不是真的更好。

    `LLM_JUDGE_MODEL` 换成同厂商的另一个模型只是缓解；
    真正消除自我偏好要跨厂商，所以 base_url / api_key 也允许单独指定。
    """
    judge_model = (settings.llm_judge_model or "").strip()
    if not judge_model or judge_model == primary:
        return None

    extra_body: dict[str, object] = {}
    if settings.llm_judge_extra:
        try:
            import json

            parsed = json.loads(settings.llm_judge_extra)
            if isinstance(parsed, dict):
                extra_body = parsed
            else:
                get_logger("assembler").warning(
                    "LLM_JUDGE_EXTRA_JSON 不是对象，已忽略：%s", settings.llm_judge_extra
                )
        except ValueError as exc:
            get_logger("assembler").warning(
                "LLM_JUDGE_EXTRA_JSON 解析失败，已忽略：%s", exc
            )

    transport: object = OpenAICompatibleTransport(
        settings.llm_judge_base_url or settings.llm_base_url,
        settings.llm_judge_api_key or settings.llm_api_key,
        default_body=extra_body or None,
    )
    if mode is not CacheMode.LIVE:
        # 开发阶段判官结果也走缓存：同一份正文反复判，不该反复花钱
        transport = CachedTransport(
            transport,  # type: ignore[arg-type]
            mode=mode,
            cache_dir=settings.llm_cache_dir,
        )
    return LLMGateway(
        transport,  # type: ignore[arg-type]
        primary_model=judge_model,
        fallback_model=None,
        max_retries=settings.llm_max_retries,
        backoff_s=settings.llm_backoff_s,
        # 跨厂商判官必须单独填单价，否则拿生成模型的价目表算，成本表会错得离谱。
        # 没填就退回主模型单价，并在日志里说明这是估算。
        prices={
            judge_model: ModelPrice(
                settings.llm_judge_price_input_per_m
                or settings.llm_price_input_per_m,
                settings.llm_judge_price_output_per_m
                or settings.llm_price_output_per_m,
            )
        },
        recorder=SqlSpanRecorder(session_factory),  # type: ignore[arg-type]
    )
