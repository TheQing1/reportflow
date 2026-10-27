"""从公开来源抓取真实语料，生成知识库文档。

为什么用脚本而不是手动复制粘贴：
1. **出处必须随文档一起落盘**。研报流水线的核心是「每个数字都能溯源到原文」，
   手抄会丢 URL 和发布方，最后生成的正文有角标但没有出处——那等于白做。
2. **格式必须统一**，才能和 sample-corpus 混用同一个检索器。

只抓**公开可引用**的来源：政府发布（国家能源局）、行业协会公开数据（CNESA）、
权威媒体报道（人民日报、经济日报、央广网）。
正文版权归原作者所有，本脚本仅做本地知识库用途的文本提取。

    python -m scripts.fetch_real_corpus              # 抓到 data/docs（默认）
    python -m scripts.fetch_real_corpus --out DIR    # 指定目录
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "data" / "docs"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# (分类目录, doc_id, 标题, 来源媒体, 发布日期, URL)
SOURCES: list[tuple[str, str, str, str, str, str]] = [
    (
        "01-行业概览",
        "doc_01_nea_report2026",
        "新型储能成为第一大调节性资源（中国新型储能发展报告2026）",
        "国家能源局",
        "2026-08-21",
        "https://www.nea.gov.cn/20260821/f3fa895ecd23480a83547ca29b12f4cc/c.html",
    ),
    (
        "01-行业概览",
        "doc_02_cnr_energy2025",
        "2025年全国能源形势：新型储能装机较2024年底增长84%",
        "央广网",
        "2026-01-31",
        "https://www.cnr.cn/tech/gstj/20260131/t20260131_527511710.shtml",
    ),
    (
        "01-行业概览",
        "doc_03_rmrb_energy",
        "能源保供与绿色转型同向发力（2025年能源与电力市场综述）",
        "人民日报",
        "2026-02-09",
        "https://paper.people.com.cn/zgcsb/pc/content/202602/09/content_30139543.html",
    ),
    (
        "01-行业概览",
        "doc_04_cnesa_annual",
        "2025年度CNESA储能数据：新型储能累计装机144.7GW",
        "中关村储能产业技术联盟",
        "2026-01-22",
        "https://www.cnesa.org/information/detail/?column_id=1&id=7798",
    ),
    (
        "02-政策与机制",
        "doc_05_nea_press",
        "2025年新型储能发展情况新闻发布会实录（分省份与分区域数据）",
        "国家能源局",
        "2026-01-30",
        "https://www.nea.gov.cn/xwfb/202601zb/iframe_text.htm",
    ),
    (
        "02-政策与机制",
        "doc_06_escn_policy",
        "2025年度储能政策：从规模扩张转向价值深耕（分省机制梳理）",
        "中国储能网",
        "2025-12",
        "https://www.escn.com.cn/news/show-2156863.html",
    ),
    (
        "02-政策与机制",
        "doc_07_cnnes_tou",
        "多地出台分时电价新政 储能产业站上转型关口",
        "中国城市报",
        "2025-12-01",
        "http://www.cnnes.cc/shichang/caijing/20251201/12341.html",
    ),
    (
        "03-市场与数据",
        "doc_08_ce_region",
        "新型储能装机大幅增长：分区域、单站规模与利用小时数",
        "中国经济网",
        "2026-02-02",
        "https://www.ce.cn/cysc/newmain/yc/jsxw/202602/t20260202_2743965.shtml",
    ),
    (
        "03-市场与数据",
        "doc_09_cnesa_h1",
        "2025上半年储能数据：累计装机首超100GW，新增23.03GW/56.12GWh",
        "中关村储能产业技术联盟",
        "2025-08-19",
        "https://www.cnesa.org/information/detail/?column_id=1&id=7332",
    ),
    (
        "03-市场与数据",
        "doc_10_overseas_orders",
        "我国储能出海爆发年：2025年新增海外订单366GWh，同比增长144%",
        "新京报贝壳财经",
        "2026-01-22",
        "https://news.qq.com/rain/a/20260122A05LKK00",
    ),
    (
        "03-市场与数据",
        "doc_11_overseas_factory",
        "中国储能企业出海进行时：订单高增叠加海外建厂潮",
        "证券时报网",
        "2026",
        "https://stcn.com/article/detail/3729088.html",
    ),
    (
        "04-技术与成本",
        "doc_12_ce_price",
        "价格三年暴跌近八成 储能业联手反内卷（产能与价格）",
        "中国经济网",
        "2025-08-21",
        "https://www.ce.cn/cysc//newmain/yc/jsxw/202508/t20250821_2449238.shtml",
    ),
    (
        "04-技术与成本",
        "doc_13_rmrb_cell",
        "储能电芯长期低价困局有望缓解（电芯报价与成本）",
        "中国能源报",
        "2025-09-15",
        "https://paper.people.com.cn/zgnyb/pc/content/202509/15/content_30105416.html",
    ),
    (
        "04-技术与成本",
        "doc_14_esie_price",
        "2025年度储能系统中标均价与EPC价格（CNESA年度数据）",
        "储能国际峰会暨展览会",
        "2026-01",
        "https://www.esie-expo.cn/?zhxw/82.html",
    ),
    (
        "05-竞争格局",
        "doc_15_sgpj_rank",
        "储能企业竞争格局：电池、PCS、系统集成全球榜单",
        "储能产业研究白皮书2026（CNESA）",
        "2026-03",
        "https://www.sgpjbg.com/searchtag/26694424.html",
    ),
    (
        "05-竞争格局",
        "doc_16_catl_orders",
        "宁德时代新签10GWh项目订单：2025年全球新增装机与中企海外大单",
        "电池工业网",
        "2026-01-30",
        "https://www.chbin.com.cn/index.php?a=index&aid=10259&c=View&m=home",
    ),
    (
        "03-市场与数据",
        "doc_17_cig_business",
        "工商业储能商业模式：投资回收期、IRR 与合作模式",
        "三个皮匠报告",
        "2026",
        "https://www.sgpjbg.com/searchtag/26657952.html",
    ),
    (
        "02-政策与机制",
        "doc_18_capacity_cnesa",
        "新型储能容量电价机制破局：各省政策盘点与发展趋势",
        "中关村储能产业技术联盟",
        "2025",
        "https://www.cnesa.org/information/detail/?id=7655",
    ),
    (
        "02-政策与机制",
        "doc_19_capacity_cpnn",
        "新型储能迈向价值竞争新阶段：容量补偿与火储同权",
        "中国能源新闻网",
        "2025-12-11",
        "https://www.cpnn.com.cn/news/baogao2023/202512/t20251211_1852714.html",
    ),
    (
        "04-技术与成本",
        "doc_20_gb44240",
        "中国首个储能强制性国标 GB 44240-2024 生效",
        "中国能源网",
        "2025-08-04",
        "https://www.cnenergynews.cn/cyjj/2025/08/04/detail_20250804225059.html",
    ),
    (
        "04-技术与成本",
        "doc_21_gb44240_tests",
        "新国标 GB 44240 用23项极限测试重塑储能安全门槛",
        "中国储能网",
        "2025-08",
        "https://c.m.163.com/news/a/K55S1TGD05533BYM.html",
    ),
    (
        "03-市场与数据",
        "doc_22_grid_escn",
        "全年新增装机超175GWh：分类型与分省份装机数据",
        "中国储能网",
        "2026-01",
        "https://www.escn.com.cn/news/show-2159683.html",
    ),
    (
        "03-市场与数据",
        "doc_23_grid_cnenergy",
        "2025全年用电量首超10万亿度：电网投资与储能装机",
        "中国能源网",
        "2026",
        "https://www.cnenergynews.cn/article/4Q19LYMXBiu",
    ),
    (
        "02-政策与机制",
        "doc_24_value_ce",
        "储能产业开启价值创造新篇章：容量电价打通三大收益渠道",
        "中国经济网",
        "2026-03-12",
        "https://www.ce.cn/cysc/newmain/yc/jsxw/202603/t20260312_2821784.shtml",
    ),
    (
        "04-技术与成本",
        "doc_25_penglai",
        "蓬莱101MW/205MWh：国内首个四类技术融合储能项目并网",
        "储能中国网",
        "2025-04-07",
        "http://www.cnnes.cc/anli/20250407/10507.html",
    ),
    (
        "04-技术与成本",
        "doc_26_ganquanpu",
        "甘泉堡混合电化学共享储能：钠离子与全钒液流试点",
        "中新网新疆",
        "2025-05-21",
        "https://www.xj.chinanews.com.cn/dizhou/2025-05-21/detail-iherrwae6456158.shtml",
    ),
    (
        "04-技术与成本",
        "doc_27_plant_tech",
        "储能电站市场前景：314Ah电芯、钠电、液流与构网型技术",
        "三个皮匠报告",
        "2026",
        "https://www.sgpjbg.com/searchtag/26291041.html",
    ),
    (
        "05-竞争格局",
        "doc_28_review_cinn",
        "储能行业2025年回顾与2026年展望：安全与投融资",
        "中国工业新闻网",
        "2026-03-21",
        "https://www.cinn.cn/yc/2026/03-21/qkNpnNMr.html",
    ),
    (
        "05-竞争格局",
        "doc_29_cnesa_review",
        "CNESA年度盘点2025：标准、资本、IPO与全球化",
        "同花顺基金",
        "2026-02-02",
        "https://fund.10jqka.com.cn/20260202/c674466446.shtml",
    ),
]

#: 正文中出现的这些片段几乎都是导航、版权、推广，不是内容
BLACKLIST = re.compile(
    r"(版权声明|未经授权|不得转载|责任编辑|编辑：|记者：|扫一扫|扫码|"
    r"相关阅读|推荐阅读|上一篇|下一篇|返回顶部|分享到|微博|微信朋友圈|"
    r"投稿邮箱|E-mail|联系电话|客服热线|加入我们|关于我们|网站地图|Copyright|"
    r"All Rights|京ICP|互联网新闻信息服务许可证|违法和不良信息举报|"
    r"客户端下载|报料热线|下载APP)"
)

#: 图表说明、页码之类
NOISE_ONLY = re.compile(r"^(图\d|表\d|图片来源|数据来源[:：]|点击查看|▲|△)\s*$")

#: 报刊版面/栏目/站点导航。它们短、含数字，所以躲得过「短且无数字」那条规则，
#: 但不承载任何行业信息，必须单独拦掉，否则正文第一段会是「第14版：能源」。
LAYOUT_NOISE = re.compile(
    r"(^第\d+版|^\d{4}年\d{1,2}月\d{1,2}日$|^(首页|行业资讯|政策标准|能源环境|"
    r"展会概况|新闻中心|关于我们|登录|注册)$|文库|任意下载|主办单位|承办单位)"
)

#: 正文句子的合理下限。调研过真实语料：有价值的句子都在 16 字以上。
MIN_PARAGRAPH = 16
#: 标题的下限（比正文宽松）。站点栏目名「展会概况」「能源环境」都在 4 字量级，
#: 而真实的小节标题都在 10 字以上，这个阈值能分开两者。
MIN_HEADING = 10

#: 「2026-01-22 18:05发布于…」这类时间戳/署名行
TIMESTAMP = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}.*(发布|来源|作者|记者)")

#: HTML 属性残留。标签剥离失败时（属性里带引号、标签未闭合）会漏出这种东西，
#: 它长得像内容但完全不可读，而且会污染检索词表。
HTML_LEFTOVER = re.compile(r"(data-[a-z-]+=|class=|href=|style=|onclick=|\.sub-menu|<[a-z/])")

_SENTENCE_PUNCT = re.compile(r"[。，、！？；：,.!?;:]")


def _looks_like_nav(text: str) -> bool:
    """栏目导航条：一整串 2~5 字的短词，中间没有任何句子标点。

    例如「新闻 要闻 金融 评论 产经 创投 滚动」「电池制造 锂电池 储能」。
    它们来自 fallback 路径（整块 div 被压成一行），逐个加进黑名单是打地鼠，
    所以按结构特征判：没标点 + 全短词 + 够多个。
    """
    if _SENTENCE_PUNCT.search(text) or len(text) > 120:
        return False
    parts = text.split()
    if len(parts) < 3:
        return False
    # 用平均长度而不是「每一个都短」：混进一个稍长的栏目名（如「主场搭建商推荐」）
    # 就会让 all() 失效，而导航条本来就是长短不一的
    return sum(len(p) for p in parts) / len(parts) <= 6


def _text_of(fragment: str) -> str:
    """标签片段 → 单行纯文本。

    `<br>` 必须压成**空格而不是换行**：保留换行会让「展会概况<br>展会介绍<br>…」
    变成一个多行长字符串，长度远远超过下限，于是整块导航堂而皇之通过过滤。
    压平之后它才会被 _looks_like_nav 按结构特征认出来。
    """
    s = re.sub(r"<[^>]+>", "", fragment)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


#: 块级边界。正文有时整篇躺在 <div> 里、只用 <br> 换行（常见于转载站），
#: 这时按标签配对提取会得到 0 段，必须能退化到「按行切分」。
_BLOCK_END = re.compile(r"</(div|p|li|tr|h[1-6]|section|article)>|<br\s*/?>", re.I)


def _keep(text: str, *, is_heading: bool) -> bool:
    if not text or BLACKLIST.search(text) or NOISE_ONLY.match(text):
        return False
    if LAYOUT_NOISE.search(text) or _looks_like_nav(text):
        return False
    if TIMESTAMP.search(text) or HTML_LEFTOVER.search(text):
        return False
    # 短段落几乎都是导航/栏目名。含数字也不放行——「第14版」这类正是靠数字躲过来的
    floor = MIN_HEADING if is_heading else MIN_PARAGRAPH
    if len(text) < floor:
        return False
    return True


def extract_text(raw: str) -> list[str]:
    """抽取正文段落。宁可少抽，也不要把导航混进来——假内容比少内容更糟。"""
    cleaned = re.sub(r"<(script|style|noscript|iframe|svg)\b.*?</\1>", "", raw, flags=re.S | re.I)

    blocks: list[tuple[str, bool]] = []
    for m in re.finditer(r"<(h[1-4]|p|li)\b[^>]*>(.*?)</\1>", cleaned, flags=re.S | re.I):
        tag, inner = m.group(1).lower(), m.group(2)
        text = _text_of(inner)
        if not text:
            continue
        is_heading = tag.startswith("h")
        # 噪声判定必须在加 "## " 前缀**之前**做：加上前缀后锚定的规则就匹配不上了，
        # "## 行业资讯" 之类栏目名会堂而皇之成为正文第一段。
        if not _keep(text, is_heading=is_heading):
            continue
        blocks.append((f"## {text}" if is_heading else text, is_heading))

    if len(blocks) < 5:
        # 结构化提取失败（正文在 div 里、只用 br 换行）→ 退化成按行切分
        flat = _BLOCK_END.sub("\n", cleaned)
        flat = re.sub(r"<[^>]+>", "", flat)
        flat = html.unescape(flat)
        blocks = []
        for line in flat.split("\n"):
            line = re.sub(r"[ \t\u3000]+", " ", line).strip()
            if line and _keep(line, is_heading=False):
                blocks.append((line, False))

    out: list[str] = []
    seen: set[str] = set()
    for text, _ in blocks:
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([a-zA-Z0-9\-_]+)", re.I)


def fetch(url: str, *, timeout: float = 25.0) -> str:
    """抓取并解码。

    必须先看 HTML 里的 <meta charset>：不少中文站点仍是 GBK，
    而 HTTP 头常常不含 charset。直接按 utf-8 解会得到一整篇乱码，
    而且**不报错**——乱码文档会安静地污染整个知识库。
    """
    with httpx.Client(
        headers={"User-Agent": UA},
        timeout=timeout,
        follow_redirects=True,
        trust_env=False,
    ) as client:
        r = client.get(url)
        r.raise_for_status()

    candidates: list[str] = []
    m = _META_CHARSET.search(r.content)
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore"))
    if r.encoding:
        candidates.append(r.encoding)
    candidates += ["utf-8", "gb18030"]

    for enc in candidates:
        try:
            text = r.content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
        # 中文站点解码正确时替换字符应该很少；乱码会大量出现
        if text.count("\ufffd") <= 3:
            return text
    return r.content.decode("utf-8", errors="replace")


def render(title: str, source: str, published: str, url: str, body: list[str]) -> str:
    meta = (
        f"> 来源：{source}　|　发布：{published}　|　"
        f"抓取：{date.today().isoformat()}　|　原文：{url}"
    )
    parts = [f"# {title}", "", meta, "", "## 正文", ""]
    parts.extend(body)
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取公开语料，生成知识库文档")
    parser.add_argument("--out", help=f"输出目录，默认 {DEFAULT_OUT}")
    parser.add_argument("--clean", action="store_true", help="清空目标目录后重建")
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else DEFAULT_OUT
    if args.clean and out.is_dir():
        for f in out.iterdir():
            if f.is_file() and f.suffix.lower() in {".md", ".markdown", ".txt"}:
                f.unlink()
    out.mkdir(parents=True, exist_ok=True)

    ok = skipped = 0
    for category, doc_id, title, source, published, url in SOURCES:
        try:
            raw = fetch(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  跳过 {doc_id}：抓取失败 {type(exc).__name__}", file=sys.stderr)
            skipped += 1
            continue
        body = extract_text(raw)
        if len(body) < 3:
            print(f"  跳过 {doc_id}：正文不足（{len(body)} 段）", file=sys.stderr)
            skipped += 1
            continue
        target_dir = out / category
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / f"{doc_id}.md").write_text(
            render(title, source, published, url, body), encoding="utf-8"
        )
        chars = sum(len(b) for b in body)
        print(f"  {doc_id:<26} {len(body):>3} 段 / {chars:>5} 字  [{category}]")
        ok += 1

    print(f"\n完成：{ok} 篇写入 {out}（跳过 {skipped}）")
    if ok:
        _write_index(out)
    return 0 if ok else 1


def _write_index(out: Path) -> None:
    """写一份 README，说明来源与性质。

    没有它，别人拿到这个目录无法判断「这些数字能不能信」——
    而这恰恰是研报流水线里最要紧的一件事。
    """
    lines = [
        "# 真实语料（公开来源抓取）",
        "",
        f"抓取日期：{date.today().isoformat()}",
        "",
        "**来源性质**：政府发布（国家能源局）、行业协会公开数据（CNESA）、",
        "权威媒体报道（人民日报、经济日报、证券时报、央广网）。",
        "**正文版权归原作者所有**，本目录仅用于本地知识库与功能验证，不用于商业分发。",
        "",
        "| 分类 | 说明 |",
        "|---|---|",
        "| `01-行业概览` | 总体规模、年度数据、能源形势 |",
        "| `02-政策与机制` | 发布会实录、分省政策、分时电价 |",
        "| `03-市场与数据` | 区域分布、上半年数据、海外订单与建厂 |",
        "| `04-技术与成本` | 价格与产能、电芯成本、中标均价 |",
        "| `05-竞争格局` | 全球榜单、企业订单 |",
        "",
        "每篇文档的第一行元信息含**原文 URL**，生成报告时的角标可据此回溯。",
        "",
        "> 与 `sample-corpus/`（虚构数据）的区别：本目录是真实公开数据，",
        "> 适合验证「引用能不能溯源」；但阶段三的评测标注是基于合成语料做的，",
        "> 换成本目录后评测结论需要重跑。",
        "",
        "重新抓取：`cd backend && python -m scripts.fetch_real_corpus`",
        "",
    ]
    (out / "README.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
