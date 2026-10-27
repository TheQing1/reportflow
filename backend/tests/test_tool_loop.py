"""工具调用循环（function calling）与缓存键的测试。

T3 有两个"静默出错"的高风险点，这组用例专门盯它们：

1. **缓存键漏掉 tools**：同一 prompt 配不同工具集却命中同一条缓存，
   回放出别的工具组合下的响应。参数不同、结果相同，排查时极其隐蔽。
2. **工具返回的片段没进证据池**：模型引用网页内容时被溯源校验判成编造，
   好不容易查到的材料被全部丢弃——而且不会报错，只是"素材变少了"。

全部离线：用 fake LLM 模拟 tool_calls，用 fake 工具模拟执行。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from app.agents.base import JsonAgent, _parse_arguments
from app.contracts.llm import (
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    LLMToolSpec,
    LLMUsage,
)
from app.contracts.nodes import NodeType
from app.contracts.tools import ToolRegistry, ToolResult, ToolSpec
from app.llm.cache import prompt_key


def _resp(
    text: str = "",
    *,
    tool_calls: tuple[LLMToolCall, ...] = (),
) -> LLMResponse:
    return LLMResponse(
        text=text,
        model="fake",
        usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
        tool_calls=tool_calls,
    )


class _ScriptedToolLLM:
    """按脚本依次返回响应；记录每次请求，便于断言请求形状。"""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        if not self._responses:
            return _resp('{"ok": true}')
        return self._responses.pop(0)


class _EchoTool:
    """返回固定片段，并记录被调用了几次。"""

    spec = ToolSpec(
        name="kb_search",
        description="检索",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    )

    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._items = items or [
            {"doc_id": "doc_01", "title": "t", "page": None, "text": "原文片段"}
        ]

    async def run(self, arguments):  # noqa: ANN001
        self.calls.append(dict(arguments))
        return ToolResult(ok=True, content="片段内容", items=tuple(self._items))


class _FailingTool:
    spec = ToolSpec(name="boom", description="总是失败")

    async def run(self, arguments):  # noqa: ANN001
        raise RuntimeError("工具内部炸了")


class _Agent(JsonAgent):
    node_type = NodeType.RESEARCH


class ParseArgumentsTest(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(_parse_arguments('{"query":"储能"}'), {"query": "储能"})

    def test_fenced_json(self) -> None:
        raw = '```json\n{"query":"储能"}\n```'
        self.assertEqual(_parse_arguments(raw), {"query": "储能"})

    def test_empty_means_no_args(self) -> None:
        self.assertEqual(_parse_arguments(""), {})
        self.assertEqual(_parse_arguments("   "), {})

    def test_invalid_raises(self) -> None:
        with self.assertRaises(ValueError):
            _parse_arguments("{不是 json")

    def test_array_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _parse_arguments("[1,2,3]")


class CacheKeyToolsTest(unittest.TestCase):
    """T3 最高风险项：缓存键必须区分工具集与工具往返。"""

    def _req(self, **kw: Any) -> LLMRequest:
        from app.contracts.llm import LLMMessage

        base: dict[str, Any] = {
            "messages": [LLMMessage(role="user", content="同一个 prompt")],
            "model": "m",
            "json_mode": True,
        }
        base.update(kw)
        return LLMRequest(**base)

    def test_no_tools_key_is_backwards_compatible(self) -> None:
        """不带工具时键必须与旧版一致，否则历史缓存全部失效、离线开发凭空多花钱。"""
        req = self._req()
        # 旧算法的等价实现
        import hashlib

        canonical = json.dumps(
            {"v": 1, "messages": [["user", "同一个 prompt"]], "json_mode": True},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        old = hashlib.sha256(canonical.encode()).hexdigest()
        self.assertEqual(prompt_key(req), old)

    def test_different_tools_give_different_keys(self) -> None:
        """同一 prompt、不同工具集 → 必须不同键。这是本组的核心断言。"""
        t1 = (LLMToolSpec(name="kb_search", description="d"),)
        t2 = (LLMToolSpec(name="web_search", description="d"),)
        k_none = prompt_key(self._req())
        k1 = prompt_key(self._req(tools=t1))
        k2 = prompt_key(self._req(tools=t2))
        self.assertNotEqual(k_none, k1)
        self.assertNotEqual(k1, k2)

    def test_tool_order_does_not_change_key(self) -> None:
        """工具声明顺序不该影响语义，也不该影响键（否则抖动一下就 miss）。"""
        a = LLMToolSpec(name="a", description="x")
        b = LLMToolSpec(name="b", description="y")
        self.assertEqual(
            prompt_key(self._req(tools=(a, b))), prompt_key(self._req(tools=(b, a)))
        )

    def test_tool_result_message_changes_key(self) -> None:
        """工具返回不同内容 → 键必须不同，否则会回放出错误的工具结果。"""
        from app.contracts.llm import LLMMessage

        r1 = self._req(
            messages=[
                LLMMessage(role="tool", content="结果A", tool_call_id="c1", name="kb_search")
            ]
        )
        r2 = self._req(
            messages=[
                LLMMessage(role="tool", content="结果B", tool_call_id="c1", name="kb_search")
            ]
        )
        self.assertNotEqual(prompt_key(r1), prompt_key(r2))

    def test_tool_call_id_matters(self) -> None:
        from app.contracts.llm import LLMMessage

        base = dict(role="tool", content="同样的结果", name="kb_search")
        a = self._req(messages=[LLMMessage(tool_call_id="call-1", **base)])  # type: ignore[arg-type]
        b = self._req(messages=[LLMMessage(tool_call_id="call-2", **base)])  # type: ignore[arg-type]
        self.assertNotEqual(prompt_key(a), prompt_key(b))


class ToolLoopTest(unittest.TestCase):
    def _agent(self, llm: Any, reg: ToolRegistry | None) -> _Agent:
        return _Agent(llm, tools=reg)

    def test_no_tools_sends_single_request(self) -> None:
        """不传 tools 时必须只发一次请求，且不带 tools 字段。"""
        llm = _ScriptedToolLLM([_resp('{"a": 1}')])
        agent = _Agent(llm)
        data, _ = asyncio.run(agent.ask_json("t", "sys", "user"))
        self.assertEqual(data, {"a": 1})
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(llm.requests[0].tools, ())
        self.assertTrue(llm.requests[0].json_mode)

    def test_tool_round_then_final_json(self) -> None:
        """第一轮要求调工具 → 执行 → 回填 → 第二轮产出 JSON。"""
        tool = _EchoTool()
        llm = _ScriptedToolLLM(
            [
                _resp(tool_calls=(LLMToolCall(id="c1", name="kb_search", arguments='{"query":"储能"}'),)),
                _resp('{"chapter":1,"key_facts":[]}'),
            ]
        )
        agent = self._agent(llm, ToolRegistry([tool]))
        data, _ = asyncio.run(agent.ask_json("t", "sys", "user"))

        self.assertEqual(data["chapter"], 1)
        self.assertEqual(len(tool.calls), 1)
        self.assertEqual(tool.calls[0]["query"], "储能")
        # 第一次请求必须带工具（否则模型根本不知道能查）
        self.assertTrue(llm.requests[0].tools)
        # 工具结果确实被回填
        roles = [m.role for m in llm.requests[-1].messages]
        self.assertIn("tool", roles)

    def test_tool_result_is_fed_back_to_model(self) -> None:
        """工具结果必须以 role=tool + tool_call_id 回填，否则模型丢失上下文。"""
        tool = _EchoTool()
        llm = _ScriptedToolLLM(
            [
                _resp(tool_calls=(LLMToolCall(id="call-9", name="kb_search", arguments="{}"),)),
                _resp('{"ok":1}'),
            ]
        )
        agent = self._agent(llm, ToolRegistry([tool]))
        asyncio.run(agent.ask_json("t", "sys", "user"))

        second = llm.requests[1].messages
        roles = [m.role for m in second]
        self.assertIn("tool", roles)
        tool_msg = next(m for m in second if m.role == "tool")
        self.assertEqual(tool_msg.tool_call_id, "call-9")
        self.assertIn("片段内容", tool_msg.content)

    def test_assistant_tool_calls_are_preserved(self) -> None:
        """assistant 的 tool_calls 必须原样回传，模型才知道自己在等哪个结果。"""
        tool = _EchoTool()
        llm = _ScriptedToolLLM(
            [
                _resp(tool_calls=(LLMToolCall(id="c1", name="kb_search", arguments="{}"),)),
                _resp('{"ok":1}'),
            ]
        )
        agent = self._agent(llm, ToolRegistry([tool]))
        asyncio.run(agent.ask_json("t", "sys", "user"))

        msgs = llm.requests[1].messages
        assistant = next(m for m in msgs if m.role == "assistant")
        self.assertEqual(len(assistant.tool_calls), 1)
        self.assertEqual(assistant.tool_calls[0].name, "kb_search")

    def test_unknown_tool_name_does_not_crash(self) -> None:
        """模型编造工具名：回一句"不存在"，下一轮它有机会改对。"""
        llm = _ScriptedToolLLM(
            [
                _resp(tool_calls=(LLMToolCall(id="c1", name="不存在的工具", arguments="{}"),)),
                _resp('{"ok":1}'),
            ]
        )
        agent = self._agent(llm, ToolRegistry([_EchoTool()]))
        data, _ = asyncio.run(agent.ask_json("t", "sys", "user"))
        self.assertEqual(data["ok"], 1)
        tool_msg = next(m for m in llm.requests[1].messages if m.role == "tool")
        self.assertIn("不存在", tool_msg.content)

    def test_tool_exception_becomes_text_not_raise(self) -> None:
        """工具抛异常必须转成文本回给模型——抛出去会让整章 research 崩掉。"""
        llm = _ScriptedToolLLM(
            [
                _resp(tool_calls=(LLMToolCall(id="c1", name="boom", arguments="{}"),)),
                _resp('{"ok":1}'),
            ]
        )
        agent = self._agent(llm, ToolRegistry([_FailingTool()]))
        data, _ = asyncio.run(agent.ask_json("t", "sys", "user"))
        self.assertEqual(data["ok"], 1)
        tool_msg = next(m for m in llm.requests[1].messages if m.role == "tool")
        self.assertIn("异常", tool_msg.content)
        self.assertIn("工具内部炸了", tool_msg.content)

    def test_bad_arguments_fed_back(self) -> None:
        tool = _EchoTool()
        llm = _ScriptedToolLLM(
            [
                _resp(tool_calls=(LLMToolCall(id="c1", name="kb_search", arguments="{坏 json"),)),
                _resp('{"ok":1}'),
            ]
        )
        agent = self._agent(llm, ToolRegistry([tool]))
        asyncio.run(agent.ask_json("t", "sys", "user"))
        tool_msg = next(m for m in llm.requests[1].messages if m.role == "tool")
        self.assertIn("合法 JSON", tool_msg.content)

    def test_round_limit_enforced(self) -> None:
        """模型无限要求调工具时必须被轮次上限截断，否则死循环且一直花钱。

        注意脚本要备足响应：达到上限后还会再发一次"收尾要 JSON"的请求，
        那一次如果拿到空文本会抛 LLMBadResponse——那不是轮次上限的错，
        而是替身没准备好。
        """
        tool = _EchoTool()
        always_tools = [
            _resp(tool_calls=(LLMToolCall(id=f"c{i}", name="kb_search", arguments="{}"),))
            for i in range(20)
        ]
        llm = _ScriptedToolLLM([*always_tools])
        # 用兜底响应保证收尾阶段一定能拿到 JSON
        llm._responses = always_tools  # noqa: SLF001
        original_complete = llm.complete

        async def complete_with_fallback(req: LLMRequest) -> LLMResponse:  # noqa: ANN001
            # 收尾请求（不带工具、要 JSON）给一个合法 JSON
            if not req.tools and req.json_mode:
                llm.requests.append(req)
                return _resp('{"chapter":1,"key_facts":[]}')
            return await original_complete(req)

        llm.complete = complete_with_fallback  # type: ignore[method-assign]

        agent = self._agent(llm, ToolRegistry([tool]))
        data, _ = asyncio.run(agent.ask_json("t", "sys", "user"))
        self.assertEqual(data["chapter"], 1)
        # 工具调用次数不能超过上限
        self.assertLessEqual(len(tool.calls), JsonAgent.MAX_TOOL_ROUNDS)
        self.assertEqual(
            len(tool.calls), JsonAgent.MAX_TOOL_ROUNDS, "上限内应尽量用满，但绝不超"
        )

    def test_tool_items_are_collected_for_evidence(self) -> None:
        """工具返回的片段必须被登记，否则溯源校验会把合法引用判成编造。"""
        tool = _EchoTool([{"doc_id": "web_x", "title": "t", "text": "网页原文"}])
        llm = _ScriptedToolLLM(
            [
                _resp(tool_calls=(LLMToolCall(id="c1", name="kb_search", arguments="{}"),)),
                _resp('{"ok":1}'),
            ]
        )
        reg = ToolRegistry([tool])
        agent = self._agent(llm, reg)
        asyncio.run(agent.ask_json("t", "sys", "user"))
        items = reg.last_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["doc_id"], "web_x")

    def test_items_reset_between_calls(self) -> None:
        """两次 ask 之间片段必须清空，否则第 2 章会看到第 1 章的材料。"""
        tool = _EchoTool()
        llm = _ScriptedToolLLM(
            [
                _resp(tool_calls=(LLMToolCall(id="c1", name="kb_search", arguments="{}"),)),
                _resp('{"ok":1}'),
                _resp('{"ok":2}'),  # 第二次 ask：模型不再要工具
            ]
        )
        reg = ToolRegistry([tool])
        agent = self._agent(llm, reg)
        asyncio.run(agent.ask_json("t", "sys", "user"))
        self.assertEqual(len(reg.last_items()), 1)
        asyncio.run(agent.ask_json("t", "sys", "user"))
        self.assertEqual(len(reg.last_items()), 0, "第二次执行应已重置片段池")

    def test_model_answering_directly_skips_more_rounds(self) -> None:
        """模型第一轮就不要工具时，不该再白问一次。"""
        llm = _ScriptedToolLLM([_resp('{"ok":1}')])
        agent = self._agent(llm, ToolRegistry([_EchoTool()]))
        asyncio.run(agent.ask_json("t", "sys", "user"))
        # 一轮工具请求 + 一轮收尾 JSON
        self.assertLessEqual(len(llm.requests), 2)


if __name__ == "__main__":
    unittest.main()
