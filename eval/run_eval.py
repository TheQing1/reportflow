"""评测入口（薄壳）。

数据在这里（`eval/topics/`），逻辑在 `backend/app/evaluation/`。
放两处的理由：数据集要能独立 review、独立扩，不该混在后端包里；
而指标计算必须复用后端的 LLM 网关与 span 计量，不能另写一套计费。

用法：

    python eval/run_eval.py --arms A,B,C --limit 2        # 小规模冒烟
    python eval/run_eval.py --arms C                      # 只跑完整流水线
    python eval/run_eval.py --arms A,B,C --run-id v1      # 指定批次名
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.assembler import build_runtime  # noqa: E402
from app.contracts.eval import Arm  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.evaluation import EvalRunner, load_dataset  # noqa: E402


def parse_arms(raw: str) -> list[Arm]:
    arms = []
    for token in raw.replace(" ", "").split(","):
        if not token:
            continue
        try:
            arms.append(Arm(token.upper()))
        except ValueError:
            raise SystemExit(f"未知方案 {token!r}，可选 A/B/C") from None
    if not arms:
        raise SystemExit("至少选择一档方案")
    return arms


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ReportFlow 三档对比评测")
    parser.add_argument("--topics", default=str(REPO_ROOT / "eval" / "topics"))
    parser.add_argument("--arms", default="A,B,C", help="逗号分隔，如 A,B,C")
    parser.add_argument(
        "--repeats", type=int, default=1, help="每个主题每档重复次数（降噪用，建议 2）"
    )
    parser.add_argument("--no-resume", action="store_true", help="忽略已完成的 case 缓存")
    parser.add_argument(
        "--no-audit", action="store_true", help="跳过语义支持度审计（省一次 judge 调用）"
    )
    parser.add_argument("--limit", type=int, help="只跑前 N 个主题（冒烟用）")
    parser.add_argument("--only", help="只跑指定 id 的主题，逗号分隔")
    parser.add_argument("--out", default=str(REPO_ROOT / "eval" / "results"))
    parser.add_argument("--run-id", help="批次名，默认时间戳")
    parser.add_argument("--sections", type=int, help="覆盖数据集里的章节数")
    parser.add_argument(
        "--allow-replay",
        action="store_true",
        help="允许在 LLM_MODE != live 时跑评测（仅用于链路冒烟，结果不可作结论）",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    settings = Settings.load()
    setup_logging(args.log_level)
    if not settings.llm_api_key or settings.llm_api_key.startswith("sk-your"):
        raise SystemExit("LLM_API_KEY 未配置：请先在 .env 填写（评测必须用真实模型）")

    cases = load_dataset(args.topics)
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        cases = [c for c in cases if c.id in wanted]
        if not cases:
            raise SystemExit(f"没有匹配的主题: {sorted(wanted)}")
    if args.sections:
        cases = [
            type(c)(
                id=c.id,
                topic=c.topic,
                sections=args.sections,
                must_cover=c.must_cover,
                must_cite=c.must_cite,
                forbidden=c.forbidden,
                notes=c.notes,
            )
            for c in cases
        ]
    if args.limit:
        cases = cases[: args.limit]

    arms = parse_arms(args.arms)
    print(
        f"主题 {len(cases)} 个 | 方案 {[a.value for a in arms]} | "
        f"每档重复 {args.repeats} 次 | 合计 {len(cases) * len(arms) * args.repeats} 次观测"
    )
    judge = settings.llm_judge_model or f"{settings.llm_model_primary}（与生成同模型）"
    print(f"生成模型 {settings.llm_model_primary} | 判官 {judge} | LLM_MODE={settings.llm_mode}")
    for c in cases:
        print(f"  - {c.id:<18} {c.topic}  ({c.sections} 章, {len(c.must_cover)} 要点)")

    runtime = build_runtime(settings, profile="real")
    runner = EvalRunner(
        runtime=runtime,
        out_dir=args.out,
        run_id=args.run_id,
        allow_replay=args.allow_replay,
        judge_llm=runtime.extras.get("judge_llm"),
    )
    runner.audit_semantics = not args.no_audit
    results = asyncio.run(
        runner.run_all(
            cases, arms, repeats=args.repeats, resume=not args.no_resume
        )
    )

    print(f"\n报告：{runner.out_dir / 'report.md'}")
    for arm, metrics in results.items():
        ok = [m for m in metrics if m.status != "ERROR"]
        if not ok:
            print(f"  {arm.value}: 全部失败")
            continue
        cov = sum(m.coverage for m in ok) / len(ok)
        cost = sum(m.cost for m in metrics)
        print(
            f"  {arm.value}: 覆盖率 {cov:.1%} | 成本 ¥{cost:.4f} | "
            f"{len(ok)}/{len(metrics)} 成功"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
