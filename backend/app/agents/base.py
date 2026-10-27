"""Agent 公共基类：调 LLM 拿 JSON + 解析兜底。

三个刻意的设计：

1. **模型名不由 Agent 决定**。Agent 传 `model=""`，由网关按分级策略填。
   这样"换模型/加降级"不需要改任何 Agent。
2. **解析失败重试一次，且把错误原因回灌给模型**。结构化输出失败在真实项目里
   高频（多一个逗号、包一层 ```json），直接失败会浪费掉已经花掉的 prefill。
3. **解析失败不允许"猜"**。正则兜底只做"从混合文本里抠出 JSON 对象"，
   不做字段修复——字段缺失必须暴露成 HandoffError，让上层看到问题。
4. **工具是可选能力，不是默认行为**。构造时不传 `tools` 就完全是老路径
   （单次补全），传了才走 function calling 循环。这样 T3 不改变默认配置下
   任何一次请求的形状，评测基线因此不受影响。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..contracts.llm import (
    LLMBadResponse,
    LLMClient,
    LLMMessage,
    LLMRequest,
    LLMResponse,
)
from ..contracts.nodes import NodeType
from ..contracts.tools import ToolRegistry
from ..core.logging import get_logger

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_object(text: str) -> dict[str, Any]:
    """从模型输出里取出一个 JSON 对象。

    容忍：前后说明文字、```json 围栏、BOM。
    不容忍：数组顶层（要求对象，避免下游到处判断类型）。
    """
    raw = text.strip().lstrip("\ufeff")
    fenced = _FENCE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()

    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise LLMBadResponse(f"输出里找不到 JSON 对象: {text[:160]!r}")
        raw = raw[start : end + 1]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMBadResponse(f"JSON 解析失败（{exc.msg} @ {exc.pos}）") from exc

    if not isinstance(data, dict):
        raise LLMBadResponse(f"顶层必须是 JSON 对象，实际是 {type(data).__name__}")
    return data


class JsonAgent:
    """所有真实 Agent 的基类：负责"调一次、解析、失败再调一次"。

    可选能力：**工具调用循环**。构造时传入 `tools` 即启用；不传则行为与
    引入 T3 之前逐字节一致（纯文本补全，一次请求）。

    工具循环的形状（就是标准 function calling）：

        user ──> 模型 ──(要调工具)──> 执行工具 ──> 把结果作为 role=tool 回填 ──┐
                  ↑                                                          │
                  └──────────────────── 直到模型不再要工具 ──────────────────┘
                                        └──> 产出最终 JSON

    三条必须守住的边界：

    1. **轮次硬上限**。模型可能反复要求同一个工具（尤其工具一直返回空），
       没有上限就是死循环，而且每轮都在花钱。
    2. **单次工具调用失败不抛出**。`ToolResult.ok=False` 回给模型，
       让它自己决定换词重试还是承认没资料；抛异常会让整章 research 崩掉。
    3. **最终仍要求 JSON**。工具阶段结束后关闭 tools、打开 json_mode，
       否则模型会把"我还要查什么"当成最终答案返回。
    """

    node_type: NodeType

    #: 工具循环的最大轮次。3 轮足够"查→补查→写"，再多基本是在空转
    MAX_TOOL_ROUNDS = 3

    def __init__(
        self,
        llm: LLMClient,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2000,
        tools: ToolRegistry | None = None,
    ) -> None:
        self._llm = llm
        self._temperature = temperature
        self._max_tokens = max_tokens
        #: None = 不启用工具，走原来的单次补全路径（行为完全不变）
        self._tools = tools if (tools is not None and len(tools) > 0) else None
        self._log = get_logger(f"agent.{self.__class__.__name__.lower()}")

    # ---------------------------------------------------------------- 调用

    async def ask_json(
        self,
        ctx_tag: str,
        system: str,
        user: str,
        *,
        timeout_s: float = 60.0,
    ) -> tuple[dict[str, Any], LLMResponse]:
        """要一份 JSON。启用工具时先跑完工具循环，再要 JSON。"""
        if self._tools is not None:
            return await self._ask_json_with_tools(ctx_tag, system, user, timeout_s=timeout_s)
        return await self._ask_json_plain(ctx_tag, system, user, timeout_s=timeout_s)

    async def _ask_json_plain(
        self,
        ctx_tag: str,
        system: str,
        user: str,
        *,
        timeout_s: float,
        messages_override: list[LLMMessage] | None = None,
    ) -> tuple[dict[str, Any], LLMResponse]:
        # 工具循环结束后要用"带工具往返历史"的整段对话收尾，所以允许外部传入
        # 完整 messages；不传时就是最朴素的两条消息（与引入 T3 前一致）。
        messages = (
            list(messages_override)
            if messages_override is not None
            else [
                LLMMessage(role="system", content=system),
                LLMMessage(role="user", content=user),
            ]
        )
        last_error: Exception | None = None

        for attempt in (1, 2):
            req = LLMRequest(
                messages=messages,
                model="",  # 由网关注入，Agent 不关心具体模型
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                json_mode=True,
                timeout_s=timeout_s,
                tag=ctx_tag,
            )
            resp = await self._llm.complete(req)
            try:
                return parse_json_object(resp.text), resp
            except LLMBadResponse as exc:
                last_error = exc
                self._log.warning(
                    "[%s] 第 %d 次结构化输出解析失败: %s | 原文片段: %r",
                    ctx_tag,
                    attempt,
                    exc,
                    resp.text[:200],
                )
                if attempt == 1:
                    # 只把"错在哪"回灌，不重发整段上下文
                    messages = [
                        *messages,
                        LLMMessage(role="assistant", content=resp.text[:4000]),
                        LLMMessage(
                            role="user",
                            content=(
                                f"你上一次的输出无法解析为 JSON（{exc}）。"
                                "请只输出一个合法的 JSON 对象，不要任何解释文字或代码围栏。"
                            ),
                        ),
                    ]

        raise LLMBadResponse(f"两次尝试都未能得到合法 JSON: {last_error}") from last_error

    # ---------------------------------------------------------------- 工具循环

    async def _ask_json_with_tools(
        self,
        ctx_tag: str,
        system: str,
        user: str,
        *,
        timeout_s: float,
    ) -> tuple[dict[str, Any], LLMResponse]:
        """先让模型用工具收集材料，再要最终 JSON。

        工具轮与 JSON 轮分开的原因：工具阶段模型输出的是 tool_calls（没有正文），
        而 json_mode 会强制它输出 JSON——两者同时要求会互相打架。
        所以先放开 tools 跑若干轮，再关掉 tools、打开 json_mode 收尾。
        """
        assert self._tools is not None
        # 每次进入工具循环先清空上一轮的片段，避免跨章串味
        self._tools.begin()
        messages: list[LLMMessage] = [
            LLMMessage(role="system", content=system),
            LLMMessage(role="user", content=user),
        ]
        tool_rounds = 0

        for round_no in range(self.MAX_TOOL_ROUNDS):
            req = LLMRequest(
                messages=messages,
                model="",
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                json_mode=False,
                timeout_s=timeout_s,
                tag=ctx_tag if round_no == 0 else f"{ctx_tag}.r{round_no}",
                tools=self._tools.llm_specs(),
                tool_choice="auto",
            )
            resp = await self._llm.complete(req)
            if not resp.wants_tools:
                # 模型这一轮已经直接给答案了（没有要求工具）。
                # 它是按 json_mode=False 问出来的，但仍可能是合法 JSON；
                # 先试着解析，成了就直接用——**不要再补一次请求**。
                # 补请求的代价是白花一次 prefill，而且会让脚本化/回放场景的
                # 响应序列错位（测试里踩到过）。
                try:
                    return parse_json_object(resp.text), resp
                except LLMBadResponse:
                    # 不是 JSON（模型写了段说明文字）。把它当作"材料收集完毕"，
                    # 走下面的收尾请求，强制要 JSON。
                    messages.append(LLMMessage(role="assistant", content=resp.text))
                    break

            tool_rounds += 1
            # 原样回传 assistant 的 tool_calls，否则模型不知道自己在等什么结果
            messages.append(
                LLMMessage(
                    role="assistant",
                    content=resp.text,
                    tool_calls=resp.tool_calls,
                )
            )
            for call in resp.tool_calls:
                text = await self._run_one_tool(call, ctx_tag)
                messages.append(
                    LLMMessage(
                        role="tool",
                        content=text,
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )

        if tool_rounds >= self.MAX_TOOL_ROUNDS:
            self._log.info(
                "[%s] 工具轮次达上限(%d)，停止继续检索，转入产出",
                ctx_tag,
                self.MAX_TOOL_ROUNDS,
            )

        # 收尾：要最终 JSON。关掉 tools —— 否则模型可能继续"想查"。
        final_messages = [
            *messages,
            LLMMessage(
                role="user",
                content=(
                    "材料收集到此为止。请基于以上所有材料，"
                    "只输出一个合法的 JSON 对象，不要任何解释文字或代码围栏。"
                ),
            ),
        ]
        data, resp = await self._ask_json_plain(
            ctx_tag, system, "", timeout_s=timeout_s, messages_override=final_messages
        )
        return data, resp

    async def _run_one_tool(self, call: Any, ctx_tag: str) -> str:
        """执行一次工具调用，**任何失败都转成文本回给模型**，不向上抛。

        这是 T3 最容易被写错的地方：工具抛异常 → 节点失败 →
        已经花掉的检索与模型调用全部作废，而问题往往只是"这个站点抓不到"。
        """
        assert self._tools is not None
        tool = self._tools.get(call.name)
        if tool is None:
            # 模型可能编造工具名。明确告知，并列出可用工具，让它下一轮改对
            available = ", ".join(self._tools.names()) or "（无）"
            self._log.warning("[%s] 模型请求了不存在的工具: %s", ctx_tag, call.name)
            return f"工具 {call.name!r} 不存在。可用工具：{available}"

        try:
            args = _parse_arguments(call.arguments)
        except ValueError as exc:
            # 参数不是合法 JSON：回灌错误，让它自己改（多一次调用，但能救回来）
            self._log.warning("[%s] 工具参数解析失败 %s: %s", ctx_tag, call.name, exc)
            return f"工具参数不是合法 JSON（{exc}），请重新以 JSON 对象形式提供参数。"

        try:
            result = await tool.run(args)
        except Exception as exc:  # noqa: BLE001 - 工具内部任何异常都不该拖垮节点
            self._log.error(
                "[%s] 工具 %s 执行异常: %s: %s", ctx_tag, call.name, type(exc).__name__, exc
            )
            return f"工具 {call.name} 执行异常：{type(exc).__name__}: {exc}"

        if result.ok:
            # 收集片段供上游做溯源校验：模型引用这些片段时，
            # 校验器需要原文才能判定"quote 是否逐字可查"。
            if result.items:
                self._tools.record_items(result.items)
            self._log.info(
                "[%s] 工具 %s 成功：%d 条，%dms",
                ctx_tag,
                call.name,
                result.total_items or len(result.items),
                result.elapsed_ms,
            )
        else:
            self._log.warning("[%s] 工具 %s 失败：%s", ctx_tag, call.name, result.error)
        return result.to_model_text()

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def timeout_for(node_timeout: float, *, reserve: float = 5.0) -> float:
        """LLM 请求超时要早于节点超时，否则调度器先杀掉节点，
        我们就拿不到网关的重试与降级机会。"""
        return max(5.0, node_timeout - reserve)


def _parse_arguments(raw: str) -> dict[str, Any]:
    """解析模型给的 arguments 字符串。

    模型常见的三种形态：干净 JSON、带 ```json 围栏、空串（无参工具）。
    容忍前两种和空串；其它一律判非法，交给调用方回灌——**不做猜测性修复**，
    猜错参数比报错更危险（可能查了完全无关的东西还看不出来）。
    """
    text = (raw or "").strip()
    if not text:
        return {}
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"找不到 JSON 对象: {text[:80]!r}")
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{exc.msg} @ {exc.pos}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"顶层必须是对象，实际是 {type(data).__name__}")
    return data
