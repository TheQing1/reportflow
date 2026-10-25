"""评测执行器：跑三档方案、算指标、出报告。

三档的变量控制（写死在这里，避免出现「哪次跑的时候多给了素材」这种事）：

| 档 | 图结构 | 素材来源 |
|---|---|---|
| A | 无 DAG，一次调用生成全文 | 同一个 retriever，总片段数与 B/C 同量级 |
| B | planner → 并行 research → 串行 write | 每章独立检索 |
| C | 同 B，另加 review（含打回重写） | 同 B |

A 组必须喂同样的素材，否则「多 Agent 更好」的结论不成立——这是这类评测最容易作弊的地方。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ..assembler import Runtime
from ..contracts.eval import ARM_LABELS, Arm, ArmSummary, CaseMetrics, TopicCase
from ..contracts.llm import LLMClient, LLMMessage, LLMRequest, ModelPrice
from ..core.logging import get_logger
from .judge import Judge
from .report import (
    ReportArtifact,
    citation_validity,
    collect_from_task,
    collect_from_text,
    must_cite_hit,
)
from .semantic import SupportAuditor, sample_assertions
from .stats import PairedResult, ci95, paired, per_case_means

_SINGLE_SHOT_SYSTEM = """[task:writer] 你是资深研报撰写者。请根据给定的素材，直接写出一份完整研报。

要求：
1. 分成用户指定数量的章节，每章用 ## 标题 起头。
2. 只能用素材里的事实，不得引入外部知识，不得补充素材中没有的数字、公司名、年份。
3. 每一个含有数据或结论的句子，都要在句末标注角标 [^doc_id]，doc_id 必须照抄素材标注。
4. 素材不足以支撑某章时，用一句话说明，不要编造。
5. 每章 400-600 字，语言连贯、有分析而非罗列。

