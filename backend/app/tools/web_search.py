"""联网检索工具：给 Researcher 补上"知识库之外"的素材来源。

**默认关闭**，理由不是保守，而是正确性：联网内容随时间变化，
一旦默认开启，40 主题评测集的结果就不再可复现——同一份代码、
同一个 prompt，今天和下周跑出来不一样，那评测基线就失去意义了。

所以这里的设计原则是：
1. `ENABLE_WEB_SEARCH=false`（默认）时**完全不联网**：连 httpx 客户端都不建；
2. provider 可替换，不绑定某家付费 API；
3. 抓回来的内容必须能作为**可溯源片段**：保留 url + 标题 + 原文文本，
   上层要拿 quote 去 text 里做逐字匹配，所以绝不做摘要改写；
4. 任何失败都以 `ToolResult.ok=False` 返回给模型，不抛给编排层。

**关于默认 provider 的选择**（实测得出，不是拍脑袋）：
最初按惯例用 DuckDuckGo 的 HTML 端点，但本机实测 `html.duckduckgo.com` 连接超时
（该域名在部分网络环境不可达）。Bing 能连通，但返回的是 JS 渲染的空壳页，
`h2 > a` 抓到的标题与查询毫无关系。360 搜索（so.com）能连通且返回真实结果，
所以默认用它。这也说明为什么要保留可替换的 provider 接口——
搜索源的可达性随环境而变，写死一家就等于把工具的可移植性押在运气上。

刻意不引 playwright/selenium：那类依赖装起来重、CI 里还常失败，
而"抓一页 HTML 转文本"用正则足够。代价是抓不了 JS 渲染的页面——
对研报素材来说，能抓到静态正文的站点已经覆盖大部分公开数据源。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

from ..contracts.tools import (
    MAX_FETCH_BYTES,
    ToolResult,
    ToolSpec,
    ToolUnavailable,
    now_ms,
    strip_html,
    truncate,
)

#: 抓取正文时假装成普通浏览器：不少站点对默认 UA 直接返回 403
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
#: 单篇抓取的硬超时。必须显著小于工具超时，否则一个慢站点会吃光整个工具的预算
_FETCH_TIMEOUT_S = 8.0

#: 内置搜索源。key 是 provider 名，值是 (端点模板, 解析器名)。
#:
#: 端点里的 `{q}` 会被 URL 编码后的查询替换。
_SEARCH_PROVIDERS: dict[str, tuple[str, str]] = {
    # 默认：国内可达、返回真实静态结果
    "so360": ("https://www.so.com/s?q={q}", "so360"),
    # 备用：部分网络环境下可达。用的是它的 HTML 端点
    "duckduckgo": ("https://html.duckduckgo.com/html/?q={q}", "duckduckgo"),
    # 备用：可达但多为 JS 渲染，抓不到结果时会被上层当"没搜到"
    "bing": ("https://cn.bing.com/search?q={q}", "bing"),
}
DEFAULT_PROVIDER = "so360"


class WebSearchTool:
    """联网检索：搜 -> 取前 N 条 -> 抓正文 -> 截断后返回。

    `provider` 只依赖一个很窄的接口：给 query 返回 [(title, url)]。
    默认实现走 DuckDuckGo 的 HTML 端点，不需要 API key。
    """

    spec = ToolSpec(
        name="web_search",
        description=(
            "在互联网上检索给定查询的公开资料，返回若干条网页的标题、链接与正文摘录。"
            "当本地知识库没有相关材料、或需要最新数据时使用。"
            "返回的正文是原文摘录，引用时必须逐字照抄其中的连续文字。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词，中文或英文均可，尽量具体（含年份/指标名）",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回条数，1~5，默认 3",
                    "minimum": 1,
                    "maximum": 5,
                },
            },
            "required": ["query"],
        },
        timeout_s=25.0,
    )

    def __init__(
        self,
        *,
        enabled: bool = False,
        top_k: int = 3,
        max_fetch: int = 4,
        client: httpx.AsyncClient | None = None,
        provider: str = DEFAULT_PROVIDER,
        search_endpoint: str | None = None,
    ) -> None:
        self._enabled = enabled
        self._default_top_k = max(1, min(5, top_k))
        #: 最多抓几篇正文。搜索结果里常有 PDF / 视频页，抓了也没用，多抓只是浪费
        self._max_fetch = max(1, max_fetch)
        if provider not in _SEARCH_PROVIDERS and search_endpoint is None:
            raise ValueError(
                f"未知的搜索 provider: {provider!r}（可选 {' / '.join(_SEARCH_PROVIDERS)}）"
            )
        template, parser = _SEARCH_PROVIDERS.get(provider, ("{q}", "so360"))
        self._search_template = search_endpoint or template
        self._parser = parser
        self.provider = provider
        # 关键：关闭时不建客户端。构造 httpx.AsyncClient 本身不联网，
        # 但"没有客户端"是最强的保证——不可能有代码路径偷偷发请求。
        self._client = client if enabled else None
        self._owns_client = client is None and enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    # ---------------------------------------------------------------- 主流程

    async def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        started = now_ms()
        if not self._enabled:
            # 明确告诉模型"这个工具没开"，而不是静默返回空——
            # 静默空结果会让模型误判"网上也没有"，进而编造。
            return ToolResult.failure(
                "联网检索未启用（ENABLE_WEB_SEARCH=false），请只使用知识库中的材料",
                elapsed_ms=now_ms() - started,
            )

        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult.failure("query 不能为空", elapsed_ms=now_ms() - started)
        try:
            top_k = int(arguments.get("top_k") or self._default_top_k)
        except (TypeError, ValueError):
            top_k = self._default_top_k
        top_k = max(1, min(5, top_k))

        try:
            hits = await self._search(query, limit=top_k)
        except ToolUnavailable as exc:
            return ToolResult.failure(str(exc), elapsed_ms=now_ms() - started)
        except Exception as exc:  # noqa: BLE001 - 网络层任何异常都不该拖垮节点
            return ToolResult.failure(
                f"检索请求失败：{type(exc).__name__}: {exc}",
                elapsed_ms=now_ms() - started,
            )

        if not hits:
            return ToolResult(
                ok=True,
                content="未检索到结果。可以换用更具体的关键词，或改用本地知识库。",
                items=(),
                elapsed_ms=now_ms() - started,
                total_items=0,
            )

        # hits 已经是带着正文/摘要的完整条目（见 _search 的说明），
        # 不再二次抓取：少一次网络往返，也不会被反爬挡住。
        items = hits[: self._max_fetch]
        return ToolResult(
            ok=True,
            content=self._render(items),
            items=tuple(items),
            elapsed_ms=now_ms() - started,
            total_items=len(hits),
        )

    # ---------------------------------------------------------------- 搜索

    async def _search(self, query: str, *, limit: int) -> list[dict[str, Any]]:
        """搜一次，返回候选条目（含搜索源自带的摘要）。

        注意这里**不发第二次请求去抓原页**——360 的结果链接带反爬（单独请求 400），
        而结果页已经给了 data-mdurl + 摘要，够用且更稳。详见 _parse_so360_html。
        """
        assert self._client is not None  # enabled 时才可能走到这里
        url = self._search_template.replace("{q}", quote_plus(query))
        resp = await self._client.get(
            url,
            headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            timeout=_FETCH_TIMEOUT_S,
            follow_redirects=True,
        )
        if resp.status_code >= 400:
            raise ToolUnavailable(f"搜索服务返回 HTTP {resp.status_code}")

        if self._parser == "so360":
            raw = _parse_so360_html(resp.text, limit=limit)
            return [
                {
                    "doc_id": _doc_id_of(u),
                    "title": t or u,
                    "url": u,
                    "text": clip,
                    "truncated": False,
                }
                for t, u, clip in raw
            ]

        # 其它 provider 只有 (title, url)，退回到抓原页
        if self._parser == "bing":
            hits = _parse_bing_html(resp.text, limit=limit)
        else:
            hits = _parse_ddg_html(resp.text, limit=limit)
        return await self._fetch_all(hits)

    async def _fetch_all(self, hits: list[tuple[str, str]]) -> list[dict[str, Any]]:
        # 并发抓：串行抓 N 篇、每篇 8s 上限，最坏会超出工具超时
        results = await asyncio.gather(
            *(self._fetch_one(title, url) for title, url in hits),
            return_exceptions=True,
        )
        return [r for r in results if isinstance(r, dict)]

    async def _fetch_one(self, title: str, url: str) -> dict[str, Any] | None:
        assert self._client is not None
        try:
            resp = await self._client.get(
                url,
                headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                timeout=_FETCH_TIMEOUT_S,
                follow_redirects=True,
            )
            if resp.status_code >= 400:
                return None
            body = resp.text[:MAX_FETCH_BYTES]
        except Exception:  # noqa: BLE001 - 单篇失败不影响其它篇
            return None

        text = strip_html(body).strip()
        if len(text) < 80:
            # 太短的多半是登录墙 / 空壳页 / JS 容器，引用它没有意义
            return None
        clipped, truncated = truncate(text)
        return {
            "doc_id": _doc_id_of(url),
            "title": title or url,
            "url": url,
            "text": clipped,
            "truncated": truncated,
        }

    @staticmethod
    def _render(items: list[dict[str, Any]]) -> str:
        """回给模型的文本。

        **必须显式打印 doc_id**。这是踩过的坑：原先只写「[网页1]」，
        模型就自己编了个 `web_1` 当 source_id，结果全部被溯源校验丢弃
        （"source_id 不在检索结果中"）——网页查到了却一条也用不上。
        与 kb_search 的渲染格式保持一致，模型才知道该照抄哪个 id。
        """
        blocks: list[str] = []
        for i, it in enumerate(items, 1):
            flag = "（正文过长已截断）" if it.get("truncated") else ""
            blocks.append(
                f"[片段{i}] doc_id={it['doc_id']} 标题={it['title']}\n"
                f"来源：{it['url']}{flag}\n"
                f"原文：{it['text']}"
            )
        blocks.append(
            "引用要求：source_id 必须照抄上面的 doc_id（形如 web_xxx）；"
            "quote 必须从「原文」中逐字复制连续文字，不得改写、不得拼接。"
        )
        return "\n\n".join(blocks)


# -------------------------------------------------------------------- 解析


def _doc_id_of(url: str) -> str:
    """把 url 压成一个短而稳定的 doc_id（用于角标 `[^web_xxx]`）。

    用域名 + 路径尾段，而不是 hash：人看角标时能大致知道来源站点，
    排查"这条引用是哪来的"比一串 hex 有用得多。
    """
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "web").replace("www.", "")
        tail = [seg for seg in (parsed.path or "").split("/") if seg]
        name = tail[-1] if tail else ""
        name = name.rsplit(".", 1)[0] or "page"
    except Exception:  # noqa: BLE001
        host, name = "web", "page"
    # 允许点号：域名里的 `.` 是合法且有意义的信息（`nea.gov.cn` 比 `nea_gov_cn`
    # 更容易让人一眼看出出处）。dot 不会破坏 markdown 角标 `[^web_xxx]` 的语法
    # ——旧版 sanitize_filename 也保留了点号，属于既有约定。
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in f"{host}_{name}")
    # 角标里不该出现连续下划线或首尾分隔符，收个尾
    safe = safe.strip("_.") or "page"
    return f"web_{safe[:48]}"


def _parse_so360_html(html: str, *, limit: int) -> list[tuple[str, str, str]]:
    """从 360 搜索结果页抽 (标题, 真实URL, 摘要)。

    返回三元组而不是二元组，是因为**搜索页自带摘要**（`res-list-summary`）。
    实测发现两条硬约束，逼出了这个设计：

    1. 360 的结果链接是 `so.com/link?m=...` 跳转，**单独请求会返回 400**
       （需要搜索页的会话 cookie / Referer，属反爬机制）。所以
       "跟随重定向拿真实 URL"这条路走不通——实测所有跳转都拿不到 Location。
    2. 但结果页里每条都带 `data-mdurl`（真实 URL）与一段 80~100 字原文摘要。

    所以最终方案：**不抓原页，直接用搜索索引提供的摘要**。三个好处：
    - 不受反爬影响，稳定性远高于逐页抓取；
    - 快（1 次请求 vs N 次抓取），工具超时压力小；
    - 摘要本身就是从原文截取的连续文字，仍可满足"逐字可查"的溯源要求——
      模型必须从这段摘要里逐字复制，不能改写。

    代价也要说清：摘要只有百来字，信息密度低于整页。对"抽一句话做引用"够用，
    对"通读全文做综述"不够。这是有意识的取舍，不是偷懒。
    """
    out: list[tuple[str, str, str]] = []
    # 逐个 <li class="res-list"> 块解析，避免跨条目误匹配
    for block in re.finditer(r'(?s)<li[^>]+class="[^"]*res-list[^"]*"[^>]*>(.*?)</li>', html):
        seg = block.group(1)
        title_m = re.search(
            r'<h3[^>]*class="[^"]*res-title[^"]*"[^>]*>.*?<a([^>]*)>(.*?)</a>',
            seg,
            re.I | re.S,
        )
        if not title_m:
            continue
        attrs = title_m.group(1)
        md = re.search(r'data-mdurl="([^"]+)"', attrs)
        url = md.group(1) if md else ""
        if not url.startswith("http"):
            continue
        title = strip_html(title_m.group(2)).strip()
        if not title:
            continue
        sm = re.search(
            r'class="[^"]*(?:res-list-summary|res-desc)[^"]*"[^>]*>(.*?)</(?:span|p|div)>',
            seg,
            re.I | re.S,
        )
        summary = strip_html(sm.group(1)).strip() if sm else ""
        # 过短摘要没有引用价值（多半是"查看更多"之类的引导语）
        if len(summary) < 30:
            continue
        out.append((title, url, summary))
        if len(out) >= limit:
            break
    return out


def _parse_bing_html(html: str, *, limit: int) -> list[tuple[str, str]]:
    """从 Bing 结果页抽 (标题, 链接)。

    注意：Bing 在部分网络环境下返回的是 JS 渲染的空壳页，
    这时本函数会返回空列表，上层当作"没搜到"。这是可接受的降级，
    比抛异常让工具整体失败要好。
    """
    out: list[tuple[str, str]] = []
    pattern = re.compile(
        r"<h2[^>]*>\s*<a[^>]+href=\"(https?://[^\"]+)\"[^>]*>(.*?)</a>",
        re.I | re.S,
    )
    for m in pattern.finditer(html):
        url = m.group(1)
        if "bing.com" in url or "microsoft.com/bing" in url:
            continue
        title = strip_html(m.group(2)).strip()
        if not title:
            continue
        out.append((title, url))
        if len(out) >= limit:
            break
    return out


def _parse_ddg_html(html: str, *, limit: int) -> list[tuple[str, str]]:
    """从 DuckDuckGo HTML 结果页抽 (标题, 链接)。

    只认 `<a class="result__a" href="...">标题</a>` 这个结构。
    解析失败返回空列表，由上层当作"没搜到"处理——比抛异常温和，
    也让 provider 换实现时更容易替换。
    """
    out: list[tuple[str, str]] = []
    pattern = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.I | re.S,
    )
    for m in pattern.finditer(html):
        href = m.group(1)
        title = strip_html(m.group(2)).strip()
        url = _unwrap_ddg_redirect(href)
        if not url.startswith("http"):
            continue
        out.append((title or url, url))
        if len(out) >= limit:
            break
    return out


def _unwrap_ddg_redirect(href: str) -> str:
    """DuckDuckGo 的链接常被包成 //duckduckgo.com/l/?uddg=<真实地址>。"""

    if "uddg=" not in href:
        return (
            href if href.startswith("http") else f"https:{href}" if href.startswith("//") else href
        )
    try:
        qs = parse_qs(urlparse(href).query)
        real = (qs.get("uddg") or [""])[0]
        return unquote(real) or href
    except Exception:  # noqa: BLE001
        return href


def build_web_search_tool(
    *, enabled: bool, top_k: int = 3, provider: str = DEFAULT_PROVIDER
) -> WebSearchTool:
    """装配入口。关闭时返回一个 enabled=False 的工具（而不是 None），

    这样 Registry 里始终有这个工具名，模型不会因为"工具消失"而改变行为；
    调用时它会明确回一句"未启用"，让模型转回知识库。
    """
    if not enabled:
        return WebSearchTool(enabled=False)
    return WebSearchTool(
        enabled=True,
        top_k=top_k,
        provider=provider,
        client=httpx.AsyncClient(follow_redirects=True),
    )


__all__ = [
    "DEFAULT_PROVIDER",
    "WebSearchTool",
    "build_web_search_tool",
]


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
