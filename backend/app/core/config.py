"""配置。优先级：进程环境变量 > .env 文件 > 代码默认值。

刻意不引入 pydantic-settings / python-dotenv：
阶段一只有 6 个配置项，多一个依赖就多一份版本冲突风险。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

#: 仓库根目录（backend/app/core/config.py -> reportflow/）
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "reportflow.db"
DEFAULT_LLM_CACHE_DIR = PROJECT_ROOT / "data" / "llm_cache"


def _read_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，忽略注释与空行，不处理引号转义。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value and value[0] in "\"'" and value[-1:] == value[0]:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _find_env_file(start: Path | None = None) -> Path | None:
    """从 cwd 向上找 .env，最多 4 层。这样在 backend/ 下跑也能读到仓库根的 .env。"""
    cur = (start or Path.cwd()).resolve()
    for _ in range(5):
        candidate = cur / ".env"
        if candidate.is_file():
            return candidate
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


@dataclass(frozen=True)
class Settings:
    database_url: str
    max_concurrency: int = 3
    node_timeout_s: float = 120.0
    max_retries: int = 1
    retry_backoff_s: float = 1.0
    poll_interval_s: float = 0.05
    log_level: str = "INFO"
    db_echo: bool = False

    # ---- 编排：质量闭环 ----
    max_review_rounds: int = 2
    #: Reviewer 是否进入写作循环。默认关：评测显示它在循环里没有收益却贵 49%，
    #: 改为导出时用 `cli export --check` 做一次性体检。
    review_in_pipeline: bool = False
    #: 给每章分配不重叠的文档预算（见 rag/budget.py）。用于压章节重复率。
    doc_budget: bool = True

    # ---- LLM（阶段二）----
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model_primary: str = "deepseek-chat"
    llm_model_fallback: str = ""
    llm_max_retries: int = 2
    llm_backoff_s: float = 1.0
    llm_timeout_s: float = 60.0
    #: 示例单价（元/百万 token）。不用真实账单前，成本只是量级参考
    llm_price_input_per_m: float = 1.0
    llm_price_output_per_m: float = 3.0
    #: live / record / replay / auto，见 app/llm/cache.py
    llm_mode: str = "live"
    #: 录制回放的缓存目录
    llm_cache_dir: str = ""
    #: 独立判官模型。留空 = 与生成同模型（会有自我偏好，评测不建议）
    llm_judge_model: str = ""
    #: 判官可独立指向另一家厂商（跨厂商评审才真正消除自我偏好）
    llm_judge_base_url: str = ""
    llm_judge_api_key: str = ""
    #: 判官单价。留空则沿用生成模型的单价（跨厂商时必须单独填，否则成本表会错）
    llm_judge_price_input_per_m: float = 0.0
    llm_judge_price_output_per_m: float = 0.0
    #: 判官厂商私有参数（JSON 字符串，原样并入请求体）。
    #: 例如硅基流动的 GLM 需要 {"enable_thinking": false} 才能把单次判定从 70 秒压到个位数。
    llm_judge_extra: str = ""

    # ---- 知识库 ----
    docs_dir: str = ""

    # ---- 检索（混合检索）----
    #: bm25 | vector | hybrid。默认 bm25：保持历史评测基线可复现。
    retrieval_mode: str = "bm25"
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_api_key: str = ""
    embedding_model: str = "BAAI/bge-m3"
    embedding_timeout_s: float = 30.0
    #: 批次大小。大部分 OpenAI 兼容端点单请求上限 32~128 条，32 保守可用。
    embedding_batch_size: int = 32
    #: RRF 融合的平滑常数。k=60 是原论文与工业界的默认值。
    rrf_k: int = 60
    #: 向量索引落盘目录。放 data/ 下，已被 .gitignore 覆盖。
    vector_index_dir: str = ""

    # ---- 工具（Function Calling）----
    #: 是否让 Agent 具备工具调用能力。默认**关闭**：
    #: 开着会让每次 research 多出 1~3 轮模型调用（成本上升、结果不确定性变大）。
    #: 关掉时请求形状与引入 T3 之前逐字节一致，评测基线不受影响。
    enable_tools: bool = False
    #: 是否允许联网检索。默认**关闭**，且独立于 enable_tools：
    #: 联网内容随时间变化，一旦默认开启，40 主题评测就不再可复现。
    enable_web_search: bool = False
    #: 工具循环最大轮次（模型最多要求几轮工具调用）。3 轮够「查→补查→写」。
    tool_max_rounds: int = 3
    #: 联网检索每次返回的网页数
    web_search_top_k: int = 3

    @classmethod
    def load(cls, env_file: Path | None = None) -> Settings:
        file_env = _read_env_file(env_file or _find_env_file() or Path("/nonexistent"))
        merged = {**file_env, **os.environ}  # 环境变量优先

        def get(key: str, default: str) -> str:
            return merged.get(key, default) or default

        db_url = get("DATABASE_URL", "").strip()
        if not db_url:
            DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            db_url = f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"

        docs_dir = get("DOCS_DIR", "").strip() or str(PROJECT_ROOT / "data" / "docs")

        return cls(
            database_url=db_url,
            max_concurrency=max(1, int(get("MAX_CONCURRENCY", "3"))),
            node_timeout_s=float(get("NODE_TIMEOUT", "120")),
            max_retries=max(0, int(get("MAX_RETRIES", "1"))),
            retry_backoff_s=float(get("RETRY_BACKOFF", "1.0")),
            poll_interval_s=float(get("POLL_INTERVAL", "0.05")),
            log_level=get("LOG_LEVEL", "INFO").upper(),
            db_echo=get("DB_ECHO", "false").lower() in {"1", "true", "yes"},
            max_review_rounds=max(0, int(get("MAX_REVIEW_RETRIES", "2"))),
            review_in_pipeline=get("REVIEW_IN_PIPELINE", "false").lower() in {"1", "true", "yes"},
            # 默认关闭：40 主题实测重复率 −1.2pp（不显著），成本却 +20%
            doc_budget=get("DOC_BUDGET", "false").lower() in {"1", "true", "yes"},
            llm_base_url=get("LLM_BASE_URL", "https://api.deepseek.com/v1"),
            llm_api_key=get("LLM_API_KEY", ""),
            llm_model_primary=get("LLM_MODEL_PRIMARY", "deepseek-chat"),
            llm_model_fallback=get("LLM_MODEL_FALLBACK", ""),
            llm_max_retries=max(1, int(get("LLM_MAX_RETRIES", "2"))),
            llm_backoff_s=float(get("LLM_BACKOFF", "1.0")),
            llm_timeout_s=float(get("LLM_TIMEOUT", "60")),
            llm_price_input_per_m=float(get("LLM_PRICE_INPUT_PER_M", "1.0")),
            llm_price_output_per_m=float(get("LLM_PRICE_OUTPUT_PER_M", "3.0")),
            llm_mode=get("LLM_MODE", "live").strip().lower(),
            llm_cache_dir=get("LLM_CACHE_DIR", "").strip() or str(DEFAULT_LLM_CACHE_DIR),
            llm_judge_model=get("LLM_JUDGE_MODEL", "").strip(),
            llm_judge_base_url=get("LLM_JUDGE_BASE_URL", "").strip(),
            llm_judge_api_key=get("LLM_JUDGE_API_KEY", "").strip(),
            llm_judge_price_input_per_m=float(get("LLM_JUDGE_PRICE_INPUT_PER_M", "0") or 0),
            llm_judge_price_output_per_m=float(get("LLM_JUDGE_PRICE_OUTPUT_PER_M", "0") or 0),
            llm_judge_extra=get("LLM_JUDGE_EXTRA_JSON", "").strip(),
            docs_dir=docs_dir,
            retrieval_mode=get("RETRIEVAL_MODE", "bm25").strip().lower(),
            embedding_base_url=get("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1").strip(),
            embedding_api_key=get("EMBEDDING_API_KEY", "").strip(),
            embedding_model=get("EMBEDDING_MODEL", "BAAI/bge-m3").strip(),
            embedding_timeout_s=float(get("EMBEDDING_TIMEOUT", "30")),
            embedding_batch_size=max(1, int(get("EMBEDDING_BATCH_SIZE", "32"))),
            rrf_k=max(1, int(get("RRF_K", "60"))),
            vector_index_dir=get("VECTOR_INDEX_DIR", "").strip()
            or str(PROJECT_ROOT / "data" / "vector_index"),
            enable_tools=get("ENABLE_TOOLS", "false").lower() in {"1", "true", "yes"},
            enable_web_search=get("ENABLE_WEB_SEARCH", "false").lower() in {"1", "true", "yes"},
            tool_max_rounds=max(1, int(get("TOOL_MAX_ROUNDS", "3"))),
            web_search_top_k=max(1, min(5, int(get("WEB_SEARCH_TOP_K", "3")))),
        )

    def with_overrides(self, **kwargs: object) -> Settings:
        clean = {k: v for k, v in kwargs.items() if v is not None}
        return replace(self, **clean)  # type: ignore[arg-type]