只输出 markdown 正文，不要任何解释。"""

#: A 组检索的总片段数（B/C 每章 3-6 片段 × 5 章，量级相当）
SINGLE_SHOT_TOP_K = 14

#: A 组的检索维度。**只能用主题和通用维度词，绝不能碰 must_cover**——
#: 用标注要点去检索等于拿参考答案找资料，对比立刻失效。
#: 第一版评测就犯了这个错：A 组覆盖率 97.5%，看起来单 Agent 最强，
#: 实际是它拿着答案去查资料。修掉之后重跑才有意义。
_QUERY_DIMENSIONS = (
    "市场规模 装机",
    "区域分布 省份",
    "竞争格局 份额",
    "商业模式 收益",
    "技术路线 成本",
    "政策 机制",
    "风险 挑战",
    "发展趋势 展望",
)


class EvalRunner:
    def __init__(
        self,
        *,
        runtime: Runtime,
        judge_llm: LLMClient | None = None,
        out_dir: str | Path = "eval/results",
        run_id: str | None = None,
        allow_replay: bool = False,
    ) -> None:
        self.rt = runtime
        mode = str(getattr(runtime, "llm_mode", "live"))
        if mode != "live" and not allow_replay:
            # 这条闸很关键：回放模式下同一 prompt 永远返回同一份缓存，
            # 「重复 3 次取均值」会变成「同一次结果算 3 遍」，
            # 置信区间被人为压窄，看起来"显著"，其实什么都没测。
            raise RuntimeError(
                f"评测要求 LLM_MODE=live，当前是 {mode!r}。"
                "回放会让重复实验变成假的重复，覆盖率也不再代表模型真实水平。"
                "只做链路冒烟可加 --allow-replay，但那种结果不能当结论。"
            )
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.out_dir = Path(out_dir) / self.run_id
        self.cases_dir = self.out_dir / "cases"
        self.cases_dir.mkdir(parents=True, exist_ok=True)
        self.repeats = 1
        #: 判官是谁，直接写进报告——不写清楚，读者会默认"判官=被测模型"
        settings = getattr(runtime, "settings", None)
        judge_model = getattr(settings, "llm_judge_model", "") or getattr(
            settings, "llm_model_primary", "?"
        )
        self.judge_label = (
            judge_model
            if getattr(settings, "llm_judge_model", "")
            else f"{judge_model}（与生成同模型，存在自我偏好）"
        )
        if judge_llm is None:
            if runtime.gateway is None:
                raise RuntimeError("评测需要 LLM：请用 --profile real，或显式传入 judge_llm")
            judge_llm = runtime.gateway
        self.judge = Judge(judge_llm)
        self.auditor = SupportAuditor(judge_llm)
        self._price = ModelPrice(
            runtime.settings.llm_price_input_per_m,
            runtime.settings.llm_price_output_per_m,
        )
        self._log = get_logger("evaluation")
        #: 是否做语义支持度审计（每份报告多一次 judge 调用）
        self.audit_semantics = True

    # ---------------------------------------------------------------- 对外

    def _run_key(self) -> str:
        """批次指纹。

        一开始取 `run_id[-6:]`，结果 `audit-smoke` 和 `judge-smoke` 截尾都是 `-smoke`，
        新批次直接复用了旧批次已经 DONE 的任务——**整批数据是旧的，而且看起来完全正常**
        （0.02 秒跑完、指标齐全）。这种"静默复用"比报错危险得多。
        改成对完整 run_id 取哈希，长度够、且任何两个批次不会再撞。
        """
        return hashlib.sha1(self.run_id.encode("utf-8")).hexdigest()[:8]

    async def run_all(
        self,
        cases: list[TopicCase],
        arms: list[Arm],
        *,
        repeats: int = 1,
        resume: bool = True,
    ):
        """按 (档, 重复, 主题) 三重循环执行。

        每个 case 跑完立刻落盘，因此中断后重跑会自动跳过已完成的——
        40 分钟的批次不该因为中途一次 API 抖动而全部白跑。
        """
        self.repeats = max(1, repeats)
        results: dict[Arm, list[CaseMetrics]] = {arm: [] for arm in arms}
        total = len(arms) * len(cases) * self.repeats
        done = 0

        for arm in arms:
            for rep in range(1, self.repeats + 1):
                for case in cases:
                    done += 1
                    cache = self.cases_dir / f"{arm.value}-{case.id}-r{rep}.json"
                    if resume and cache.is_file():
                        metric = CaseMetrics.from_dict(
                            json.loads(cache.read_text(encoding="utf-8"))
                        )
                        self._log.info(
                            "[%s] %d/%d %-18s r%d 已完成，读取缓存",
                            arm.value, done, total, case.id, rep,
                        )
                    else:
                        self._log.info(
                            "[%s] %d/%d %-18s r%d | %s",
                            arm.value, done, total, case.id, rep, case.topic,
                        )
                        metric = await self._run_case(arm, case, rep)
                        cache.write_text(
                            json.dumps(metric.to_dict(), ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                    results[arm].append(metric)

        self.write_reports(results)
        return results

    async def _run_case(self, arm: Arm, case: TopicCase, rep: int = 1) -> CaseMetrics:
        started = time.monotonic()
        try:
            if arm is Arm.A_SINGLE:
                metric, artifact, valid_sources = await self._run_single_shot(case)
            else:
                metric, artifact, valid_sources = await self._run_dag(case, arm, rep)
        except Exception as exc:  # noqa: BLE001 - 单例失败不应中断整个评测
            self._log.error("[%s] %s 执行失败: %s: %s", arm.value, case.id, type(exc).__name__, exc)
            return CaseMetrics(
                case_id=case.id,
                arm=arm,
                task_id="",
                status="ERROR",
                error=f"{type(exc).__name__}: {exc}",
                elapsed_s=time.monotonic() - started,
            )

        metric.elapsed_s = time.monotonic() - started
        await self._score(case, metric, artifact, valid_sources)
        return metric

    # ---------------------------------------------------------------- A 组

    async def _run_single_shot(self, case: TopicCase) -> tuple[CaseMetrics, ReportArtifact, set[str]]:
        retriever = self.rt.retriever
        if retriever is None or self.rt.gateway is None:
            raise RuntimeError("A 组需要 retriever 与 gateway（--profile real）")

        queries = [case.topic, *[f"{case.topic} {d}" for d in _QUERY_DIMENSIONS]]
        seen: set[tuple[str, str]] = set()
        snippets = []
        for q in queries:
            for sn in await retriever.search(q, top_k=6):
                key = (sn.doc_id, sn.text[:60])
                if key in seen:
                    continue
                seen.add(key)
                snippets.append(sn)
        snippets = snippets[:SINGLE_SHOT_TOP_K]

        blocks = "\n\n".join(
            f"[片段{i}] doc_id={sn.doc_id} 标题={sn.title}\n原文：{sn.text}"
            for i, sn in enumerate(snippets, 1)
        )
        user = f"研究主题：{case.topic}\n章节数量：{case.sections}\n\n可用素材：\n{blocks}"
        req = LLMRequest(
            messages=[
                LLMMessage(role="system", content=_SINGLE_SHOT_SYSTEM),
                LLMMessage(role="user", content=user),
            ],
            model="",
            temperature=0.2,
            max_tokens=4000,
            timeout_s=max(120.0, self.rt.settings.llm_timeout_s * 3),
            tag=f"single-{case.id}",
        )
        resp = await self.rt.gateway.complete(req)
        artifact = collect_from_text(resp.text)

        metric = CaseMetrics(
            case_id=case.id,
            arm=Arm.A_SINGLE,
            task_id=f"eval-A-{case.id}",
            status="DONE",
            cost=self._price.cost(resp.usage),
            llm_calls=1,
            char_count=artifact.char_count,
        )
        return metric, artifact, {sn.doc_id for sn in snippets}

    # ---------------------------------------------------------------- B/C 组

    async def _run_dag(
        self, case: TopicCase, arm: Arm, rep: int = 1
    ) -> tuple[CaseMetrics, ReportArtifact, set[str]]:
        # task_id 必须带 rep：否则第 2 次重复会命中「任务已 DONE，无需执行」，
        # 拿第 1 次的结果冒充第 2 次，重复实验变成假的
        task_id = f"eval{arm.value}-{case.id}-r{rep}-{self._run_key()}"
        self.rt.runner.submit(
            topic=case.topic,
            sections=case.sections,
            task_id=task_id,
            with_review=arm is Arm.C_MULTI_REVIEW,
        )
        result = await self.rt.runner.run(task_id)
        artifact = collect_from_task(self.rt.store, task_id, case.sections)

        valid_sources: set[str] = set()
        for node in self.rt.store.list_nodes(task_id):
            if node.type.value != "research" or not node.output:
                continue
            for fact in node.output.get("key_facts", []):  # type: ignore[union-attr]
                valid_sources.add(str(fact.get("source_id")))

        cost_report = self.rt.spans.summary(task_id) if self.rt.spans else None
        metric = CaseMetrics(
            case_id=case.id,
            arm=arm,
            task_id=task_id,
            status=result.task_status.value,
            cost=cost_report.cost if cost_report else 0.0,
            llm_calls=cost_report.calls if cost_report else 0,
            char_count=artifact.char_count,
        )
        return metric, artifact, valid_sources

    # ---------------------------------------------------------------- 打分

    async def _score(
        self,
        case: TopicCase,
        metric: CaseMetrics,
        artifact: ReportArtifact,
        valid_sources: set[str],
    ) -> None:
        valid, total = citation_validity(artifact, valid_sources)
        metric.valid_citations = valid
        metric.total_citations = total
        metric.citation_validity = (valid / total) if total else 0.0
        metric.must_cite_hit = must_cite_hit(artifact, case.must_cite)
        metric.repeat_rate = artifact.repeat_rate
        metric.claim_repeat = artifact.claim_repeat
        metric.text = artifact.text[:20000]

        # 判官与审计曾经并发跑，省 14 秒；但硅基流动的 GLM 对并发很敏感——
        # 两个请求同时打过去会排队到 60 秒以上，然后连接被服务端直接断开。
        # 宁可串行多花十几秒，也不要每 7 个用例坏 6 个。
        judged = await self.judge.assess(case, artifact.text)
        metric.covered = judged.coverage
        metric.coverage = (
            sum(1 for c in judged.coverage if c.covered) / len(judged.coverage)
            if judged.coverage
            else 0.0
        )
        metric.forbidden_hits = judged.forbidden_hits

        assertions = (
            sample_assertions(artifact.chapter_texts, per_chapter=2)
            if (self.audit_semantics and self.rt.retriever is not None)
            else []
        )
        if assertions and self.rt.retriever is not None:
            report = await self.auditor.audit(assertions, self.rt.retriever.full_text)
            metric.semantic_support = report.support_rate
            metric.semantic_total = report.total
            metric.semantic_unsupported = report.unsupported
            metric.semantic_weak = report.weak
            metric.semantic_skipped = report.skipped
            metric.semantic_issues = [
                {
                    "chapter": item.chapter,
                    "sentence": item.sentence[:80],
                    "verdict": item.verdict,
                    "reason": item.reason[:100],
                }
                for item in report.items
                if item.verdict != "supported"
            ][:8]
            if report.error:
                self._log.warning("[%s] 语义审计失败: %s", case.id, report.error)

    # ---------------------------------------------------------------- 报告

    def write_reports(self, results: dict[Arm, list[CaseMetrics]]) -> None:
        summaries = {arm: summarize(arm, ms) for arm, ms in results.items()}
        samples = {arm: _samples(ms, "coverage") for arm, ms in results.items()}
        semantic = {
            arm: _samples(ms, "semantic_support", require="semantic_total")
            for arm, ms in results.items()
        }
        arms = list(results)
        comparisons = []
        for baseline in arms:
            for other in arms:
                if baseline is other:
                    continue
                comparisons.append(
                    paired(samples[baseline], samples[other], label=f"{baseline.value} → {other.value}")
                )
        semantic_comparisons = []
        for baseline in arms:
            for other in arms:
                if baseline is other:
                    continue
                semantic_comparisons.append(
                    paired(
                        semantic[baseline],
                        semantic[other],
                        label=f"{baseline.value} → {other.value}",
                    )
                )

        payload = {
            "run_id": self.run_id,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "repeats": self.repeats,
            "summary": {arm.value: _summary_dict(s) for arm, s in summaries.items()},
            "ci95": {
                arm.value: dict(zip(("low", "high"), ci95(list(per_case_means(s).values()))))
                for arm, s in samples.items()
            },
            "paired": [_paired_dict(c) for c in comparisons],
            "paired_semantic": [_paired_dict(c) for c in semantic_comparisons],
            "cases": {arm.value: [m.to_dict() for m in ms] for arm, ms in results.items()},
        }
        (self.out_dir / "results.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (self.out_dir / "report.md").write_text(
            render_markdown(
                self.run_id,
                summaries,
                results,
                comparisons,
                self.repeats,
                semantic_comparisons=semantic_comparisons,
                judge_label=self.judge_label,
            ),
            encoding="utf-8",
        )
        self._log.info("评测报告已写入 %s", self.out_dir)


def _paired_dict(c: PairedResult) -> dict:
    return {
        "label": c.label,
        "n": c.n,
        "mean_diff": round(c.mean_diff, 4),
        "ci_low": round(c.ci_low, 4),
        "ci_high": round(c.ci_high, 4),
        "significant": c.significant,
    }


def _samples(
    metrics: list[CaseMetrics], attr: str, *, require: str | None = None
) -> dict[str, list[float]]:
    """按主题收集各次重复的指标值，供配对比较使用。

    `require` 指定一个必须大于 0 的字段（例如语义审计只在有样本时才有意义），
    否则该次观测不参与——避免"审计失败记为 0 分"污染统计。
    """
    samples: dict[str, list[float]] = {}
    for m in metrics:
        if m.status == "ERROR":
            continue
        if require is not None and getattr(m, require, 0) <= 0:
            continue
        samples.setdefault(m.case_id, []).append(float(getattr(m, attr)))
    return samples


def _summary_dict(summary: ArmSummary) -> dict:
    data = asdict(summary)
    data["arm"] = summary.arm.value
    return data


def summarize(arm: Arm, metrics: list[CaseMetrics]) -> ArmSummary:
    ok = [m for m in metrics if m.status != "ERROR"]
    n = len(ok) or 1
    audited = [m for m in ok if m.semantic_total > 0]
    return ArmSummary(
        arm=arm,
        cases=len(metrics),
        coverage=sum(m.coverage for m in ok) / n,
        citation_validity=sum(m.citation_validity for m in ok) / n,
        must_cite_hit=sum(m.must_cite_hit for m in ok) / n,
        repeat_rate=sum(m.repeat_rate for m in ok) / n,
        claim_repeat=sum(m.claim_repeat for m in ok) / n,
        semantic_support=(
            sum(m.semantic_support for m in audited) / len(audited) if audited else 0.0
        ),
        semantic_audited=len(audited),
        semantic_unsupported=sum(m.semantic_unsupported for m in ok),
        forbidden_count=sum(len(m.forbidden_hits) for m in ok),
        cost_total=sum(m.cost for m in metrics),
        cost_per_report=sum(m.cost for m in ok) / n,
        elapsed_avg_s=sum(m.elapsed_s for m in ok) / n,
        failures=len(metrics) - len(ok),
    )


def render_markdown(
    run_id: str,
    summaries: dict[Arm, ArmSummary],
    results: dict[Arm, list[CaseMetrics]],
    comparisons: list[PairedResult] | None = None,
    repeats: int = 1,
    semantic_comparisons: list[PairedResult] | None = None,
    judge_label: str = "",
) -> str:
    samples = {arm: _samples(ms, "coverage") for arm, ms in results.items()}
    case_counts = {arm: len(s) for arm, s in samples.items()}
    obs = len(next(iter(results.values()))) if results else 0
    n_cases = max(case_counts.values()) if case_counts else 0

    lines = [
        f"# 评测报告 {run_id}",
        "",
        "生成时间：" + datetime.now().isoformat(timespec="seconds"),
        "",
        f"**{n_cases} 个主题 × {repeats} 次重复 = 每档 {obs} 次观测**",
        "",
        f"判官模型：{judge_label or '未标注'}",
        "",
        "## 汇总",
        "",
        "| 方案 | 要点覆盖率（95% CI） | 语义支持率 | 不支持 | 必引命中 | "
        "论述重复 | 来源复用 | 事实错误 | 单份成本 | 平均耗时 | 失败 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for arm, s in summaries.items():
        means = list(per_case_means(samples[arm]).values())
        low, high = ci95(means) if len(means) >= 2 else (s.coverage, s.coverage)
        sem = f"{s.semantic_support:.1%}" if s.semantic_audited else "—"
        lines.append(
            f"| {ARM_LABELS[arm]} | {s.coverage:.1%} "
            f"[{low:.1%}, {high:.1%}] | {sem} | "
            f"{s.semantic_unsupported} | {s.must_cite_hit:.1%} | "
            f"**{s.claim_repeat:.1%}** | {s.repeat_rate:.1%} | {s.forbidden_count} | "
            f"¥{s.cost_per_report:.4f} | {s.elapsed_avg_s:.0f}s | {s.failures} |"
        )

    if comparisons:
        lines += [
            "",
            "## 配对比较",
            "",
            "同一主题、同一次重复内比较，先对重复取均值再算差值。",
            "**CI 跨 0 即为不显著**——这种情况下不应该写「某档更好」。",
            "",
            "### 要点覆盖率",
            "",
            "| 对比 | 差值（95% CI） | 主题数 | 是否显著 |",
            "|---|---|---|---|",
        ]
        for c in comparisons:
            lines.append(
                f"| {c.label} | {c.describe()} | {c.n} | "
                f"{'✅ 是' if c.significant else '❌ 否'} |"
            )

    if semantic_comparisons:
        lines += [
            "",
            "### 语义支持率",
            "",
            "衡量「正文断言是否被所引原文支持」，这才是 Reviewer 的目标维度。",
            "",
            "| 对比 | 差值（95% CI） | 主题数 | 是否显著 |",
            "|---|---|---|---|",
        ]
        for c in semantic_comparisons:
            lines.append(
                f"| {c.label} | {c.describe()} | {c.n} | "
                f"{'✅ 是' if c.significant else '❌ 否'} |"
            )

    lines += ["", "## 分主题明细", ""]
    for arm, metrics in results.items():
        lines += [
            f"### {ARM_LABELS[arm]}",
            "",
            "| 主题 | 次数 | 覆盖率 | 语义支持 | 引用有效率 | 重复率 | 错误说法 | 成本 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        grouped: dict[str, list[CaseMetrics]] = {}
        for m in metrics:
            grouped.setdefault(m.case_id, []).append(m)
        for case_id, ms in grouped.items():
            cov = sum(x.coverage for x in ms) / len(ms)
            cit = sum(x.citation_validity for x in ms) / len(ms)
            rep = sum(x.repeat_rate for x in ms) / len(ms)
            clm = sum(x.claim_repeat for x in ms) / len(ms)
            sem_vals = [x.semantic_support for x in ms if x.semantic_total > 0]
            sem = sum(sem_vals) / len(sem_vals) if sem_vals else 0.0
            bad = sum(len(x.forbidden_hits) for x in ms)
            cost = sum(x.cost for x in ms)
            lines.append(
                f"| {case_id} | {len(ms)} | {cov:.0%} | {sem:.0%} | {cit:.0%} | "
                f"{clm:.0%} | {rep:.0%} | {bad} | ¥{cost:.4f} |"
            )
        lines.append("")

    lines += ["## 未覆盖要点", "", "比总分更有用：它说明差在哪。", ""]
    for arm, metrics in results.items():
        missing: list[str] = []
        seen: set[tuple[str, str]] = set()
        for m in metrics:
            for c in m.covered:
                if not c.covered and (m.case_id, c.item) not in seen:
                    seen.add((m.case_id, c.item))
                    missing.append(f"- `{m.case_id}` — {c.item}")
        lines.append(f"**{ARM_LABELS[arm]}**：{len(missing)} 条")
        lines += missing[:12] or ["- （无）"]
        lines.append("")

    lines += ["## 语义不支持样例", "", "每条都是「正文这么写了，但原文不支持」。这是最该人工看的部分。", ""]
    for arm, metrics in results.items():
        bad: list[str] = []
        for m in metrics:
            for issue in m.semantic_issues:
                if issue.get("verdict") == "unsupported":
                    bad.append(
                        f"- `{m.case_id}` 第{issue.get('chapter')}章：{issue.get('sentence')}"
                        f"（{issue.get('reason')}）"
                    )
        lines.append(f"**{ARM_LABELS[arm]}**：{len(bad)} 条")
        lines += bad[:8] or ["- （无）"]
        lines.append("")

    lines += [
        "## 口径说明",
        "",
        "- **要点覆盖率**：LLM judge 判定正文是否明确主张该要点；语义等价即可，数字必须对得上。",
        "- **95% CI**：对同一主题的多次重复先取均值，再对主题级均值算置信区间。",
        "- **配对比较**：同一主题内比较两档，抵消「主题本身难度不同」这个最大方差来源。",
        "- **语义支持率**：抽样（每章 2 条）判断「正文断言是否被所引原文支持」，"
        "按 supported=1 / weak=0.5 / unsupported=0 计分。这是 Reviewer 的目标维度。",
        "- **引用有效率**：程序计算，角标能否映射到真实检索来源。当前架构下恒为 100%"
        "（Researcher 已丢弃无法核验的 quote），**没有区分度**，看语义支持率即可。",
        "- **论述重复率**（主指标）：跨章重复的 12 字论述片段占比，衡量「读起来像复读机」。"
        "对片段长度不敏感（6~20 字测得 7~12%）。",
        "- **来源复用率**（参考）：被 ≥2 章引用的来源占比。**它比论述重复率高约 4 倍**，"
        "因为「两章引用同一份资料但讲不同数据」是正常的，不该算进复读。"
        "前两轮去重实验以它为目标，是失败原因之一。",
        "- **事实错误**：judge 判定正文是否**主张**了 forbidden 中的错误说法，否定语境不算。",
        "- judge 与生成使用同一模型，存在自我偏好风险；本轮不含人工校准，结论应保守解读。",
        "",
    ]
    return "\n".join(lines)
