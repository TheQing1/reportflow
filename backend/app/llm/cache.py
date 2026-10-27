"""LLM 录制 / 回放。

开发阶段真正贵的不是钱，是「等」和「抖」：

- 改一次前端或状态机提示，就要等 25 秒真模型跑完；
- 每次产出都不一样，界面调试时分不清「是我改坏了」还是「模型这次就这么写」；
- 跑多了还要盯着账单，于是不敢频繁跑。

三层都解决的办法是把模型响应缓存成一个可提交的文件：第一次跑真实（录制），
之后全部命中缓存（回放）。成本归零、延迟归零、产出确定。
改 prompt 会自然失效——因为 prompt 内容参与哈希。

三种模式：

| 模式 | 行为 | 用在哪 |
|---|---|---|
| `live` | 直连，不读不写缓存 | 评测、交付验证 |
| `record` | 真实调用 + 落盘；已有缓存不覆盖 | 首次录制新的 prompt |
| `replay` | 只读缓存，缺失即报错，绝不联网 | 断网开发、CI |
| `auto` | 命中就回放，未命中就真实调用并记录 | **开发默认** |

关键约束：**缓存键不含模型名**，只含 prompt 内容 + 是否 JSON 模式。
这样开发时把 `deepseek-chat` 换成更便宜的模型仍能命中同一批缓存；
而一旦改动了 prompt 文案，键就变了，自动重新录制，不会拿着过期响应自欺。

评测侧另有一道闸：`EvalRunner` 拒绝在非 `live` 模式下运行（除非显式放开），
否则「重复 2 次」会命中同一份缓存，重复实验就变成了假的。
"""

from __future__ import annotations

import hashlib
import json
import os
from enum import Enum
from pathlib import Path
from typing import Any

from ..contracts.llm import (
    LLMError,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    LLMTransport,
    LLMUsage,
)
from ..core.logging import get_logger
from ..core.timeutil import iso, utcnow

#: 缓存格式版本。字段结构调整时 +1，旧缓存自然全部失效
CACHE_VERSION = 1


class CacheMode(str, Enum):
    LIVE = "live"
    RECORD = "record"
    REPLAY = "replay"
    AUTO = "auto"

    @classmethod
    def parse(cls, raw: str | None) -> CacheMode:
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
        return self in (CacheMode.REPLAY, CacheMode.AUTO)

    @property
    def writes_cache(self) -> bool:
        return self in (CacheMode.RECORD, CacheMode.AUTO)

    @property
    def allows_live(self) -> bool:
        return self in (CacheMode.LIVE, CacheMode.RECORD, CacheMode.AUTO)


class ReplayMiss(LLMError):
    """回放模式下缓存里没有这条 prompt。

    刻意不继承 LLMPermanent：那样网关会当成「模型坏了」去换备用模型再试一遍，
    每种模型都会 miss 一次，日志被无意义的重试刷满。
    直接抛到上层，让用户看到「缺哪条、怎么补」，一次就够。

    `no_retry` 是给调度器的标记：缓存缺失是确定性缺失，重试改变不了结果。
    调度层用 getattr 检查，不需要 import 这个异常类型。
    """

    no_retry = True


def prompt_key(req: LLMRequest) -> str:
    """按 prompt 内容算键。

    不含模型、温度、max_tokens：
    - 换模型不该让开发缓存全部失效；
    - 温度只影响采样，回放的就是「录制那一刻采到的那一次」，这正是我们要的确定性。

    **但必须包含 tools 与工具往返消息**。漏掉它们会产生一个极难排查的 bug：
    同一个 prompt 配不同工具集，或工具返回了不同结果，却命中同一条缓存，
    于是回放出「别的工具组合下」的响应——参数不同、结果相同，看起来完全正常。

    关键细节：只有在**真的用了工具**时才把工具信息拼进 canonical 串。
    这样引入 T3 之前录的那批缓存键仍然有效（否则整库缓存失效，
    对靠回放离线开发的人来说等于凭空多花一笔钱）。
    """
    payload: dict[str, object] = {
        "v": CACHE_VERSION,
        "messages": [_message_key(m) for m in req.messages],
        "json_mode": bool(req.json_mode),
    }
    if req.tools:
        # 排序后序列化：工具声明顺序不应影响语义，但会影响 hash
        payload["tools"] = sorted(
            [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": dict(t.parameters),
                }
                for t in req.tools
            ],
            key=lambda d: str(d["name"]),
        )
        payload["tool_choice"] = req.tool_choice or ""

    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _message_key(msg: LLMMessage) -> object:
    """单条消息进缓存键的形态。

    普通 system/user/assistant 保持与旧版完全一致的两元组，
    只有带工具痕迹的消息才升格成 dict，以保住历史缓存的可复用性。
    """
    if not msg.tool_calls and not msg.tool_call_id:
        return [msg.role, msg.content]
    return {
        "role": msg.role,
        "content": msg.content,
        "tool_calls": [
            {"id": c.id, "name": c.name, "arguments": c.arguments}
            for c in msg.tool_calls
        ],
        "tool_call_id": msg.tool_call_id,
        "name": msg.name,
    }


