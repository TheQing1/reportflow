"""离线量化：章节重复率的根因到底在哪一层。

prompt 层去重（告诉 Writer"前面用过什么"）实测无效（v7-dedup：43.4% → 44.6%，不显著），
说明 Writer 不是"不知道"，而是**素材包里本来就没有别的东西可选**。

这个脚本不调 LLM，只做检索计算，回答一个更靠前的问题：
如果改成在**检索层**对前面章节已用的文档降权，重叠度能降多少？

用法：
    python -m scripts.measure_overlap
"""

from __future__ import annotations

import asyncio
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import LocalCorpusRetriever

TOP_K = 6
PENALTY = 0.25  # 已用文档的得分折扣


def overlap_rate(per_chapter_docs: list[set[str]]) -> float:
    """被 2 章以上选中的文档占比——与评测里的 repeat_rate 同一口径。"""
    counts: dict[str, int] = {}
    for docs in per_chapter_docs:
        for d in docs:
            counts[d] = counts.get(d, 0) + 1
    if not counts:
        return 0.0
    repeated = sum(1 for n in counts.values() if n >= 2)
    return repeated / len(counts)


async def measure(topic: str, chapters: list[list[str]], retriever) -> tuple[float, float]:
    """返回 (现状重叠度, 检索层降权后的重叠度)。"""
    baseline: list[set[str]] = []
    penalised: list[set[str]] = []

    for queries in chapters:
        # 现状：每章独立检索，取 top_k
        seen: dict[str, float] = {}
        for q in queries:
            for sn in await retriever.search(q, top_k=TOP_K):
                seen.setdefault(sn.doc_id, sn.score)
        baseline.append(set(sorted(seen, key=lambda d: -seen[d])[:TOP_K]))

    used: set[str] = set()
    for queries in chapters:
        seen = {}
        for q in queries:
            for sn in await retriever.search(q, top_k=TOP_K):
                score = sn.score * (PENALTY if sn.doc_id in used else 1.0)
                seen[sn.doc_id] = max(seen.get(sn.doc_id, 0.0), score)
        picked = set(sorted(seen, key=lambda d: -seen[d])[:TOP_K])
        penalised.append(picked)
        used |= picked

    return overlap_rate(baseline), overlap_rate(penalised)


async def main() -> None:
    retriever = LocalCorpusRetriever.from_directory("../data/docs")
    if retriever.corpus_size() == 0:
        print("知识库为空，先跑 python -m app.cli corpus init")
        return

    # 用评测集里的主题与检索式（planner 生成的 queries 就在数据集的 must_cover 之外，
    # 这里直接用主题词 + 通用维度，与 A 组的检索方式一致，足够说明检索层问题）
    #: 每项 = (主题, [第1章检索式, 第2章检索式, …])，注意是**按章分组**的二维结构
    topics = [
        (
            "工商业储能",
            [
                ["工商业储能 装机规模 增速"],
                ["工商业储能 区域分布 价差"],
                ["工商业储能 成本 经济性"],
                ["工商业储能 商业模式 收益"],
                ["工商业储能 2026 展望"],
            ],
        ),
        (
            "电网侧储能",
            [
                ["电网侧储能 新增装机"],
                ["电网侧储能 区域分布"],
                ["电网侧储能 收益机制"],
                ["电网侧储能 技术路线"],
                ["电网侧储能 展望"],
            ],
        ),
        (
            "储能技术路线",
            [
                ["磷酸铁锂 占比"],
                ["钠离子电池 示范 成本"],
                ["液流电池 长时储能"],
                ["储能 变流器 出货"],
                ["储能 安全 标准"],
            ],
        ),
        (
            "竞争格局",
            [
                ["系统集成 集中度 CR5"],
                ["集成商 毛利率 低价"],
                ["电芯 产能利用率"],
                ["储能 投融资 IPO"],
                ["2026 储能 展望"],
            ],
        ),
        (
            "海外市场",
            [
                ["美国 储能 新增装机"],
                ["欧洲 户用储能 装机"],
                ["中东 储能 招标"],
                ["储能电池 出口"],
                ["海外 市场 展望"],
            ],
        ),
    ]

    print(f"{'主题':<12}{'现状重叠':>10}{'检索降权后':>12}{'变化':>10}")
    before_all, after_all = [], []
    for topic, chapters in topics:
        before, after = await measure(topic, chapters, retriever)
        before_all.append(before)
        after_all.append(after)
        print(f"{topic:<12}{before:>10.0%}{after:>12.0%}{after - before:>+10.0%}")

    print(
        f"\n均值：{st.mean(before_all):.0%} → {st.mean(after_all):.0%}"
        f"（{(st.mean(after_all) - st.mean(before_all)) * 100:+.1f}pp）"
    )
    print("\n这只是检索层能拿到的上限：真正落地还要改 research 节点，且需实测是否伤覆盖率。")


if __name__ == "__main__":
    asyncio.run(main())
