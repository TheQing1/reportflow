"""工具层的测试。

这一层出错的代价和别的层不一样：工具挂了会让**整章 research 失败**，
但工具"静默返回空"更糟——模型会据此认为"网上真没有"，然后开始编。
所以这里重点覆盖的是"失败与边界"而不是"成功路径"。

全部离线：不联网、不调真实模型。
"""

from __future__ import annotations

import asyncio
import unittest

from app.contracts.retrieval import Snippet
from app.contracts.tools import (
    ToolRegistry,
    ToolResult,
    ToolSpec,
    strip_html,
    truncate,
)
from app.tools import build_default_registry
from app.tools.kb_search import KBSearchTool
from app.tools.web_search import (
    WebSearchTool,
    _doc_id_of,
    _parse_ddg_html,
    _parse_so360_html,
    _unwrap_ddg_redirect,
)


class _FakeRetriever:
    """可控的检索替身：固定返回若干片段，或不返回。"""

    def __init__(self, snippets: list[Snippet] | None = None) -> None:
        self._snippets = snippets or []
        self.queries: list[str] = []
        self.raise_on_search: Exception | None = None

    async def search(self, query: str, *, top_k: int = 4, chapters_hint: str = ""):
        self.queries.append(query)
        if self.raise_on_search is not None:
            raise self.raise_on_search
        return self._snippets[:top_k]

    def corpus_size(self) -> int:
        return len(self._snippets)

    def full_text(self, doc_id: str) -> str:
        for s in self._snippets:
            if s.doc_id == doc_id:
                return s.text
        return ""

    def doc_ids(self) -> list[str]:
        return sorted({s.doc_id for s in self._snippets})


def _snip(doc_id: str, text: str, page: int | None = None) -> Snippet:
    return Snippet(doc_id=doc_id, title=f"标题_{doc_id}", page=page, text=text, score=1.0)


class TruncateTest(unittest.TestCase):
    def test_short_text_untouched(self) -> None:
        text = "短文本。"
        out, cut = truncate(text, limit=100)
        self.assertEqual(out, text)
        self.assertFalse(cut)

    def test_cuts_on_sentence_boundary(self) -> None:
        """必须在句子边界截断，否则会切出"半句话"，模型引用了就成了假引用。"""
        text = "第一句内容完整。第二句也很完整。第三句开始被截断了不应该出现"
        out, cut = truncate(text, limit=20)
        self.assertTrue(cut)
        self.assertLessEqual(len(out), 20)
        self.assertTrue(out.endswith("。"), f"截断点不在句末: {out!r}")

    def test_limit_is_respected(self) -> None:
        out, _ = truncate("啊" * 5000, limit=100)
        self.assertLessEqual(len(out), 100)


class StripHtmlTest(unittest.TestCase):
    def test_removes_script_and_style(self) -> None:
        html = (
            "<html><head><style>.a{color:red}</style></head><body><p>正文在这里</p></body></html>"
        )
        text = strip_html(html)
        self.assertIn("正文在这里", text)
        self.assertNotIn("color:red", text)
        self.assertNotIn("<p>", text)

    def test_keeps_paragraph_breaks(self) -> None:
        """段落边界必须保留：全压成一行的话，截断时会切到句子中间。"""
        html = "<p>第一段</p><p>第二段</p>"
        text = strip_html(html)
        self.assertIn("\n", text)
        self.assertIn("第一段", text)
        self.assertIn("第二段", text)

    def test_unescapes_entities(self) -> None:
        text = strip_html("<p>a &amp; b &lt;c&gt; &quot;d&quot;</p>")
        self.assertIn("a & b <c>", text)


