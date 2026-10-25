"""本地假 LLM 服务（OpenAI 兼容），用于离线端到端验证。

为什么需要它：阶段二的核心是"网关 + 四个 Agent"这条链路正确，
而不是"某个模型的文笔"。没有 API Key 时，用这个服务替模型说话，
链路（HTTP 请求 → JSON 解析 → 契约校验 → 状态机 → 计费）全都会被真实执行到，
只有"文本从哪来"被替换。

它按 system prompt 开头的 `[task:xxx]` 标记路由，并按 prompt 里给的原文片段
构造符合契约的 JSON —— 因为 quote 是逐字复制片段的，所以能通过 Researcher 的
「引用必须能在原文找到」校验。

故障注入：
  FAKE_FLAKY_ONCE=1     每个模型第一次请求返回 429（验证网关退避重试）
  FAKE_FAIL_MODEL=<名>  该模型恒定返回 500（验证降级到备用模型）
  FAKE_REVISE_ONCE=1    每章第一次 review 返回 revise（验证打回重写闭环）

用法：
  python -m scripts.fake_openai_server --port 8999
  python -m app.cli run --profile real --llm-base-url http://127.0.0.1:8999/v1 ...
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_lock = threading.Lock()
_seen_models: set[str] = set()
_review_rounds: dict[int, int] = {}

_SNIPPET = re.compile(
    r"\[片段\d+\] doc_id=(\S+)[^\n]*\n原文：([\s\S]*?)(?=\n\n\[片段|\n\n请输出|\Z)"
)
_FACT = re.compile(r"\d+\. claim: (.+)\n\s+source_id: (\S+)")
_PAIR = re.compile(r"\[\d+\] 断言：(.+)\n\s+原文：(.+)")
_CHAPTER = re.compile(r"第\s*(\d+)\s*章")
_SECTIONS = re.compile(r"章节数量：(\d+)")


def _chapter(text: str) -> int:
    m = _CHAPTER.search(text)
    return int(m.group(1)) if m else 0


def _user_text(payload: dict[str, Any]) -> str:
    return "\n".join(
        m.get("content", "")
        for m in payload.get("messages", [])
        if m.get("role") == "user"
    )


def _system_text(payload: dict[str, Any]) -> str:
    return "\n".join(
        m.get("content", "")
        for m in payload.get("messages", [])
        if m.get("role") == "system"
    )


def _plan(user: str) -> dict[str, Any]:
    m = _SECTIONS.search(user)
    sections = int(m.group(1)) if m else 5
    topic = "研究主题"
    tm = re.search(r"研究主题：(.+)", user)
    if tm:
        topic = tm.group(1).strip()
    titles = [
        "市场规模与增长",
        "区域分布特征",
        "竞争格局与集中度",
        "驱动因素与政策",
        "风险与展望",
    ]
    return {
        "chapters": [
            {
                "chapter": i,
                "title": f"{topic}｜{titles[(i - 1) % len(titles)]}",
                "queries": [
                    f"{topic} 装机规模 增速",
                    f"{topic} 区域分布 占比",
                ],
            }
            for i in range(1, sections + 1)
        ]
    }


def _research(user: str) -> dict[str, Any]:
    chapter = _chapter(user)
    snippets = _SNIPPET.findall(user)
    if not snippets:
        return {
            "chapter": chapter,
            "summary": "未检索到可引用材料。",
            "key_facts": [],
            "open_questions": ["知识库无相关内容"],
        }
    # 按章节轮转取片段，避免所有章节引用同一批数据（真实模型也会挑不同素材）
    offset = (chapter - 1) % len(snippets)
    ordered = snippets[offset:] + snippets[:offset]
    facts = []
    for doc_id, raw in ordered[:2]:
        quote = " ".join(raw.split())[:90]
        if len(quote) < 10:
            continue
        facts.append(
            {
                # claim 必须是 quote 的忠实概括：这里取首句，保证数字能对上
                "claim": re.split(r"[，。；]", quote)[0][:40],
                "source_id": doc_id.strip(),
                "page": 10 + chapter,
                "quote": quote,
            }
        )
    return {
        "chapter": chapter,
        "summary": f"第 {chapter} 章覆盖 {len(facts)} 条可溯源数据点。",
        "key_facts": facts,
        "open_questions": [],
    }


def _write(user: str) -> dict[str, Any]:
    chapter = _chapter(user)
    facts = _FACT.findall(user)
    if not facts:
        return {"chapter": chapter, "markdown": "", "gaps": ["无素材"]}
    lines = [f"## 第 {chapter} 章 章节正文", ""]
    for claim, source_id in facts:
        lines.append(f"- {claim.strip()} [^{source_id.strip()}]")
    lines.append("")
    lines.append("上述数据均来自知识库原文，未引入外部信息。")
    return {"chapter": chapter, "markdown": "\n".join(lines), "gaps": []}


def _review(user: str) -> dict[str, Any]:
    chapter = _chapter(user)
    if os.environ.get("FAKE_REVISE_ONCE") == "1":
        with _lock:
            rounds = _review_rounds.get(chapter, 0)
            _review_rounds[chapter] = rounds + 1
        if rounds == 0:
            pairs = _PAIR.findall(user)
            claim = pairs[0][0] if pairs else "本章结论"
            return {
                "chapter": chapter,
                "verdict": "revise",
                "defects": [
                    {
                        "type": "assertion_mismatch",
                        "claim": claim[:60],
                        "reason": "原文只给出区间描述，正文写成了确定值（模拟）",
                    }
                ],
            }
    return {"chapter": chapter, "verdict": "pass", "defects": []}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静点，别刷屏
        return

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        model = str(payload.get("model", ""))
        system = _system_text(payload)
        user = _user_text(payload)

        fail_model = os.environ.get("FAKE_FAIL_MODEL")
        if fail_model and model == fail_model:
            self._send(500, {"error": {"message": f"simulated outage: {model}"}})
            return

        if os.environ.get("FAKE_FLAKY_ONCE") == "1":
            with _lock:
                first = model not in _seen_models
                _seen_models.add(model)
            if first:
                self._send(429, {"error": {"message": "rate limit (simulated)"}})
                return

        if "[task:planner]" in system:
            data = _plan(user)
        elif "[task:researcher]" in system:
            data = _research(user)
        elif "[task:writer]" in system:
            data = _write(user)
        elif "[task:reviewer]" in system:
            data = _review(user)
        else:
            self._send(400, {"error": {"message": "unknown role marker in system prompt"}})
            return

        text = json.dumps(data, ensure_ascii=False)
        self._send(
            200,
            {
                "id": "fake-cmpl",
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
                "usage": {
                    "prompt_tokens": max(1, len(system + user) // 2),
                    "completion_tokens": max(1, len(text) // 2),
                },
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="本地假 OpenAI 服务")
    parser.add_argument("--port", type=int, default=8999)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fake openai server: http://{args.host}:{args.port}/v1  (Ctrl+C 退出)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