class CachedTransport:
    """在任意 transport 外面套一层缓存。模式为 `replay` 时可以不要内层。"""

    def __init__(
        self,
        inner: LLMTransport | None,
        *,
        mode: CacheMode,
        cache_dir: str | Path,
    ) -> None:
        self._inner = inner
        self.mode = mode
        self.dir = Path(cache_dir)
        self.hits = 0
        self.misses = 0
        self.recorded = 0
        self._log = get_logger("llm.cache")
        if mode.writes_cache:
            self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------ 对外

    async def send(self, req: LLMRequest) -> LLMResponse:
        key = prompt_key(req)
        path = self._path(key)
        # 一次读取，同时决定「能不能回放」和「写的时候要不要覆盖」。
        # 损坏/版本不符的文件会被 _read 判成 None，于是 auto 模式会自动重录一份好的，
        # 而不是留着一个永远读不出来的坏文件把流水线卡死。
        existing = self._read(path) if (self.mode.reads_cache or self.mode.writes_cache) else None

        if self.mode.reads_cache and existing is not None:
            self.hits += 1
            return LLMResponse(
                text=str(existing.get("text", "")),
                model=str(existing.get("model", req.model)),
                usage=_usage_of(existing),
                latency_ms=0,
                cached=True,
                # 工具调用也必须回放，否则回放模式下"模型要调工具"这一步消失，
                # Agent 会直接拿到空文本、走进完全不同的分支。
                tool_calls=_tool_calls_of(existing),
            )
        if self.mode.reads_cache:
            self.misses += 1
            if self.mode is CacheMode.REPLAY:
                raise ReplayMiss(
                    f"回放缓存缺失 tag={req.tag or '-'} key={key[:12]}"
                    f"（文件 {path}）。补录：LLM_MODE=record 再跑一次同一任务。"
                )

        if self._inner is None:
            raise ReplayMiss(f"transport 未配置且无缓存可回放 tag={req.tag or '-'}")

        resp = await self._inner.send(req)

        if self.mode.writes_cache and existing is None:
            self._write(path, req, resp)
            self.recorded += 1
        return resp

    async def aclose(self) -> None:
        closer = getattr(self._inner, "aclose", None)
        if closer is not None:
            await closer()  # type: ignore[misc]

    def stats(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "dir": str(self.dir),
            "files": count_files(self.dir),
            "bytes": total_bytes(self.dir),
            "hits": self.hits,
            "misses": self.misses,
            "recorded": self.recorded,
        }

    def describe(self) -> str:
        if self.mode is CacheMode.LIVE:
            return "live（不读写缓存）"
        return (
            f"{self.mode.value}（缓存 {count_files(self.dir)} 条｜"
            f"本次 命中 {self.hits} / 新录 {self.recorded}）"
        )

    # ------------------------------------------------ 缓存文件

    def _path(self, key: str) -> Path:
        # 分两级目录，避免上万条缓存挤在同一个目录里拖慢文件系统
        return self.dir / key[:2] / f"{key}.json"

    def _read(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._log.warning("缓存文件损坏，按未命中处理 %s: %s", path.name, exc)
            return None
        if not isinstance(raw, dict) or raw.get("v") != CACHE_VERSION:
            return None
        return raw

    def _write(self, path: Path, req: LLMRequest, resp: LLMResponse) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "v": CACHE_VERSION,
            "key": path.stem,
            "tag": req.tag,
            "model": resp.model,
            "text": resp.text,
            "usage": {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
            },
            "recorded_at": iso(utcnow()),
            # 只为人工排查，不参与哈希
            "preview": _preview(req),
        }
        if resp.tool_calls:
            payload["tool_calls"] = [c.to_dict() for c in resp.tool_calls]
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, path)  # 原子落盘：中途被杀也不会留下半个 JSON
        except OSError as exc:
            self._log.warning("写缓存失败 %s: %s", path, exc)
            tmp.unlink(missing_ok=True)


def _usage_of(record: dict[str, Any]) -> LLMUsage:
    usage = record.get("usage") or {}
    if not isinstance(usage, dict):
        return LLMUsage()
    return LLMUsage(
        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
        completion_tokens=int(usage.get("completion_tokens", 0) or 0),
    )


def _tool_calls_of(record: dict[str, Any]) -> tuple[LLMToolCall, ...]:
    """从缓存记录里还原 tool_calls（旧缓存没有这个字段，返回空）。"""
    raw = record.get("tool_calls")
    if not isinstance(raw, list):
        return ()
    out: list[LLMToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        out.append(
            LLMToolCall(
                id=str(item.get("id") or ""),
                name=name,
                arguments=str(fn.get("arguments") or ""),
            )
        )
    return tuple(out)


def _preview(req: LLMRequest) -> str:
    for msg in reversed(req.messages):
        if msg.role == "user":
            return " ".join(msg.content.split())[:200]
    return ""


def count_files(cache_dir: str | Path) -> int:
    root = Path(cache_dir)
    if not root.is_dir():
        return 0
    return sum(1 for _ in root.glob("*/*.json"))


def total_bytes(cache_dir: str | Path) -> int:
    root = Path(cache_dir)
    if not root.is_dir():
        return 0
    return sum(p.stat().st_size for p in root.glob("*/*.json"))


def cache_summary(cache_dir: str | Path) -> dict[str, Any]:
    """给 `cli cache` 用：不建 transport 也能看缓存现状。"""
    root = Path(cache_dir)
    files = sorted(root.glob("*/*.json"), key=lambda p: p.stat().st_mtime)
    newest: str | None = None
    if files:
        from datetime import datetime

        newest = datetime.fromtimestamp(files[-1].stat().st_mtime).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    return {
        "dir": str(root),
        "files": len(files),
        "bytes": total_bytes(root),
        "newest": newest,
        "tags": _tag_histogram(files),
    }


def _tag_histogram(files: list[Path]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for path in files:
        try:
            tag = json.loads(path.read_text(encoding="utf-8")).get("tag") or "?"
        except (OSError, json.JSONDecodeError):
            tag = "?"
        hist[tag] = hist.get(tag, 0) + 1
    return dict(sorted(hist.items()))