class RegistryTest(unittest.TestCase):
    class _Dummy:
        spec = ToolSpec(name="dummy", description="d", parameters={"type": "object"})

        async def run(self, arguments):  # noqa: ANN001
            return ToolResult(ok=True, content="ok")

    def test_register_and_lookup(self) -> None:
        reg = ToolRegistry([self._Dummy()])
        self.assertIn("dummy", reg)
        self.assertEqual(reg.names(), ["dummy"])
        self.assertIsNotNone(reg.get("dummy"))

    def test_duplicate_name_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ToolRegistry([self._Dummy(), self._Dummy()])

    def test_items_are_collected_per_epoch(self) -> None:
        """片段收集必须能按"每次执行"清空，否则第 2 章会用上第 1 章的网页。"""
        reg = ToolRegistry([self._Dummy()])
        reg.begin()
        reg.record_items([{"doc_id": "a", "text": "x"}])
        self.assertEqual(len(reg.last_items()), 1)
        reg.begin()
        self.assertEqual(len(reg.last_items()), 0)

    def test_empty_registry_yields_no_specs(self) -> None:
        self.assertEqual(ToolRegistry().llm_specs(), ())


class KBSearchToolTest(unittest.TestCase):
    def test_returns_items_and_renders_original_text(self) -> None:
        retriever = _FakeRetriever([_snip("doc_01", "2025 年装机 8.4GWh，同比增长 62%。")])
        tool = KBSearchTool(retriever=retriever)  # type: ignore[arg-type]
        res = asyncio.run(tool.run({"query": "装机规模"}))
        self.assertTrue(res.ok)
        self.assertEqual(len(res.items), 1)
        self.assertEqual(res.items[0]["doc_id"], "doc_01")
        # 原文必须逐字出现在回给模型的文本里（上下文字符串匹配的前提）
        self.assertIn("8.4GWh", res.content)
        self.assertIn("doc_01", res.content)

    def test_empty_result_is_ok_not_failure(self) -> None:
        """查到空是正常结果。报成失败会诱导模型反复重试同一个词。"""
        tool = KBSearchTool(retriever=_FakeRetriever([]))  # type: ignore[arg-type]
        res = asyncio.run(tool.run({"query": "不存在的主题"}))
        self.assertTrue(res.ok)
        self.assertEqual(res.items, ())
        self.assertIn("未检索到", res.content)

    def test_search_exception_becomes_failure_result(self) -> None:
        retriever = _FakeRetriever([_snip("d", "t")])
        retriever.raise_on_search = RuntimeError("索引损坏")
        tool = KBSearchTool(retriever=retriever)  # type: ignore[arg-type]
        res = asyncio.run(tool.run({"query": "x"}))
        self.assertFalse(res.ok)  # 不抛异常，而是 ok=False 回给模型
        self.assertIn("索引损坏", res.error)

    def test_missing_query_rejected(self) -> None:
        tool = KBSearchTool(retriever=_FakeRetriever([_snip("d", "t")]))  # type: ignore[arg-type]
        res = asyncio.run(tool.run({}))
        self.assertFalse(res.ok)

    def test_top_k_clamped(self) -> None:
        snippets = [_snip(f"doc_{i:02d}", f"第{i}段") for i in range(10)]
        tool = KBSearchTool(retriever=_FakeRetriever(snippets))  # type: ignore[arg-type]
        res = asyncio.run(tool.run({"query": "x", "top_k": 999}))
        self.assertLessEqual(len(res.items), 8, "top_k 上限必须是 8")


class WebSearchDisabledTest(unittest.TestCase):
    """默认关闭时的行为——这是最重要的一组：必须**完全不联网**。"""

    def test_disabled_reports_clearly(self) -> None:
        tool = WebSearchTool(enabled=False)
        res = asyncio.run(tool.run({"query": "储能"}))
        self.assertFalse(res.ok)
        # 必须明确说"没启用"，而不是返回空结果。
        # 返回空会让模型以为"网上也没有"，进而编造。
        self.assertIn("未启用", res.error)

    def test_disabled_has_no_http_client(self) -> None:
        """关闭时连 httpx 客户端都不应创建，杜绝任何代码路径偷偷发请求。"""
        tool = WebSearchTool(enabled=False)
        self.assertIsNone(tool._client)  # noqa: SLF001 - 就是要断言这个内部不变量

    def test_disabled_tool_still_registered_in_specs(self) -> None:
        """关闭时工具仍出现在声明里，但调用会明确返回"未启用"。"""
        tool = WebSearchTool(enabled=False)
        self.assertIn("web_search", tool.spec.name)


