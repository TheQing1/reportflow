"""按章分配文档预算（material budget）。

为什么要这一层：

章节重复率的根因不是模型偷懒，也不是"不知道前面用过什么"（prompt 层去重实测无效），
而是**分章检索时对每章最匹配的都是同一批文档**。所以要在检索层解决。

关键机制（离线实测 `scripts/measure_overlap.py`）：
候选池只有 top_k 时在池内降权没用——换不出池外的文档。
必须先**过召回**（top_k 20），再对前面章节已用的文档降权，最后取 top 6。
离线结果：重叠度 57% → 43%（折扣 0.5）/ 36%（完全排除）。

分配发生在**任务提交时**而不是 research 节点运行时，这样：
- 五章的 research 仍然并行（不需要把 research 串起来，那会破坏并行的卖点）；
- 预算是纯本地计算，不调 LLM，几乎零成本。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Sequence

from ..contracts.retrieval import Retriever

#: 章节的通用维度。planner 还没跑时，用它把"每章该看什么方向"分开。
DEFAULT_DIMENSIONS: tuple[str, ...] = (
    "市场规模 现状 装机",
    "区域分布 格局 集中度",
    "成本 价格 经济性",
    "商业模式 政策 驱动因素",
    "风险 挑战 问题",
    "展望 趋势 预测",
)

POOL = 20
PER_CHAPTER = 6
#: 已被前序章节占用的文档的得分折扣
REUSE_DISCOUNT = 0.5


async def allocate_doc_budgets(
    *,
    topic: str,
    sections: int,
    retriever: Retriever,
    per_chapter: int = PER_CHAPTER,
    pool: int = POOL,
    dimensions: Sequence[str] | None = None,
) -> dict[int, list[str]]:
    """给每一章分配一组尽量不重叠的文档。

    贪心分配：按章顺序取，已分配的文档打折。不保证绝对互斥——
    语料里相关文档不够时，硬互斥会让某章拿到一堆不相关的文档，反而伤覆盖率。
    """
    dims = list(dimensions or DEFAULT_DIMENSIONS)
    if retriever.corpus_size() == 0:
        return {}

    used: set[str] = set()
    budgets: dict[int, list[str]] = {}
    for i in range(1, max(1, sections) + 1):
        query = f"{topic} {dims[(i - 1) % len(dims)]}"
        scored: dict[str, float] = {}
        for sn in await retriever.search(query, top_k=pool):
            score = sn.score * (REUSE_DISCOUNT if sn.doc_id in used else 1.0)
            scored[sn.doc_id] = max(scored.get(sn.doc_id, 0.0), score)
        picked = sorted(scored, key=lambda d: (-scored[d], d))[:per_chapter]
        budgets[i] = picked
        used.update(picked)
    return budgets


def allocate_sync(*, topic: str, sections: int, retriever: Retriever, **kw) -> dict[int, list[str]]:
    """同步包装。

    `submit()` 是同步的，但它可能已经在事件循环里被调（评测执行器就是 async），
    这时 `asyncio.run` 会直接抛"cannot be called from a running event loop"，
    于是预算被静默跳过——所以这里要判断：已在循环里就丢到独立线程跑。
    """
    coro = allocate_doc_budgets(topic=topic, sections=sections, retriever=retriever, **kw)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def overlap(budgets: dict[int, list[str]]) -> float:
    """预算本身的重叠度，用于自检与测试。"""
    counts: dict[str, int] = {}
    for docs in budgets.values():
        for d in docs:
            counts[d] = counts.get(d, 0) + 1
    if not counts:
        return 0.0
    return sum(1 for n in counts.values() if n >= 2) / len(counts)


__all__ = ["allocate_doc_budgets", "allocate_sync", "overlap", "DEFAULT_DIMENSIONS"]
