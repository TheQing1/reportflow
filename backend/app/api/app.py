"""HTTP 接口层：REST + SSE。

刻意只做三件事：接请求、把 runtime 的能力开个门、把节点状态推给前端。
**不含任何业务逻辑**——拼装在 `exporter`、体检在 `checkup`、调度在 `orchestrator`，
这里只做协议转换。业务一旦漏进来，就会出现"接口能跑、CLI 不行"的分叉。

SSE 用**轮询数据库**而不是给调度器加回调：
调度器完全不知道 HTTP 的存在，崩溃重启后前端连上就能继续看到进度，
不需要任何"事件重放"逻辑。代价是 300ms 粒度——对进度条完全够用。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from ..assembler import Runtime, build_runtime
from ..checkup import check_report
from ..contracts.nodes import NodeStatus
from ..contracts.task import TaskStatus
from ..core.config import Settings
from ..core.logging import get_logger
from ..core.timeutil import iso
from ..exporter import build_report
from ..rag.store import SAMPLE_DIR, CorpusStore

#: 前端 dev server 的地址（vite 默认 5173）
DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
)

POLL_INTERVAL_S = 0.3


def _duration_ms(node: Any) -> int:
    if not node.started_at or not node.finished_at:
        return 0
    return int((node.finished_at - node.started_at).total_seconds() * 1000)


def _node_payload(node: Any) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "type": node.type.value,
        "status": node.status.value,
        "attempt": node.attempt,
        "deps": list(node.deps),
        "duration_ms": _duration_ms(node),
        "error": node.error,
        "review_round": getattr(node, "review_round", 0),
    }


def _task_payload(task: Any) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "topic": task.topic,
        "sections": task.sections,
        "status": task.status.value,
        "profile": getattr(task, "profile", "real"),
        "created_at": iso(task.created_at),
        "finished_at": iso(task.finished_at) if task.finished_at else None,
    }


def create_app(
    settings: Settings | None = None,
    *,
    runtime: Runtime | None = None,
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS,
) -> FastAPI:
    # 传入 runtime 时必须沿用它的 settings：否则接口的 docs_dir 与运行时的语料目录
    # 可能指向两个地方，上传的文档检索不到，且测试会污染真实知识库。
    settings = settings or (runtime.settings if runtime is not None else Settings.load())
    rt = runtime or build_runtime(settings, profile="real", announce=True)
    log = get_logger("api")

    app = FastAPI(title="ReportFlow", version="0.6.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.rt = rt
    app.state.settings = settings
    # 空库自动用示例语料兜底：第一次打开项目时 data/docs 是空的（且被 gitignore），
    # 不兜底的话检索必然为空，正文全是「资料未涉及」，用户会以为流水线坏了。
    if rt.retriever is not None:
        copied, total = CorpusStore(settings.docs_dir).ensure()
        if copied:
            rt.retriever.reload()
            log.info("知识库为空，已用示例语料初始化 %s（%d 篇）", settings.docs_dir, total)
    #: 正在跑的任务，防止同一个 task 被并发驱动两次
    app.state.running: dict[str, asyncio.Task] = {}

    # ---------------------------------------------------------------- 健康检查

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        retriever = rt.retriever
        # 检索模式要如实报告**实际生效**的那一档：hybrid 配了却没 key 时会降级成
        # bm25，这里必须显示降级后的结果与原因，否则界面上写着 hybrid 而实际在跑
        # 关键词检索，排查时会完全找错方向。
        effective = getattr(retriever, "effective_mode", None)
        return {
            "ok": True,
            "llm_mode": rt.llm_mode,
            "judge_model": (settings.llm_judge_model or settings.llm_model_primary),
            "profile": rt.profile,
            "corpus_chunks": retriever.corpus_size() if retriever else 0,
            "review_in_pipeline": settings.review_in_pipeline,
            "retrieval_mode": settings.retrieval_mode,
            "retrieval_effective": effective or settings.retrieval_mode,
            "retrieval_degraded": str(getattr(retriever, "degraded_reason", "") or ""),
            "embedding_model": settings.embedding_model
            if (effective or settings.retrieval_mode) != "bm25"
            else "",
        }

    # ---------------------------------------------------------------- 任务

    @app.post("/api/tasks")
    async def create_task(body: dict[str, Any]) -> dict[str, Any]:
        topic = str(body.get("topic", "")).strip()
        if not topic:
            raise HTTPException(400, "topic 不能为空")
        sections = int(body.get("sections", 5) or 5)
        task_id = (body.get("task_id") or "").strip() or None
        with_review = body.get("review")
        task_id, created = rt.runner.submit(
            topic=topic,
            sections=max(1, sections),
            task_id=task_id,
            with_review=None if with_review is None else bool(with_review),
        )
        log.info("任务 %s（created=%s）", task_id, created)
        return {"task_id": task_id, "created": created}

    @app.post("/api/tasks/{task_id}/run")
    async def run_task(task_id: str) -> dict[str, Any]:
        task = rt.store.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"任务不存在: {task_id}")
        if task_id in app.state.running and not app.state.running[task_id].done():
            return {"task_id": task_id, "started": False, "reason": "already running"}
        app.state.running[task_id] = asyncio.create_task(rt.runner.run(task_id))
        return {"task_id": task_id, "started": True}

    @app.get("/api/tasks")
    async def list_tasks(limit: int = 20) -> dict[str, Any]:
        rows = []
        for tid in rt.store.list_task_ids(limit=limit):
            task = rt.store.get_task(tid)
            if task is None:
                continue
            counts = rt.store.count_nodes_by_status(tid)
            rows.append(
                {
                    **_task_payload(task),
                    "done": counts.get(NodeStatus.DONE, 0),
                    "total": sum(counts.values()),
                }
            )
        return {"tasks": rows}

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        task = rt.store.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"任务不存在: {task_id}")
        nodes = rt.store.list_nodes(task_id)
        return {
            **_task_payload(task),
            "nodes": [_node_payload(n) for n in nodes],
            "cost": _cost_of(rt, task_id),
        }

    # ---------------------------------------------------------------- SSE

    @app.get("/api/tasks/{task_id}/stream")
    async def stream(task_id: str) -> StreamingResponse:
        if rt.store.get_task(task_id) is None:
            raise HTTPException(404, f"任务不存在: {task_id}")
        return StreamingResponse(
            _event_stream(rt, task_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ---------------------------------------------------------------- 产出

    @app.get("/api/tasks/{task_id}/export")
    async def export(task_id: str) -> dict[str, Any]:
        task = rt.store.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"任务不存在: {task_id}")
        built = build_report(task, rt.store.list_nodes(task_id))
        return {"task_id": task_id, "markdown": built.markdown}

    @app.get("/api/tasks/{task_id}/cost")
    async def cost(task_id: str) -> dict[str, Any]:
        """成本归因：整份报告花了多少、花在哪个节点、有没有降级。

        回放命中的调用记 ¥0，所以这里的数字在 `LLM_MODE=auto` 下会偏小——
        接口同时返回 cached_calls，前端可以据此说明"这次没花钱"。
        """
        task = rt.store.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"任务不存在: {task_id}")
        if rt.spans is None:
            raise HTTPException(400, "spans 未启用")
        rep = rt.spans.summary(task_id)
        return {
            "task_id": task_id,
            "calls": rep.calls,
            "cost": round(rep.cost, 5),
            "prompt_tokens": rep.prompt_tokens,
            "completion_tokens": rep.completion_tokens,
            "latency_ms_avg": rep.latency_ms_avg,
            "degraded_calls": rep.degraded_calls,
            "failed_calls": rep.failed_calls,
            # 从 spans 算，而不是网关内存计数：后者换进程就归零
            "cached_calls": rt.spans.cached_calls(task_id),
            "by_node": [
                {"node_id": n.node_id, "calls": n.calls, "cost": round(n.cost, 5)}
                for n in rt.spans.by_node(task_id)
            ],
        }

    @app.get("/api/tasks/{task_id}/trace")
    async def trace(task_id: str) -> dict[str, Any]:
        """溯源面板的数据：每章用了哪些素材、原文是什么。"""
        task = rt.store.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"任务不存在: {task_id}")
        nodes = {n.node_id: n for n in rt.store.list_nodes(task_id)}
        chapters = []
        for i in range(1, task.sections + 1):
            write = nodes.get(f"write_ch{i}")
            research = nodes.get(f"research_ch{i}")
            out = write.output if write and write.output else {}
            chapters.append(
                {
                    "chapter": i,
                    "markdown": str(out.get("markdown", "")),
                    "citation_map": out.get("citation_map", {}),
                    "gaps": out.get("gaps", []),
                    "facts": (research.output or {}).get("key_facts", [])
                    if research and research.output
                    else [],
                    "dropped_facts": (research.output or {}).get("dropped_facts", 0)
                    if research and research.output
                    else 0,
                }
            )
        return {"task_id": task_id, "chapters": chapters}

    @app.get("/api/tasks/{task_id}/check")
    async def check(task_id: str) -> dict[str, Any]:
        task = rt.store.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"任务不存在: {task_id}")
        if rt.retriever is None or rt.gateway is None:
            raise HTTPException(400, "体检需要 real profile（判官 + 知识库）")
        built = build_report(task, rt.store.list_nodes(task_id))
        report = await check_report(
            chapters=[c for c in built.chapters if c.strip()],
            retriever=rt.retriever,
            llm=rt.gateway,
        )
        return {
            "task_id": task_id,
            "total": report.total,
            "supported": report.supported,
            "weak": report.weak,
            "unsupported": report.unsupported,
            "skipped": report.skipped,
            "support_rate": round(report.support_rate, 4),
            "clean": report.clean,
            "error": report.error,
            "issues": [
                {
                    "chapter": i.chapter,
                    "sentence": i.sentence,
                    "cited": i.cited,
                    "verdict": i.verdict,
                    "reason": i.reason,
                    "likely_docs": i.likely_docs,
                }
                for i in report.issues
            ],
        }

    @app.get("/api/corpus/{doc_id}")
    async def corpus(doc_id: str) -> dict[str, Any]:
        """取一篇文档的全文，供溯源面板对照显示。"""
        if rt.retriever is None:
            raise HTTPException(400, "知识库未加载")
        text = rt.retriever.full_text(doc_id)
        if not text:
            raise HTTPException(404, f"知识库里没有: {doc_id}")
        return {"doc_id": doc_id, "text": text}

    # ---------------------------------------------------------------- 知识库管理

    @app.get("/api/corpus")
    async def list_corpus() -> dict[str, Any]:
        """列出知识库里有哪些文档。"""
        store = CorpusStore(settings.docs_dir)
        return {
            "docs_dir": str(settings.docs_dir),
            "sample_available": SAMPLE_DIR.is_dir(),
            "docs": store.list_docs(),
            "chunks": rt.retriever.corpus_size() if rt.retriever else 0,
        }

    @app.post("/api/corpus/upload")
    async def upload_corpus(files: list[UploadFile]) -> dict[str, Any]:
        """上传 .md/.txt 文档，立即重新索引。

        必须 reload：不然上传完还得重启服务才检索得到，用户会以为上传失败了。
        """
        if not files:
            raise HTTPException(400, "没有收到文件")
        store = CorpusStore(settings.docs_dir)
        uploaded: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        for f in files:
            try:
                data = await f.read()
                doc_id, note = store.save(f.filename or "uploaded.md", data)
                uploaded.append({"doc_id": doc_id, "note": note})
            except ValueError as exc:
                failed.append({"filename": f.filename or "", "reason": str(exc)})
        chunks = rt.retriever.reload() if rt.retriever else 0
        return {"uploaded": uploaded, "failed": failed, "chunks": chunks}

    @app.delete("/api/corpus/{doc_id}")
    async def delete_corpus(doc_id: str) -> dict[str, Any]:
        store = CorpusStore(settings.docs_dir)
        deleted = store.delete(doc_id)
        chunks = rt.retriever.reload() if rt.retriever else 0
        return {"doc_id": doc_id, "deleted": deleted, "chunks": chunks}

    return app


def _cost_of(rt: Runtime, task_id: str) -> dict[str, Any]:
    if rt.spans is None:
        return {}
    rep = rt.spans.summary(task_id)
    return {
        "calls": rep.calls,
        "cost": round(rep.cost, 5),
        "prompt_tokens": rep.prompt_tokens,
        "completion_tokens": rep.completion_tokens,
        "degraded": rep.degraded_calls,
    }


async def _event_stream(rt: Runtime, task_id: str) -> AsyncIterator[bytes]:
    """按 300ms 轮询，只在"有变化"时推事件。"""
    last_node: dict[str, tuple] = {}
    last_task: str | None = None

    def frame(event: str, data: dict[str, Any]) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

    while True:
        task = rt.store.get_task(task_id)
        if task is None:
            yield frame("error", {"message": f"任务不存在: {task_id}"})
            return

        if task.status.value != last_task:
            last_task = task.status.value
            yield frame("task", _task_payload(task))

        for node in rt.store.list_nodes(task_id):
            sig = (node.status.value, node.attempt, _duration_ms(node))
            if last_node.get(node.node_id) != sig:
                last_node[node.node_id] = sig
                yield frame("node", _node_payload(node))

        if task.status is not TaskStatus.RUNNING and task.status is not TaskStatus.PENDING:
            yield frame("done", {"task_id": task_id, "status": task.status.value})
            return
        await asyncio.sleep(POLL_INTERVAL_S)


__all__ = ["create_app"]