class So360ParseTest(unittest.TestCase):
    """360 解析器：返回 (标题, 真实URL, 摘要) 三元组。

    为什么单独测它：这是"能联网"这件事的核心。它错了整条联网链路就废了，
    而错误形态还特别隐蔽——解析出 0 条会被当成"网上没搜到"，
    模型于是"如实说没资料"，看起来完全正常。
    """

    #: 贴近真实结构的最小样本（来自实测页面，已裁剪）
    _HTML = """
    <li class="res-list" data-result-type="richsum">
      <h3 class="res-title"><a href="https://www.so.com/link?m=abc"
         data-mdurl="https://m.gmw.cn/2026-01/30/content_1304325576.htm">国家能源局:<em>2025</em>年装机增长84%</a></h3>
      <div class='res-rich'><span class="res-list-summary">
        截至2025年底,全国已建成投运新型储能装机规模达到1.36亿千瓦,与十三五末相比增长超40倍。
      </span></div>
    </li>
    <li class="res-list">
      <h3 class="res-title"><a href="https://www.so.com/link?m=xyz"
         data-mdurl="https://xueqiu.com/123/456">2025年新增装机规模</a></h3>
      <div class='res-rich'><span class="res-list-summary">
        2025年中国新型储能新增装机规模为64.6GW/196.5GWh,同比增长52%。
      </span></div>
    </li>
    """

    def test_extracts_three_tuple(self) -> None:
        hits = _parse_so360_html(self._HTML, limit=5)
        self.assertEqual(len(hits), 2)
        title, url, summary = hits[0]
        self.assertIn("国家能源局", title)
        # 关键：URL 必须取 data-mdurl（真实地址），不是 so.com 跳转链接
        self.assertEqual(url, "https://m.gmw.cn/2026-01/30/content_1304325576.htm")
        self.assertIn("1.36亿千瓦", summary)

    def test_uses_mdurl_not_redirect_link(self) -> None:
        """必须拿 data-mdurl。拿跳转链接会导致所有 doc_id 都变成 web_so.com_link。"""
        hits = _parse_so360_html(self._HTML, limit=5)
        for _t, url, _s in hits:
            self.assertNotIn("so.com/link", url)

    def test_summary_html_stripped(self) -> None:
        """摘要里的 <em> 高亮必须去掉，否则 quote 逐字匹配会失败。"""
        hits = _parse_so360_html(self._HTML, limit=5)
        self.assertNotIn("<em>", hits[0][2])
        self.assertNotIn("</em>", hits[0][2])

    def test_short_summary_dropped(self) -> None:
        """摘要过短的条目丢弃——多半是「查看更多」引导语，没有引用价值。"""
        html = """
        <li class="res-list">
          <h3 class="res-title"><a href="/link?m=a" data-mdurl="https://e.com/a">标题</a></h3>
          <span class="res-list-summary">太短</span>
        </li>
        """
        self.assertEqual(_parse_so360_html(html, limit=5), [])

    def test_missing_mdurl_dropped(self) -> None:
        html = """
        <li class="res-list">
          <h3 class="res-title"><a href="/link?m=a">没有 mdurl 的条目</a></h3>
          <span class="res-list-summary">这是一段足够长的摘要内容，应该被丢弃因为缺少 data-mdurl 属性</span>
        </li>
        """
        self.assertEqual(_parse_so360_html(html, limit=5), [])

    def test_respects_limit(self) -> None:
        self.assertEqual(len(_parse_so360_html(self._HTML, limit=1)), 1)

    def test_garbage_html_returns_empty(self) -> None:
        self.assertEqual(_parse_so360_html("<div>什么都没有</div>", limit=5), [])


class ProviderConfigTest(unittest.TestCase):
    def test_unknown_provider_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WebSearchTool(enabled=True, provider="不存在的源")

    def test_default_provider_is_so360(self) -> None:
        """默认用 360：这是实测唯一「可达 + 返回真实结果」的源。

        DuckDuckGo 在本机不可达、Bing 返回 JS 空壳页——都试过才定的这个默认值。
        """
        tool = WebSearchTool(enabled=False)
        self.assertEqual(tool.provider, "so360")


