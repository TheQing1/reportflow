"""评测统计：配对比较与置信区间。

为什么需要这个模块：

8 主题单次运行时，A 与 C 的覆盖率差 4.5pp，但单主题波动可达 25–50pp。
不算置信区间就报"提升 4.5pp"，等于把噪声当信号。

做法是**按主题配对**：同一个主题在 A 和 C 下各跑 2 次，先取各自均值，
得到 20 组配对差值，再算差值的均值和 95% CI。
配对能消掉"主题本身难度不同"这个最大的方差来源——同一主题下比，才比得准。

t 临界值用查表（不引 scipy）：df 10~60 的常用值足够，超出范围退到正态 1.96。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: 双侧 95% 的 t 临界值。key 为自由度，取不超过 df 的最大表项。
_T_TABLE: dict[int, float] = {
    5: 2.571,
    8: 2.306,
    10: 2.228,
    12: 2.179,
    15: 2.131,
    19: 2.093,
    20: 2.086,
    25: 2.060,
    30: 2.042,
    40: 2.021,
    60: 2.000,
}


def t_critical(df: int) -> float:
    if df < 1:
        return 1.96
    for key in sorted(_T_TABLE):
        if df <= key:
            return _T_TABLE[key]
    return 1.96


@dataclass(frozen=True)
class PairedResult:
    label: str
    n: int
    mean_diff: float
    ci_low: float
    ci_high: float
    significant: bool

    def describe(self, unit: str = "pp", scale: float = 100.0) -> str:
        sign = "+" if self.mean_diff >= 0 else ""
        return (
            f"{sign}{self.mean_diff * scale:.1f}{unit} "
            f"[{self.ci_low * scale:+.1f}, {self.ci_high * scale:+.1f}]"
        )


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = mean(values)
    var = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(var)


def ci95(values: list[float]) -> tuple[float, float]:
    """单样本均值的 95% CI。"""
    n = len(values)
    if n < 2:
        return (mean(values), mean(values))
    half = t_critical(n - 1) * stdev(values) / math.sqrt(n)
    m = mean(values)
    return (m - half, m + half)


def per_case_means(samples: dict[str, list[float]]) -> dict[str, float]:
    return {k: mean(v) for k, v in samples.items() if v}


def paired(a: dict[str, list[float]], b: dict[str, list[float]], *, label: str) -> PairedResult:
    """对同一批主题比较 b 相对 a 的差值（b - a），并给 95% CI。

    只使用两边都有数据的主题；任一档缺该主题就整体剔除，
    否则配对就退化成两个不同样本的比较。
    """
    diffs: list[float] = []
    for case_id, a_vals in a.items():
        b_vals = b.get(case_id)
        if not a_vals or not b_vals:
            continue
        diffs.append(mean(b_vals) - mean(a_vals))

    n = len(diffs)
    if n < 2:
        return PairedResult(label, n, 0.0, 0.0, 0.0, False)

    m = mean(diffs)
    sd = stdev(diffs)
    half = t_critical(n - 1) * sd / math.sqrt(n)
    low, high = m - half, m + half
    # 显著 = CI 不跨 0
    return PairedResult(label, n, m, low, high, significant=(low > 0 or high < 0))


def render_paired_table(results: list[PairedResult], *, metric: str = "覆盖率") -> list[str]:
    lines = [
        f"| 对比 | {metric}差值（95% CI） | 主题数 | 是否显著 |",
        "|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.label} | {r.describe()} | {r.n} | {'✅ 是' if r.significant else '❌ 否'} |"
        )
    return lines
