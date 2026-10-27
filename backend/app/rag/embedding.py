"""Embedding 客户端：OpenAI 兼容 /embeddings + 独立的录制回放缓存。

为什么另做一份缓存，而不是复用 app/llm/cache.py：

- 那份缓存的键是 `LLMRequest(messages)`，而 embedding 根本没有 messages，
  硬塞进去就得改它的核心逻辑——而缓存层是 T1 同事正在动的地方；
- 分层约束（tests/test_layering.py）也不允许 rag 反向依赖 llm 层；
- 键的语义必须**含模型名**：`bge-m3` 换 `bge-large` 时向量空间完全不同，
  沿用 LLM 缓存"不含模型名"的口径会静默回放另一个模型的向量，
  维度对不上才报错，对得上就成了无法察觉的错误检索。

所以这里是一份语义等价的实现：同样的 live/record/replay/auto 四态、
同样的"回放缺失即抛 no_retry 异常、绝不联网"、同样的两级目录与原子落盘，
只是键 = 模型名 + 输入文本，落盘在 `<llm_cache_dir>/embeddings/` 子目录下。

为什么不缓存整个 batch：批次大小是配置项（EMBEDDING_BATCH_SIZE），
改一次批次就会整批失效；按**单条文本**缓存，改批次、改切分顺序都仍能命中。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..core.config import DEFAULT_LLM_CACHE_DIR
from ..core.logging import get_logger
from ..core.timeutil import iso, utcnow

#: 缓存格式版本。记录结构变化时 +1，旧缓存自然全部失效
EMBED_CACHE_VERSION = 1
#: llm_cache_dir 下的子目录名：与 LLM 的 prompt 缓存分开，避免互相污染
EMBED_CACHE_SUBDIR = "embeddings"
#: 单次请求失败后的重试次数（不含首次）。embedding 是按量计费的，
#: 无脑重试 5 次不如让上层看到失败；2 次足够跨过偶发 429/5xx。
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_S = 0.5


class EmbeddingError(RuntimeError):
    """embedding 相关错误的根。"""


class EmbeddingTransient(EmbeddingError):
    """超时 / 连接失败 / 5xx：重试有意义。"""


class EmbeddingRateLimit(EmbeddingTransient):
    """429。也归入可重试，但要退避。"""


class EmbeddingPermanent(EmbeddingError):
    """4xx / 响应结构不对：重试改变不了结果。"""


class EmbeddingReplayMiss(EmbeddingError):
    """回放模式下缓存里没有这条文本。

    `no_retry` 与 llm/cache.ReplayMiss 保持同一个约定：缓存缺失是确定性缺失，
    重试只会白等退避时间。调度层用 getattr 检查，不需要 import 这个类型。
    """

    no_retry = True


class EmbeddingCacheMode(str, Enum):
    """与 settings.llm_mode 完全同名的四态。

    刻意重新定义而不是 import app.llm.cache.CacheMode：分层不允许，
    而且这里的读/写语义要能独立演进（比如将来给向量加"只失效某模型"）。
    """

    LIVE = "live"
    RECORD = "record"
    REPLAY = "replay"
    AUTO = "auto"

    @classmethod
    def parse(cls, raw: str | None) -> EmbeddingCacheMode:
        text = (raw or "").strip().lower()
        if not text:
            return cls.LIVE
        for mode in cls:
            if mode.value == text:
                return mode
        valid = " / ".join(m.value for m in cls)
        raise ValueError(f"未知的 LLM_MODE: {raw!r}（可选 {valid}）")

    @property
    def reads_cache(self) -> bool:
        return self in (EmbeddingCacheMode.REPLAY, EmbeddingCacheMode.AUTO)

    @property
    def writes_cache(self) -> bool:
        return self in (EmbeddingCacheMode.RECORD, EmbeddingCacheMode.AUTO)

    @property
    def allows_live(self) -> bool:
        return self in (
            EmbeddingCacheMode.LIVE,
            EmbeddingCacheMode.RECORD,
            EmbeddingCacheMode.AUTO,
        )


def embed_key(model: str, text: str) -> str:
    """键 = 模型名 + 输入文本。

    文本逐字参与哈希：改一个字的 chunk 就是一次新的 embedding，
    回放时绝不会把旧向量安到新文本上。
    """
    canonical = json.dumps(
        {"v": EMBED_CACHE_VERSION, "model": model, "text": text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EmbeddingTransport(Protocol):
    """最小传输端口。测试用假实现替换它即可完全不联网。"""

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]: ...


class HTTPEmbeddingClient:
    """OpenAI 兼容 `/embeddings`（硅基流动 / 智谱 / 自建 vLLM 都是这个形状）。

    只发 HTTP、不引官方 SDK：这里只需要一个端点，
    自己发请求才能把超时、错误分类、批量顺序这些细节握在手里。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout_s
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        # 自建 vLLM / Ollama 常常不校验 key，所以缺 key 只提示不阻断
        self._client = client or httpx.AsyncClient(follow_redirects=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        if not texts:
            return []
        payload = {"model": model, "input": list(texts)}
        try:
            resp = await self._client.post(
                f"{self._base}/embeddings",
                json=payload,
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise EmbeddingTransient(f"embedding 请求超时（{self._timeout}s）: {exc}") from exc
        except httpx.HTTPError as exc:
            raise EmbeddingTransient(f"embedding 连接失败: {exc}") from exc

        status = resp.status_code
        if status == 429:
            raise EmbeddingRateLimit(f"embedding 限流 429: {resp.text[:200]}")
        if 500 <= status < 600:
            raise EmbeddingTransient(f"embedding 服务端 {status}: {resp.text[:200]}")
        if status >= 400:
            raise EmbeddingPermanent(f"embedding HTTP {status}: {resp.text[:300]}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise EmbeddingPermanent(f"embedding 响应不是合法 JSON: {resp.text[:200]}") from exc

        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not rows:
            raise EmbeddingPermanent(f"embedding 响应缺少 data: {str(data)[:200]}")
        # 端点不保证按输入顺序返回，必须按 index 重排后再交给调用方
        ordered = sorted(rows, key=lambda row: int(row.get("index", 0) or 0))
        if len(ordered) != len(texts):
            raise EmbeddingPermanent(
                f"embedding 返回条数不符：要 {len(texts)} 条，得 {len(ordered)} 条"
            )
        vectors: list[list[float]] = []
        for row in ordered:
            vec = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(vec, list) or not vec:
                raise EmbeddingPermanent("embedding 条目缺少 embedding 数组")
            vectors.append([float(x) for x in vec])
        return vectors


class Embedder:
    """带缓存与批量的 embedding 门面。上层只需要 `embed_texts`。"""

    def __init__(
        self,
        transport: EmbeddingTransport | None,
        *,
        mode: EmbeddingCacheMode = EmbeddingCacheMode.LIVE,
        cache_dir: str | Path,
        model: str,
        batch_size: int = 32,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_s: float = DEFAULT_BACKOFF_S,
    ) -> None:
        self._transport = transport
        self.mode = mode
        self.model = model
        self.batch_size = max(1, int(batch_size))
        self.max_retries = max(0, int(max_retries))
        self.backoff_s = max(0.0, float(backoff_s))
        # 缓存落在 <cache_dir>/embeddings/，与 LLM 的 prompt 缓存井水不犯河水
        self.dir = Path(cache_dir) / EMBED_CACHE_SUBDIR
        self.hits = 0
        self.misses = 0
        self.recorded = 0
        self.calls = 0  # 真实请求次数（批量算一次），用于断言"回放没联网"
        self._log = get_logger("rag.embedding")
        if mode.writes_cache:
            self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 对外

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """按输入顺序返回向量。相同文本只请求一次（去重后回填）。"""
        items = list(texts)
        if not items:
            return []
        unique = list(dict.fromkeys(items))
        reads = self.mode.reads_cache
        writes = self.mode.writes_cache

        found: dict[str, list[float]] = {}
        missing: list[str] = []
        #: 只用于 record 模式的"已有缓存不覆盖"。与 found 分开记：
        #: 缓存文件存在但内容损坏时应判为未命中（auto 要重录一份好的），
        #: 但 record 仍然不覆盖它——覆盖会把"上次录的"悄悄换成"这次的结果"。
        on_disk: set[str] = set()
        first_missing: str | None = None
        for text in unique:
            key = embed_key(self.model, text)
            cached = self._read(key) if (reads or writes) else None
            if cached is not None:
                on_disk.add(text)
                if reads:
                    found[text] = cached
                    self.hits += 1
                    continue
            missing.append(text)
            if reads:
                self.misses += 1
                if first_missing is None:
                    first_missing = text

        if missing and not self.mode.allows_live:
            # 回放模式：缺一条就是缺，不联网、不猜
            raise EmbeddingReplayMiss(
                f"embedding 回放缓存缺失 {len(missing)} 条（模型 {self.model}，"
                f"示例 {_preview(first_missing or '')!r}，目录 {self.dir}）。"
                "补录：LLM_MODE=record + 配好 EMBEDDING_API_KEY 再跑一次同一任务。"
            )
        if missing:
            if self._transport is None:
                raise EmbeddingReplayMiss(
                    f"embedding transport 未配置且无缓存可回放（模型 {self.model}）"
                )
            embedded, keys = await self._embed_in_batches(missing)
            for text, vec, key in zip(missing, embedded, keys, strict=True):
                found[text] = vec
                # record 模式"已有缓存不覆盖"：想换向量只能改模型名或删缓存文件
                if writes and text not in on_disk:
                    self._write(key, text, vec)
                    self.recorded += 1

        return [found[text] for text in items]

    async def aclose(self) -> None:
        closer = getattr(self._transport, "aclose", None)
        if closer is not None:
            await closer()  # type: ignore[misc]

    def describe(self) -> str:
        if self.mode is EmbeddingCacheMode.LIVE:
            return f"live（不读写缓存｜模型 {self.model}）"
        return (
            f"{self.mode.value}（模型 {self.model}｜缓存 {self.dir}｜"
            f"本次 命中 {self.hits} / 新录 {self.recorded}）"
        )

    # ------------------------------------------------------------ 批量 + 重试

    async def _embed_in_batches(self, missing: list[str]) -> tuple[list[list[float]], list[str]]:
        vectors: list[list[float]] = []
        keys: list[str] = []
        for start in range(0, len(missing), self.batch_size):
            batch = missing[start : start + self.batch_size]
            out = await self._call_with_retry(batch)
            if len(out) != len(batch):
                raise EmbeddingPermanent(
                    f"transport 返回条数不符：要 {len(batch)} 条，得 {len(out)} 条"
                )
            vectors.extend(out)
            keys.extend(embed_key(self.model, text) for text in batch)
        return vectors, keys

    async def _call_with_retry(self, batch: list[str]) -> list[list[float]]:
        attempt = 0
        while True:
            self.calls += 1
            try:
                return await self._transport.embed(batch, model=self.model)  # type: ignore[union-attr]
            except EmbeddingTransient as exc:
                if attempt >= self.max_retries:
                    raise
                delay = self.backoff_s * (2**attempt)
                self._log.warning(
                    "embedding 请求失败（第 %d 次，%s 条），%.1fs 后重试: %s",
                    attempt + 1,
                    len(batch),
                    delay,
                    exc,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                attempt += 1

    # ------------------------------------------------------------ 缓存文件

    def _path(self, key: str) -> Path:
        # 两级目录：与 llm/cache 同款，避免上千条文件挤在同一层
        return self.dir / key[:2] / f"{key}.json"

    def _has_cache(self, key: str) -> bool:
        return self._path(key).is_file()

    def _read(self, key: str) -> list[float] | None:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 损坏的缓存按未命中处理：auto 会重录一份好的，而不是卡死在坏文件上
            self._log.warning("embedding 缓存损坏，按未命中处理 %s: %s", path.name, exc)
            return None
        if not isinstance(raw, dict) or raw.get("v") != EMBED_CACHE_VERSION:
            return None
        if raw.get("model") != self.model:
            return None
        vec = raw.get("vector")
        if not isinstance(vec, list) or not vec:
            return None
        try:
            return [float(x) for x in vec]
        except (TypeError, ValueError):
            return None

    def _write(self, key: str, text: str, vector: list[float]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "v": EMBED_CACHE_VERSION,
            "key": key,
            "model": self.model,
            "dim": len(vector),
            # 刻意不四舍五入：JSON 的 float repr 可以无损往返，
            # 而"回放出来的向量"必须与"录制那一刻的向量"逐位相同——
            # 差 1e-8 不会报错，只会让相似度排序偶尔翻转，
            # 于是同一份缓存跑两次的结果不再逐字一致，评测基线就不再是确定的。
            "vector": [float(x) for x in vector],
            "recorded_at": iso(utcnow()),
            # 只为人工排查（"这条向量是哪段文本的"），不参与哈希
            "preview": _preview(text),
        }
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)  # 原子落盘：中途被杀不会留下半个 JSON
        except OSError as exc:
            self._log.warning("写 embedding 缓存失败 %s: %s", path, exc)
            tmp.unlink(missing_ok=True)


def _preview(text: str) -> str:
    return " ".join(text.split())[:200]


def build_embedder(settings: Any) -> Embedder | None:
    """按配置造 Embedder；没配 key 就返回 None（由检索层优雅降级为 BM25）。

    这里**不抛异常**是刻意的：知识库功能不该因为没配 embedding 就起不来，
    而"静默降级"必须可观测——所以打一条 info，把降级原因写清楚。
    """
    key = (settings.embedding_api_key or "").strip()
    if not key:
        get_logger("rag.embedding").info(
            "EMBEDDING_API_KEY 为空：retrieval_mode=%s 将降级为 BM25（不联网、不建向量索引）",
            settings.retrieval_mode,
        )
        return None
    transport = HTTPEmbeddingClient(
        settings.embedding_base_url,
        key,
        timeout_s=settings.embedding_timeout_s,
    )
    cache_dir = settings.llm_cache_dir or DEFAULT_LLM_CACHE_DIR
    return Embedder(
        transport,
        mode=EmbeddingCacheMode.parse(settings.llm_mode),
        cache_dir=cache_dir,
        model=settings.embedding_model,
        batch_size=settings.embedding_batch_size,
    )


__all__ = [
    "DEFAULT_BACKOFF_S",
    "DEFAULT_MAX_RETRIES",
    "EMBED_CACHE_SUBDIR",
    "EMBED_CACHE_VERSION",
    "Embedder",
    "EmbeddingCacheMode",
    "EmbeddingError",
    "EmbeddingPermanent",
    "EmbeddingRateLimit",
    "EmbeddingReplayMiss",
    "EmbeddingTransient",
    "EmbeddingTransport",
    "HTTPEmbeddingClient",
    "build_embedder",
    "embed_key",
]
