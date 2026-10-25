"""阶段一命令行入口。

    python -m app.cli run   --topic "..." --sections 5
    python -m app.cli resume --task-id <id>
    python -m app.cli status --task-id <id>
    python -m app.cli ls

刻意先做 CLI 而不是 REST：阶段一验收靠"日志 + 命令行"观察调度行为，
多一层 HTTP 只会增加噪声。REST 层在阶段四随前端一起加。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from .agents import MockAgentConfig
from .assembler import Runtime, build_runtime
from .checkup import check_report, render_check
from .exporter import build_report
from .rag.store import CorpusStore, SAMPLE_DIR
from .contracts.nodes import NodeStatus
from .contracts.task import NodeState, TaskStatus
from .core.config import PROJECT_ROOT, Settings
from .core.logging import setup_logging
from .core.timeutil import iso
from .llm import (
    CacheMode,
    LLMGateway,
    OpenAICompatibleTransport,
    ReplayMiss,
    cache_summary,
    count_files,
)
from .contracts.llm import LLMBadResponse, LLMMessage, LLMRequest, LLMUnavailable
from .rag import LocalCorpusRetriever

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PARTIAL = 2


def _build_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.load()
    overrides: dict[str, object] = {}
    if getattr(args, "database_url", None):
        overrides["database_url"] = args.database_url
    if getattr(args, "log_level", None):
        overrides["log_level"] = args.log_level
    if getattr(args, "concurrency", None):
        overrides["max_concurrency"] = args.concurrency
    if getattr(args, "max_retries", None) is not None:
        overrides["max_retries"] = args.max_retries
    if getattr(args, "max_review_rounds", None) is not None:
        overrides["max_review_rounds"] = args.max_review_rounds
    if getattr(args, "docs", None):
        overrides["docs_dir"] = args.docs
    if getattr(args, "llm_base_url", None):
        overrides["llm_base_url"] = args.llm_base_url
    if getattr(args, "llm_model", None):
        overrides["llm_model_primary"] = args.llm_model
    if getattr(args, "llm_fallback", None) is not None:
        overrides["llm_model_fallback"] = args.llm_fallback
    if getattr(args, "llm_api_key", None):
        overrides["llm_api_key"] = args.llm_api_key
    if getattr(args, "llm_timeout", None):
        overrides["llm_timeout_s"] = args.llm_timeout
    if getattr(args, "llm_mode", None):
        overrides["llm_mode"] = args.llm_mode
    if getattr(args, "llm_cache_dir", None):
        overrides["llm_cache_dir"] = args.llm_cache_dir
    return settings.with_overrides(**overrides)


def _runtime(args: argparse.Namespace) -> Runtime:
    settings = _build_settings(args)
    setup_logging(settings.log_level)
    mock_config = MockAgentConfig(
        fail_nodes=frozenset(getattr(args, "fail_node", None) or ())
    )
    return build_runtime(
        settings,
        profile=getattr(args, "profile", "mock"),
        mock_config=mock_config,
        # 只读命令不要把"当前进程装了哪些 Agent"打进日志，容易和任务自身的 profile 混淆
        announce=getattr(args, "cmd", "") in {"run", "resume"},
    )


def _bar(char: str = "-", width: int = 92) -> str:
    return char * width


def _print_task_header(rt: Runtime, task_id: str, *, show_status: bool = True) -> None:
    task = rt.store.get_task(task_id)
    if task is None:
        print(f"任务不存在: {task_id}")
        return
    print(_bar("="))
    print(f"task_id   : {task.id}")
    print(f"topic     : {task.topic}")
    print(f"sections  : {task.sections}")
    if show_status:
        print(f"status    : {task.status.value}")
    print(f"created_at: {iso(task.created_at)}")
    if task.finished_at:
        print(f"finished  : {iso(task.finished_at)}")
    print(f"database  : {rt.settings.database_url}")
    print(f"profile   : {task.profile}（生成这份结果所用的 Agent 组合）")
    if task.profile == "real":
        print(f"llm_mode  : {_mode_note(rt)}")
    print(f"concurrency: {rt.settings.max_concurrency}")
    print(_bar("="))


def _mode_note(rt: Runtime) -> str:
    """把当前进程的模型模式说清楚——回放跑出来的成本是 0，不说清楚会让人误会。"""
    mode = rt.llm_mode
    if mode == "live":
        return f"live（直连 {rt.settings.llm_model_primary}）"
    recorded = count_files(rt.settings.llm_cache_dir)
    tail = {
        "replay": "不联网，只读缓存",
        "record": "真实调用并落盘",
        "auto": "命中即回放，未命中才联网",
    }.get(mode, "")
    return f"{mode}（{tail}｜缓存 {recorded} 条｜{rt.settings.llm_cache_dir}）"


def _duration_ms(node: NodeState) -> str:
    if not node.started_at or not node.finished_at:
        return "-"
    return str(int((node.finished_at - node.started_at).total_seconds() * 1000))


def _note(node: NodeState) -> str:
    if node.error:
        return node.error.replace("\n", " ")[:60]
    if node.output:
        verdict = node.output.get("verdict")
        if verdict is not None:
            return f"verdict={verdict} round={node.review_round}"
        if node.output.get("insufficient_evidence"):
            return "资料未涉及（素材为空）"
        keys = ",".join(sorted(node.output)[:4])
        return f"output keys: {keys}"
    if node.revision_hint and node.status is NodeStatus.PENDING:
        return "待重写（有审稿意见）"
    return ""


def print_status(rt: Runtime, task_id: str) -> None:
    _print_task_header(rt, task_id)
    nodes = rt.store.list_nodes(task_id)
    if not nodes:
        print("（无节点）")
        return
    print(
        f"{'NODE':<16}{'TYPE':<10}{'STATUS':<10}{'ATT':<5}"
        f"{'DEPS':<30}{'ms':<8}NOTE"
    )
    print(_bar("-"))
    for n in nodes:
        deps = ",".join(n.deps) or "-"
        att = f"{n.attempt}"
        print(
            f"{n.node_id:<16}{n.type.value:<10}{n.status.value:<10}{att:<5}"
            f"{deps:<30}{_duration_ms(n):<8}{_note(n)}"
        )
    print(_bar("-"))
    counts = rt.store.count_nodes_by_status(task_id)
    done = counts.get(NodeStatus.DONE, 0)
    total = len(nodes)
    summary = "  ".join(
        f"{s.value}={counts[s]}" for s in NodeStatus if counts.get(s)
    )
    print(f"合计 {done}/{total} 完成 | {summary or '无'}")


def _print_cost(rt: Runtime, task_id: str) -> None:
    if rt.spans is None:
        return
    rep = rt.spans.summary(task_id)
    if rep.calls == 0:
        return
    print()
    print("成本归因（spans）:")
    print(
        f"  LLM 调用 {rep.calls} 次 | prompt {rep.prompt_tokens} tok | "
        f"completion {rep.completion_tokens} tok | 均延迟 {rep.latency_ms_avg}ms"
    )
    print(
        f"  估算成本 ¥{rep.cost:.4f} | 降级调用 {rep.degraded_calls} 次 | "
        f"失败调用 {rep.failed_calls} 次"
    )
    cached = rt.gateway.cached_calls if rt.gateway else 0
    if cached:
        print(
            f"  其中 {cached}/{rep.calls} 次命中回放缓存（不计费）"
            f" —— 成本为 ¥0 是回放，不是免费推理"
        )
    top = rt.spans.by_node(task_id)[:5]
    if top:
        print("  最贵节点: " + " | ".join(f"{n.node_id} ¥{n.cost:.4f}({n.calls} 次)" for n in top))


def _print_outcome(rt: Runtime, task_id: str) -> int:
    print()
    print_status(rt, task_id)
    _print_cost(rt, task_id)
    task = rt.store.get_task(task_id)
    status = task.status if task else TaskStatus.FAILED
    print()
    if status is TaskStatus.DONE:
        print("结果：DONE —— 全部节点成功")
        return EXIT_OK
    if status is TaskStatus.PARTIAL:
        print("结果：PARTIAL —— 部分章节完成，失败/未通过审查的章节见上表")
        return EXIT_PARTIAL
    print(f"结果：{status.value}")
    return EXIT_FAIL


def _preflight_replay(rt: Runtime) -> bool:
    """回放模式 + 空缓存 = 一定跑不通。提前 0.1 秒说清楚，别让人等一屏 FAILED。"""
    if rt.profile != "real" or rt.llm_mode != "replay":
        return True
    cached = count_files(rt.settings.llm_cache_dir)
    if cached:
        return True
    print(_bar("!"))
    print(f"LLM_MODE=replay，但缓存目录为空：{rt.settings.llm_cache_dir}")
    print("先录制一次（会产生少量真实费用）：")
    print('  LLM_MODE=record python -m app.cli run --profile real --topic "..." --sections 5')
    print(_bar("!"))
    return False


def cmd_run(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    if not _preflight_replay(rt):
        return EXIT_FAIL
    task_id, created = rt.runner.submit(
        topic=args.topic,
        sections=args.sections,
        task_id=args.task_id,
        with_review=True if getattr(args, "review", False) else None,
    )
    graph = rt.store.load_graph(task_id)
    _print_task_header(rt, task_id, show_status=False)
    if created and graph is not None:
        print("DAG（按拓扑序）:")
        print(graph.describe())
    print(_bar("="))
    if args.fail_node:
        print(f"故障注入: {', '.join(args.fail_node)}")
    if args.crash_before:
        print(f"崩溃注入: 在 {args.crash_before} 启动后退出进程")
    print()

    result = asyncio.run(
        rt.runner.run(
            task_id,
            crash_before=args.crash_before,
            reset_failed=args.reset_failed,
            concurrency=args.concurrency,
        )
    )
    return _print_outcome(rt, result.task_id)


def cmd_resume(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    if not _preflight_replay(rt):
        return EXIT_FAIL
    if rt.store.get_task(args.task_id) is None:
        print(f"任务不存在: {args.task_id}")
        return EXIT_FAIL
    if args.fail_node:
        print(f"故障注入: {', '.join(args.fail_node)}")
    print()
    result = asyncio.run(
        rt.runner.run(
            args.task_id,
            crash_before=args.crash_before,
            reset_failed=args.reset_failed,
            concurrency=args.concurrency,
        )
    )
    return _print_outcome(rt, result.task_id)


def cmd_status(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    if rt.store.get_task(args.task_id) is None:
        print(f"任务不存在: {args.task_id}")
        return EXIT_FAIL
    print_status(rt, args.task_id)
    return EXIT_OK


def cmd_ls(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    rows = []
    for task_id in rt.store.list_task_ids(limit=args.limit):
        task = rt.store.get_task(task_id)
        if task is None:
            continue
        counts = rt.store.count_nodes_by_status(task_id)
        rows.append((task, counts.get(NodeStatus.DONE, 0), sum(counts.values())))
    if not rows:
        print("（还没有任务）")
        return EXIT_OK
    print(f"{'TASK_ID':<16}{'STATUS':<10}{'DONE':<10}{'CREATED':<22}TOPIC")
    print(_bar("-"))
    for task, done, total in rows:
        created: datetime = task.created_at
        print(
            f"{task.id:<16}{task.status.value:<10}{f'{done}/{total}':<10}"
            f"{iso(created):<22}{task.topic[:40]}"
        )
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    """把各章正文拼成一份可读的 markdown 初稿。"""
    rt = _runtime(args)
    task = rt.store.get_task(args.task_id)
    if task is None:
        print(f"任务不存在: {args.task_id}")
        return EXIT_FAIL
    nodes = {n.node_id: n for n in rt.store.list_nodes(args.task_id)}
    built = build_report(task, nodes.values())
    chapters = built.chapters
    refs = built.references

    out = Path(args.out) if args.out else (
        PROJECT_ROOT / "data" / "output" / f"{task.id}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(built.markdown, encoding="utf-8")
    print(f"已导出：{out}")
    print(f"章节 {len([c for c in chapters if c.strip()])} 段 | 引用来源 {len(refs)} 条")

    if getattr(args, "check", False):
        return _run_checkup(rt, chapters)
    return EXIT_OK


def _run_checkup(rt: Runtime, chapters: list[str]) -> int:
    """发布前体检：只标记，不改稿。"""
    if rt.gateway is None or rt.retriever is None:
        print()
        print("体检需要 real profile：要调用判官、并读知识库取原文。")
        print("  python -m app.cli export --profile real --task-id <id> --check")
        return EXIT_FAIL
    print()
    report = asyncio.run(
        check_report(
            chapters=[c for c in chapters if c.strip()],
            retriever=rt.retriever,
            llm=rt.gateway,
        )
    )
    if report.error:
        # 必须显式报出来。只打「核查 0 条」会让人以为「没问题」，
        # 而真相是「判官坏了、根本没测」——这两件事天差地别。
        print(f"体检未完成：{report.error}")
        print("  判官返回非法 JSON 通常是输出被 token 上限截断；已按批审计降低概率，")
        print("  仍失败可重跑一次（LLM 输出有随机性）。")
        return EXIT_FAIL
    for line in render_check(report):
        print(line)
    return EXIT_OK if report.clean else EXIT_PARTIAL


def cmd_cost(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    if rt.store.get_task(args.task_id) is None:
        print(f"任务不存在: {args.task_id}")
        return EXIT_FAIL
    _print_cost(rt, args.task_id)
    return EXIT_OK


def cmd_cache(args: argparse.Namespace) -> int:
    """查看/说明录制回放缓存。开发阶段用它确认"这次跑的是真模型还是缓存"。"""
    settings = _build_settings(args)
    info = cache_summary(settings.llm_cache_dir)
    print(_bar("="))
    print(f"LLM 录制缓存  {info['dir']}")
    print(_bar("-"))
    mode = CacheMode.parse(settings.llm_mode)
    print(f"当前 LLM_MODE: {mode.value}")
    print(f"条目        : {info['files']} 条（{info['bytes'] / 1024:.0f} KB）")
    print(f"最近录制    : {info['newest'] or '—'}")
    if info["tags"]:
        print("按环节      : " + " | ".join(f"{k} {v}" for k, v in info["tags"].items()))
    print(_bar("-"))
    if info["files"] == 0:
        print("缓存为空。录制一次（会产生真实费用）：")
        print("  LLM_MODE=record python -m app.cli run --profile real --topic \"...\" --sections 5")
        print("之后把 LLM_MODE 改成 replay 或 auto，同一任务不再联网。")
    else:
        print("开发（零成本、确定性、毫秒级）：LLM_MODE=auto")
        print("断网 / 无 key 复用已有结果   ：LLM_MODE=replay")
        print("评测 / 交付验证（必须真实）   ：LLM_MODE=live")
        print("注意：改 prompt 会让缓存自然失效（prompt 参与哈希），这是刻意的。")
    print(_bar("="))
    return EXIT_OK


def _probe_model(*, base_url: str, api_key: str, model: str, timeout_s: float, max_tokens: int):
    """发一次最小请求。返回 (status, payload)。

    区分三态而不是二值：推理模型的思维链会把小 max_tokens 吃光，
    content 为空并不代表端点有问题——把它当成"失败"会误报。
    """
    transport = OpenAICompatibleTransport(base_url, api_key)
    gateway = LLMGateway(transport, primary_model=model, max_retries=1, backoff_s=0.0)
    req = LLMRequest(
        messages=[LLMMessage(role="user", content="回复两个字：OK")],
        model="",
        max_tokens=max_tokens,
        temperature=0.0,
        timeout_s=timeout_s,
        tag="doctor",
    )

    async def _run():
        try:
            return await gateway.complete(req)
        finally:
            # client 在同一个事件循环里创建和关闭，避免跨 loop 的坑
            await transport.aclose()

    try:
        return "ok", asyncio.run(_run())
    except LLMBadResponse as exc:
        return "empty", exc
    except LLMUnavailable as exc:
        return "fail", exc


def _check_judge(settings: Settings, *, primary_key: str) -> int:
    """判官连通性自检。

    存在的理由很实际：判官配错时，评测不会在启动时报错，
    而是每一例都抛出解析异常、被记成 ERROR——等你看到 240 例全红已经过去两小时。
    """
    judge_model = (settings.llm_judge_model or "").strip()
    if not judge_model:
        print(_bar("-"))
        print("判官模型：未配置（与生成同模型）")
        print("      结论会带自我偏好，建议设 LLM_JUDGE_MODEL。")
        return EXIT_OK

    base = (settings.llm_judge_base_url or settings.llm_base_url).rstrip("/")
    cross = base != settings.llm_base_url.rstrip("/")
    key = settings.llm_judge_api_key or primary_key

    print(_bar("-"))
    print(f"判官模型：{judge_model}")
    print(f"  base_url : {base}" + ("   ← 跨厂商" if cross else "   ← 与生成同厂商"))
    print(f"  api_key  : {'独立配置' if settings.llm_judge_api_key else '沿用生成的 key'}")

    if cross and not settings.llm_judge_api_key:
        print("结果：失败 —— 判官指向了另一家厂商，但没有配 LLM_JUDGE_API_KEY。")
        print(f"      生成的 key 属于 {settings.llm_base_url}，拿到 {base} 上必然 401。")
        return EXIT_FAIL

    status, payload = _probe_model(
        base_url=base,
        api_key=key,
        model=judge_model,
        timeout_s=settings.llm_timeout_s,
        max_tokens=1024,
    )
    if status == "fail":
        print(f"结果：失败 —— {payload}")
        print("      先核对模型 id 拼写（跨厂商时要带组织前缀，如 zai-org/GLM-5.3-Flash）。")
        return EXIT_FAIL
    if status == "empty":
        print(f"结果：端点可用，探针输出为空 —— {payload}")
        print("      推理模型把 1024 token 全用在思维链上了；评测调用已给 4000+。")
    else:
        resp = payload
        print(f"结果：成功 —— {resp.model} ({resp.latency_ms}ms, {resp.usage.total_tokens} tok)")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """配置自检：不跑任务，只验证"填的 key 和知识库能不能用"。

    存在的理由很实际：5 章任务跑到第 3 章才 401，比启动前 3 秒发现要烦得多。
    """
    settings = _build_settings(args)
    setup_logging(settings.log_level)

    key = (settings.llm_api_key or "").strip()
    placeholder = (not key) or key.startswith("sk-your") or "your-key" in key
    masked = (
        "<空>"
        if not key
        else (key[:6] + "…" + key[-4:] if len(key) > 12 else "***")
    )

    print(_bar("="))
    print("配置检查")
    print(_bar("-"))
    print(f"database    : {settings.database_url}")
    print(f"llm_base_url: {settings.llm_base_url}")
    print(f"model       : {settings.llm_model_primary}"
          f"{' → 降级 ' + settings.llm_model_fallback if settings.llm_model_fallback else ''}")
    print(f"llm_api_key : {masked}{'  ← 仍是占位符，需要填真实 key' if placeholder else ''}")
    print(f"llm_timeout : {settings.llm_timeout_s}s（节点超时 {settings.node_timeout_s}s）")
    try:
        mode = CacheMode.parse(settings.llm_mode)
        print(f"llm_mode    : {mode.value}")
    except ValueError as exc:
        print(f"llm_mode    : 配置错误 —— {exc}")
        return EXIT_FAIL
    print(f"cache dir   : {settings.llm_cache_dir}（{count_files(settings.llm_cache_dir)} 条）")
    print(f"concurrency : {settings.max_concurrency}")
    print(f"review 轮数 : {settings.max_review_rounds}")

    docs_dir = Path(settings.docs_dir)
    corpus = LocalCorpusRetriever.from_directory(docs_dir) if docs_dir.is_dir() else None
    size = corpus.corpus_size() if corpus else 0
    print(
        f"knowledge   : {docs_dir}  →  {size} 个片段"
        + ("" if size else "   ← 为空，请先跑 python -m scripts.seed_corpus 或放入 .md/.txt")
    )

    if placeholder:
        cached = count_files(settings.llm_cache_dir)
        print(_bar("-"))
        if mode is CacheMode.REPLAY and cached:
            print(f"结论：LLM_API_KEY 未填写，但 LLM_MODE=replay 且已有 {cached} 条缓存。")
            print("      可以离线跑任务（不会产生任何费用，也不会联网）。")
            print("      注意：这只验证流程通不通，不能评价生成质量。")
            return EXIT_OK
        print("结论：LLM_API_KEY 未填写，real profile 无法联网。")
        print("      填好后重跑本命令即可验证；")
        print("      离线验证用 --profile mock，或先录制缓存再 LLM_MODE=replay。")
        return EXIT_FAIL

    print(_bar("-"))
    print("连通性测试：发送一次最小请求 ……（本命令始终直连，不受 LLM_MODE 影响）")
    status, payload = _probe_model(
        base_url=settings.llm_base_url,
        api_key=key,
        model=settings.llm_model_primary,
        timeout_s=settings.llm_timeout_s,
        max_tokens=512,
    )
    if status == "fail":
        print(f"结果：失败 —— {payload}")
        print("      401/403 多为 key 无效；超时多为 base_url 或网络问题；")
        print("      404 多为模型名写错（检查 LLM_MODEL_PRIMARY）。")
        return EXIT_FAIL
    if status == "empty":
        # 推理模型常见：思维链把 max_tokens 吃光，content 为空。端点其实是好的。
        print(f"结果：端点可用，但探针输出为空 —— {payload}")
        print("      多为推理模型把 512 token 全用在思维链上；正式调用已给 4000+。")
    else:
        resp = payload
        print(
            f"结果：成功 —— model={resp.model} 返回 {resp.text.strip()[:40]!r} "
            f"({resp.latency_ms}ms, {resp.usage.total_tokens} tok)"
        )

    judge_code = _check_judge(settings, primary_key=key)
    if judge_code != EXIT_OK:
        return judge_code
    print(_bar("="))
    print("配置可用。跑一章试试：")
    print('  python -m app.cli run --profile real --topic "2026 年中国工商业储能行业" '
          "--sections 1")
    return EXIT_OK



def cmd_corpus(args: argparse.Namespace) -> int:
    """知识库管理：没有外部资料时用示例语料兜底。

    存在的理由很实际：第一次打开项目时 data/docs 是空的（而且被 gitignore），
    检索必然返回空，生成的正文全是「资料未涉及」——
    用户会以为流水线坏了，其实是没喂资料。
    """
    settings = _build_settings(args)
    setup_logging(settings.log_level)
    store = CorpusStore(settings.docs_dir)

    if args.corpus_cmd == "init":
        source = Path(args.source) if getattr(args, "source", None) else None
        if source is None and args.real:
            source = PROJECT_ROOT / "real-corpus"
        # 切来源时必须是替换：force 只覆盖同名文件，两套语料文件名不同，
        # 直接 force 会把虚构数据和真实数据叠加进同一个库
        replace = bool(getattr(args, "replace", False)) or bool(source)
        copied, total = store.ensure(force=args.force, source=source, replace=replace)
        if copied:
            label = "真实语料（公开来源抓取）" if source else "示例语料（虚构数据）"
            print(f"已从{label}初始化：{settings.docs_dir}（{total} 篇）")
            if not source:
                print("换成真实资料：python -m app.cli corpus init --real --force")
                print("或把自己的 .md/.txt 直接放进该目录。")
        else:
            print(f"知识库非空，未覆盖：{settings.docs_dir}（{total} 篇）")
            print(f"要重置示例库：python -m app.cli corpus init --force")
        return EXIT_OK

    if args.corpus_cmd == "ls":
        docs = store.list_docs()
        if not docs:
            print(f"知识库为空：{settings.docs_dir}")
            print("先跑：python -m app.cli corpus init")
            return EXIT_OK
        print(f"知识库：{settings.docs_dir}（{len(docs)} 篇）")
        print(_bar("-"))
        for d in docs:
            print(f"{d['doc_id']:<28}{d['chars']:>6} 字  {d['title'][:34]}")
        return EXIT_OK

    if args.corpus_cmd == "rm":
        if store.delete(args.doc_id):
            print(f"已删除：{args.doc_id}")
            return EXIT_OK
        print(f"不存在：{args.doc_id}")
        return EXIT_FAIL

    return EXIT_FAIL


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--database-url", help="覆盖 DATABASE_URL（默认 sqlite）")
    common.add_argument("--log-level", help="DEBUG/INFO/WARNING/ERROR")
    common.add_argument("--concurrency", type=int, help="并发上限，覆盖 MAX_CONCURRENCY")
    common.add_argument(
        "--profile",
        choices=["mock", "real"],
        default="mock",
        help="mock=不联网的固定假输出；real=LLM 网关 + 四个真实 Agent",
    )
    common.add_argument("--docs", help="知识库目录（real profile 用），默认 data/docs")
    common.add_argument("--llm-base-url", help="OpenAI 兼容接口地址，覆盖 LLM_BASE_URL")
    common.add_argument("--llm-model", help="主模型名，覆盖 LLM_MODEL_PRIMARY")
    common.add_argument("--llm-fallback", help="降级模型名，覆盖 LLM_MODEL_FALLBACK")
    common.add_argument("--llm-api-key", help="覆盖 LLM_API_KEY")
    common.add_argument("--llm-timeout", type=float, help="单次 LLM 请求超时（秒）")
    common.add_argument(
        "--llm-mode",
        choices=[m.value for m in CacheMode],
        help="live=直连不缓存；record=调用并落盘；replay=只读缓存；auto=命中回放/未命中联网",
    )
    common.add_argument("--llm-cache-dir", help="覆盖 LLM_CACHE_DIR（默认 data/llm_cache）")
    common.add_argument(
        "--max-review-rounds", type=int, help="每章允许被打回的轮数上限"
    )

    parser = argparse.ArgumentParser(
        prog="python -m app.cli", description="ReportFlow 阶段一 CLI（Mock Agent）"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", parents=[common], help="创建并执行一个任务")
    run_p.add_argument("--topic", required=True)
    run_p.add_argument("--sections", type=int, default=5)
    run_p.add_argument("--task-id", help="复用指定 task_id（存在则续跑）")
    run_p.add_argument("--max-retries", type=int, help="覆盖 MAX_RETRIES")
    run_p.add_argument(
        "--fail-node",
        action="append",
        default=[],
        metavar="NODE_ID",
        help="让指定节点恒定失败（可重复），用于验证阻塞传播",
    )
    run_p.add_argument(
        "--crash-before",
        metavar="NODE_ID",
        help="该节点启动后立即退出进程，用于验证断点续跑",
    )
    run_p.add_argument(
        "--reset-failed", action="store_true", help="先把失败/阻塞节点重置再跑"
    )
    run_p.add_argument(
        "--review",
        action="store_true",
        help="把 Reviewer 放进写作循环（默认关闭：评测显示无收益却贵 49%%）",
    )
    run_p.set_defaults(func=cmd_run)

    resume_p = sub.add_parser("resume", parents=[common], help="从断点续跑已有任务")
    resume_p.add_argument("--task-id", required=True)
    resume_p.add_argument("--fail-node", action="append", default=[], metavar="NODE_ID")
    resume_p.add_argument("--crash-before", metavar="NODE_ID")
    resume_p.add_argument("--reset-failed", action="store_true")
    resume_p.set_defaults(func=cmd_resume)

    status_p = sub.add_parser("status", parents=[common], help="查看任务节点状态")
    status_p.add_argument("--task-id", required=True)
    status_p.set_defaults(func=cmd_status)

    export_p = sub.add_parser("export", parents=[common], help="导出 markdown 初稿")
    export_p.add_argument("--task-id", required=True)
    export_p.add_argument("--out", help="输出路径，默认 data/output/<task>.md")
    export_p.add_argument(
        "--check",
        action="store_true",
        help="导出后做一次发布前体检（核查角标与原文是否一致，只标记不改稿）",
    )
    export_p.set_defaults(func=cmd_export)

    cost_p = sub.add_parser("cost", parents=[common], help="查看成本归因")
    cost_p.add_argument("--task-id", required=True)
    cost_p.set_defaults(func=cmd_cost)

    corpus_p = sub.add_parser("corpus", parents=[common], help="知识库：初始化/查看/删除")
    corpus_p.add_argument("corpus_cmd", choices=["init", "ls", "rm"])
    corpus_p.add_argument("doc_id", nargs="?", help="rm 时指定文档 id")
    corpus_p.add_argument("--force", action="store_true", help="init 时覆盖同名文件")
    corpus_p.add_argument(
        "--real", action="store_true", help="init 时用真实语料（real-corpus/）而非示例语料"
    )
    corpus_p.add_argument("--source", help="init 时指定语料来源目录")
    corpus_p.add_argument(
        "--replace", action="store_true", help="init 时先清空知识库（换语料来源时用）"
    )
    corpus_p.set_defaults(func=cmd_corpus)

    cache_p = sub.add_parser(
        "cache", parents=[common], help="查看 LLM 录制回放缓存（开发阶段零成本复用）"
    )
    cache_p.set_defaults(func=cmd_cache)

    doctor_p = sub.add_parser(
        "doctor", parents=[common], help="配置自检：key 是否可用、知识库是否为空"
    )
    doctor_p.set_defaults(func=cmd_doctor)

    ls_p = sub.add_parser("ls", parents=[common], help="列出最近的任务")
    ls_p.add_argument("--limit", type=int, default=10)
    ls_p.set_defaults(func=cmd_ls)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            # 行缓冲：否则 stdout 被管道重定向时会整块缓冲，
            # 导致表格出现在日志之后，验收输出难以阅读
            stream.reconfigure(encoding="utf-8", line_buffering=True)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
