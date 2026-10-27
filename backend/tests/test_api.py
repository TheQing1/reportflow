"""HTTP 接口层测试。

接口是给前端唯一的门，它错了前端必然错，而且错误会以"界面卡住"这种
最难排查的形式出现。所以这里每条路由都测，包括 404 和 SSE。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app
from app.assembler import build_runtime
from app.core.config import Settings


def _runtime(tmp: Path):
    docs = tmp / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        database_url=f"sqlite:///{(tmp / 'api.db').as_posix()}",
        max_concurrency=3,
        node_timeout_s=10,
        max_retries=0,
        retry_backoff_s=0.01,
        poll_interval_s=0.01,
        docs_dir=str(docs),
    )
    rt = build_runtime(settings, profile="mock", announce=False)
    # mock profile 不带检索器（它不调 LLM），但知识库接口需要它：
    # 手动挂一个，才能在测试里验证"空库自动初始化 / 上传后重新索引"。
    from app.rag import LocalCorpusRetriever

    rt.retriever = LocalCorpusRetriever.from_directory(settings.docs_dir)
    return rt


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rfapi-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.rt = _runtime(self.tmp)
        # 必须用上下文管理器：否则每个请求一个临时事件循环，
        # /run 里起的后台任务在请求返回时就没了，任务永远停在 RUNNING。
        self._cm = TestClient(create_app(runtime=self.rt))
        self.client = self._cm.__enter__()
        self.addCleanup(self._cm.__exit__, None, None, None)

    # ------------------------------------------------------------ 基础

    def test_health_reports_mode_and_corpus(self) -> None:
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertIn("llm_mode", body)
        self.assertIn("review_in_pipeline", body)

    def test_unknown_task_is_404(self) -> None:
        self.assertEqual(self.client.get("/api/tasks/nope").status_code, 404)
        self.assertEqual(self.client.post("/api/tasks/nope/run").status_code, 404)
        self.assertEqual(self.client.get("/api/tasks/nope/export").status_code, 404)

    def test_empty_topic_is_rejected(self) -> None:
        r = self.client.post("/api/tasks", json={"topic": "  "})
        self.assertEqual(r.status_code, 400)

    # ------------------------------------------------------------ 任务流程

    def test_create_run_and_read_back(self) -> None:
        created = self.client.post("/api/tasks", json={"topic": "工商业储能", "sections": 2})
        self.assertEqual(created.status_code, 200)
        task_id = created.json()["task_id"]
        self.assertTrue(created.json()["created"])

        detail = self.client.get(f"/api/tasks/{task_id}").json()
        self.assertEqual(detail["sections"], 2)
        self.assertTrue(detail["nodes"])

        listed = self.client.get("/api/tasks").json()
        self.assertIn(task_id, [t["task_id"] for t in listed["tasks"]])

        ran = self.client.post(f"/api/tasks/{task_id}/run").json()
        self.assertTrue(ran["started"])

        # run 是后台执行（前端靠 SSE/轮询看进度），所以这里要等终态
        done = self._wait_done(task_id)
        self.assertEqual(done["status"], "DONE")
        self.assertTrue(all(n["status"] == "DONE" for n in done["nodes"]))

    def _wait_done(self, task_id: str, timeout_s: float = 10.0) -> dict:
        import time

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            body = self.client.get(f"/api/tasks/{task_id}").json()
            if body["status"] in {"DONE", "PARTIAL", "FAILED"}:
                return body
            time.sleep(0.05)
        raise AssertionError(f"任务 {task_id} 在 {timeout_s}s 内未进入终态")

    def test_creating_same_task_id_is_idempotent(self) -> None:
        body = {"topic": "储能", "sections": 1, "task_id": "fixed-01"}
        first = self.client.post("/api/tasks", json=body).json()
        second = self.client.post("/api/tasks", json=body).json()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["task_id"], second["task_id"])

    def test_review_flag_adds_review_nodes(self) -> None:
        body = {"topic": "储能", "sections": 1}
        plain = self.client.post("/api/tasks", json={**body, "task_id": "no-rev"}).json()
        with_rev = self.client.post(
            "/api/tasks", json={**body, "task_id": "with-rev", "review": True}
        ).json()

        def kinds(tid: str) -> list[str]:
            return [n["type"] for n in self.client.get(f"/api/tasks/{tid}").json()["nodes"]]

        self.assertNotIn("review", kinds(plain["task_id"]))
        self.assertIn("review", kinds(with_rev["task_id"]))

    # ------------------------------------------------------------ 产出

    def test_export_returns_markdown(self) -> None:
        tid = self.client.post("/api/tasks", json={"topic": "工商业储能", "sections": 2}).json()[
            "task_id"
        ]
        self.client.post(f"/api/tasks/{tid}/run")
        md = self.client.get(f"/api/tasks/{tid}/export").json()["markdown"]
        self.assertIn("工商业储能", md)
        self.assertIn("## ", md)

    def test_cost_endpoint_attributes_by_node(self) -> None:
        tid = self.client.post("/api/tasks", json={"topic": "工商业储能", "sections": 1}).json()[
            "task_id"
        ]
        self.client.post(f"/api/tasks/{tid}/run")
        self._wait_done(tid)
        body = self.client.get(f"/api/tasks/{tid}/cost").json()
        self.assertEqual(body["task_id"], tid)
        # mock profile 没有 LLM 调用，但结构必须完整：前端按字段渲染
        self.assertIn("by_node", body)
        self.assertIn("cached_calls", body)
        self.assertIn("degraded_calls", body)

    # ------------------------------------------------------------ 知识库

    def test_empty_corpus_is_auto_initialised_from_sample(self) -> None:
        # 建 app 时库是空的 → 应自动用示例语料兜底，检索不再是 0 片段
        body = self.client.get("/api/corpus").json()
        self.assertTrue(body["docs"], "空库应被示例语料初始化")
        self.assertGreater(body["chunks"], 0)

    def test_upload_adds_a_searchable_document(self) -> None:
        before = self.client.get("/api/corpus").json()["chunks"]
        res = self.client.post(
            "/api/corpus/upload",
            files={
                "files": ("我的笔记.md", "# 我的笔记\n\n2026 年内部目标装机 9.9GWh。\n".encode())
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data["uploaded"]), 1)
        self.assertFalse(data["failed"])
        self.assertGreater(data["chunks"], before, "上传后必须重新索引")

        listed = {d["doc_id"] for d in self.client.get("/api/corpus").json()["docs"]}
        self.assertIn(data["uploaded"][0]["doc_id"], listed)

    def test_upload_rejects_unsupported_extension(self) -> None:
        res = self.client.post("/api/corpus/upload", files={"files": ("表.docx", b"binary")})
        data = res.json()
        self.assertEqual(data["uploaded"], [])
        self.assertTrue(data["failed"])

    def test_delete_removes_document(self) -> None:
        up = self.client.post(
            "/api/corpus/upload", files={"files": ("临时.md", "# 临时\n\n待删除。".encode())}
        ).json()
        doc_id = up["uploaded"][0]["doc_id"]
        res = self.client.delete(f"/api/corpus/{doc_id}").json()
        self.assertTrue(res["deleted"])
        listed = {d["doc_id"] for d in self.client.get("/api/corpus").json()["docs"]}
        self.assertNotIn(doc_id, listed)

    def test_cost_of_unknown_task_is_404(self) -> None:
        self.assertEqual(self.client.get("/api/tasks/nope/cost").status_code, 404)

    def test_trace_exposes_facts_for_citation_panel(self) -> None:
        tid = self.client.post("/api/tasks", json={"topic": "工商业储能", "sections": 1}).json()[
            "task_id"
        ]
        self.client.post(f"/api/tasks/{tid}/run")
        trace = self.client.get(f"/api/tasks/{tid}/trace").json()
        self.assertEqual(len(trace["chapters"]), 1)
        chapter = trace["chapters"][0]
        self.assertIn("markdown", chapter)
        self.assertIn("citation_map", chapter)
        self.assertIn("facts", chapter)

    # ------------------------------------------------------------ SSE

    def test_stream_emits_node_and_done_events(self) -> None:
        tid = self.client.post("/api/tasks", json={"topic": "工商业储能", "sections": 1}).json()[
            "task_id"
        ]
        # 必须先启动：任务停在 PENDING 时流不会结束（那是有意的，等待用户触发）
        self.client.post(f"/api/tasks/{tid}/run")

        events: list[str] = []
        with self.client.stream("GET", f"/api/tasks/{tid}/stream") as resp:
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/event-stream", resp.headers["content-type"])
            # 上限防御：万一 done 事件没来，测试要失败而不是挂死
            for _i, line in enumerate(resp.iter_lines()):
                if line.startswith("event: "):
                    events.append(line[7:])
                if "done" in events or _i > 400:
                    break
        self.assertIn("task", events)
        self.assertIn("node", events)
        self.assertIn("done", events)


if __name__ == "__main__":
    unittest.main()