class RenderFormatTest(unittest.TestCase):
    """回给模型的文本必须显式带 doc_id。

    这是踩过的真坑：原先只写「[网页1]」，模型就自己编了个 `web_1` 当 source_id，
    结果全部被溯源校验丢弃（"source_id 不在检索结果中"）——
    网页明明查到了，却一条 fact 也用不上，而且不报错、只是"素材变少"。
    这一条锁住"渲染里必须出现真实 doc_id"。
    """

    def test_web_render_includes_real_doc_id(self) -> None:
        items = [
            {
                "doc_id": "web_xueqiu.com_123456",
                "title": "标题",
                "url": "https://xueqiu.com/123456",
                "text": "一段足够长的原始摘要内容，用来说明这个字段必须原样出现。",
                "truncated": False,
            }
        ]
        text = WebSearchTool._render(items)
        self.assertIn("doc_id=web_xueqiu.com_123456", text)
        # 不能只给序号
        self.assertNotIn("[网页1]", text)
        self.assertIn("source_id 必须照抄", text)

    def test_kb_render_includes_doc_id(self) -> None:
        """两个工具格式保持一致，模型才不会在不同工具间混淆 id 规则。"""
        items = (
            {
                "doc_id": "doc_07_region_cig",
                "title": "区域",
                "page": 12,
                "text": "浙江地区工商业储能项目全投资内部收益率约12.5%。",
            },
        )
        text = KBSearchTool._render(items)
        self.assertIn("doc_id=doc_07_region_cig", text)
        self.assertIn("页码=12", text)


class DdgParseTest(unittest.TestCase):
    def test_parses_result_links(self) -> None:
        html = (
            '<a class="result__a" href="https://example.com/a">标题A</a>'
            '<a class="result__a" href="https://example.com/b">标题B</a>'
        )
        hits = _parse_ddg_html(html, limit=5)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0][1], "https://example.com/a")
        self.assertEqual(hits[0][0], "标题A")

    def test_respects_limit(self) -> None:
        html = "".join(f'<a class="result__a" href="https://e.com/{i}">t{i}</a>' for i in range(10))
        self.assertEqual(len(_parse_ddg_html(html, limit=3)), 3)

    def test_broken_html_returns_empty_not_crash(self) -> None:
        self.assertEqual(_parse_ddg_html("<div>没有结果</div>", limit=5), [])

    def test_unwraps_redirect(self) -> None:
        wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage"
        self.assertEqual(_unwrap_ddg_redirect(wrapped), "https://example.com/page")


class DocIdTest(unittest.TestCase):
    def test_doc_id_is_stable_and_readable(self) -> None:
        """doc_id 会直接进正文角标，必须稳定且人能看懂来源站点。"""
        a = _doc_id_of("https://www.nea.gov.cn/2026/report.html")
        b = _doc_id_of("https://www.nea.gov.cn/2026/report.html")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("web_"))
        self.assertIn("nea.gov.cn", a)
        # 不能含空格/斜杠等会破坏角标语法的字符（点号是刻意保留的，
        # 因为域名需要可读；markdown 角标 `[^web_a.b_c]` 语法上完全合法）
        for ch in a:
            self.assertTrue(ch.isalnum() or ch in "-_.", f"非法字符 {ch!r} in {a}")
        self.assertNotIn("/", a)
        self.assertNotIn(" ", a)

    def test_different_urls_differ(self) -> None:
        self.assertNotEqual(_doc_id_of("https://a.com/x"), _doc_id_of("https://a.com/y"))


class DefaultRegistryTest(unittest.TestCase):
    def test_no_web_by_default(self) -> None:
        reg = build_default_registry(retriever=_FakeRetriever([]), enable_web=False)
        self.assertIn("kb_search", reg)
        self.assertNotIn("web_search", reg, "默认不应注册联网工具")

    def test_web_when_explicitly_enabled(self) -> None:
        reg = build_default_registry(retriever=_FakeRetriever([]), enable_web=True)
        self.assertIn("web_search", reg)

    def test_no_retriever_means_no_kb_tool(self) -> None:
        """没有知识库时宁可不给工具，也不要给一个"调用必失败"的空壳。"""
        reg = build_default_registry(retriever=None, enable_web=False)
        self.assertEqual(len(reg), 0)


if __name__ == "__main__":
    unittest.main()
