"""Researcher：检索 → 归纳 → **程序化核验引用**。

这是全项目防幻觉的关键一环。仅靠 prompt 要求"不要编造"是不够的：
模型照样会把"8.4GWh"写成"9.1GWh"，而且看起来毫无破绽。

所以这里做三道检查，任何一道不过就直接丢弃该条 fact：

1. `source_id` 必须真的出现在本次检索结果里（不能引一篇没检索到的文档）；
2. `quote` 必须能在该文档片段里**逐字找到**（去掉空白后做子串匹配）；
3. `quote` 不能太短（< 6 字），否则"储能"这种词也能通过，等于没校验。

被丢弃的条数记进 `dropped_facts`，评测阶段可以直接算"编造率"。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from ..contracts.agent import AgentContext
from ..contracts.handoff import Fact, HandoffError, ResearchPacket
from ..contracts.nodes import NodeType
from ..contracts.retrieval import Retriever, Snippet
from .base import JsonAgent

MIN_QUOTE_CHARS = 6

_SYSTEM = """[task:researcher] 你是资料整理员。任务是从给定原文片段中抽取可引用的事实，供后续撰写使用。

铁律（违反即视为无效输出）：
1. 只能使用下面提供的原文片段，不得引入任何外部知识，不得推算、不得补充。
2. 每条 fact 的 quote 必须从片段中**逐字复制一段连续文字**（可截取，不可改写、
   不可拼接、不可省略号连接）。quote 会与原文做字符串比对，改写一定失败。
3. source_id 必须原样照抄片段标注的 doc_id。
4. claim 是对该原文的概括，必须能被 quote 直接支撑。
5. 片段里没有的信息，写进 open_questions，绝对不要编。

只输出 JSON：
{"chapter":1,"summary":"不超过200字，说明本章素材覆盖了什么",
 "key_facts":[{"claim":"...","source_id":"doc_07","page":23,"quote":"原文逐字片段"}],
 "open_questions":["缺少什么"]}"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


#: 预算外文档的得分折扣（与 scripts/measure_overlap.py 的实测一致：0.5 约 -14pp）
_BUDGET_DISCOUNT = 0.5


def _rerank_by_budget(
    hits: list["Snippet"], budget: set[str], *, limit: int
) -> list["Snippet"]:
    """按文档预算重排：预算内的保持原分，预算外的打折。

    不是硬过滤——硬过滤会让某章在预算文档不够相关时拿到一堆低质片段，
    反而伤覆盖率。这里是"优先但不独占"。
    """
    scored = [(sn.score * (_BUDGET_DISCOUNT if sn.doc_id not in budget else 1.0), i, sn)
              for i, sn in enumerate(hits)]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [sn for _, _, sn in scored[:limit]]


class ResearcherAgent(JsonAgent):
    node_type = NodeType.RESEARCH

    def __init__(
        self,
        llm,
        retriever: Retriever,
        *,
        snippets_per_query: int = 3,
        pool_with_budget: int = 20,
    ) -> None:
        # 3000 而不是默认 2000：每条 fact 都要带上 quote 原文，
        # 素材一多输出就会顶到上限被截断，JSON 变非法（实测报
        # "Unterminated string @ 4244"），整个 research 节点失败、下游全 BLOCKED。
        super().__init__(llm, temperature=0.1, max_tokens=3000)
        self._retriever = retriever
        self._per_query = snippets_per_query
        #: 有文档预算时的召回池大小。必须显著大于 per_query，重排才有腾挪空间。
        self._pool_with_budget = pool_with_budget

    async def execute(self, ctx: AgentContext) -> dict[str, Any]:
        chapter = int(ctx.node.payload.get("chapter", 0))
        queries = self._queries(ctx, chapter)
        budget = ctx.node.payload.get("doc_budget") or None
        snippets = await self._retrieve(
            queries, chapter, [str(d) for d in budget] if budget else None
        )

        if not snippets:
            # 检索无命中不重试、不失败：让 Writer 显式写"资料未涉及"，
            # 比让整章崩掉更符合产品目标（初稿可缺，但不可编）
            self._log.warning("[%s] 检索无命中，返回空素材包", ctx.node.id)
            return ResearchPacket(
                chapter=chapter,
                summary="",
                key_facts=(),
                open_questions=(f"知识库中未检索到与「{queries[0]}」相关的材料",),
            ).to_dict() | {"retrieval": {"queries": queries, "snippets": 0, "dropped": 0}}

        data, _ = await self.ask_json(
            ctx.node.id,
            _SYSTEM,
            self._build_user(chapter, snippets, ctx.hint),
            timeout_s=self.timeout_for(ctx.node.timeout_s),
        )
        packet = self._verify(data, snippets, chapter=chapter)
        self._log.info(
            "[%s] 素材包：命中 %d 片段，保留 %d 条 fact，丢弃 %d 条（引用无法在原文中找到）",
            ctx.node.id,
            len(snippets),
            len(packet.key_facts),
            packet.dropped_facts,
        )
        return {
            **packet.to_dict(),
            "retrieval": {
                "queries": queries,
                "snippets": len(snippets),
                "dropped": packet.dropped_facts,
            },
        }

    # ---------------------------------------------------------------- 检索

    def _queries(self, ctx: AgentContext, chapter: int) -> list[str]:
        queries: list[str] = []
        outline = ctx.upstream_of("planner").get("outline") or []
        if isinstance(outline, list):
            for item in outline:
                if isinstance(item, dict) and int(item.get("chapter", 0)) == chapter:
                    queries.extend(str(q) for q in item.get("queries") or [])
        if not queries:
            payload_queries = ctx.node.payload.get("queries") or []
            queries.extend(str(q) for q in payload_queries)
        if not queries:
            queries.append(str(ctx.node.payload.get("title") or f"第 {chapter} 章"))
        if ctx.hint:
            # 打回重检时，把审稿意见当作额外检索线索（只取一行，避免把整段意见当 query）
            queries.append(ctx.hint.strip().splitlines()[0][:60])
        return [q for q in dict.fromkeys(queries) if q.strip()][:5]

    async def _retrieve(
        self,
        queries: Sequence[str],
        chapter: int,
        budget: Sequence[str] | None = None,
    ) -> list[Snippet]:
        """检索本章素材。

        `budget` 是提交任务时给这一章分配的文档清单（见 rag/budget.py）。
        有它时先**过召回**再按预算重排——这是降低章节重复率的关键：
        候选池只有 top_k 时，池内降权换不出池外的文档，必须先把池子放大。
        """
        seen: set[tuple[str, str]] = set()
        hits: list[Snippet] = []
        # 有预算就把召回池放大，否则重排只能在 pool 内部打转
        pool = max(self._per_query, self._pool_with_budget) if budget else self._per_query
        for query in queries:
            for sn in await self._retriever.search(
                query, top_k=pool, chapters_hint=f"第{chapter}章"
            ):
                key = (sn.doc_id, _norm(sn.text)[:60])
                if key in seen:
                    continue
                seen.add(key)
                hits.append(sn)
        if budget:
            hits = _rerank_by_budget(hits, set(budget), limit=self._per_query * len(queries))
        return hits

    # ---------------------------------------------------------------- 校验

    def _build_user(self, chapter: int, snippets: Iterable[Snippet], hint: str | None) -> str:
        blocks = [
            f"[片段{i}] doc_id={sn.doc_id} 标题={sn.title}"
            + (f" 页码={sn.page}" if sn.page else "")
            + f"\n原文：{sn.text}"
            for i, sn in enumerate(snippets, 1)
        ]
        parts = [
            f"第 {chapter} 章素材如下：\n\n" + "\n\n".join(blocks),
            f"请输出第 {chapter} 章的 JSON。",
        ]
        if hint:
            parts.append(f"上一轮审稿意见（本轮必须修正）：\n{hint}")
        return "\n\n".join(parts)

    def _verify(
        self, data: dict[str, Any], snippets: Sequence[Snippet], *, chapter: int
    ) -> ResearchPacket:
        by_doc: dict[str, list[str]] = {}
        for sn in snippets:
            by_doc.setdefault(sn.doc_id, []).append(_norm(sn.text))

        raw_facts = data.get("key_facts") or []
        if not isinstance(raw_facts, list):
            raise HandoffError("key_facts 必须是数组")

        kept: list[Fact] = []
        dropped = 0
        for item in raw_facts:
            if not isinstance(item, dict):
                dropped += 1
                continue
            source_id = str(item.get("source_id", "")).strip()
            quote = str(item.get("quote", "")).strip()
            if source_id not in by_doc:
                self._log.warning("[ch%d] 丢弃：source_id 不在检索结果中: %s", chapter, source_id)
                dropped += 1
                continue
            norm_quote = _norm(quote)
            if len(norm_quote) < MIN_QUOTE_CHARS:
                self._log.warning("[ch%d] 丢弃：quote 过短无法核验: %r", chapter, quote)
                dropped += 1
                continue
            if not any(norm_quote in doc_text for doc_text in by_doc[source_id]):
                self._log.warning(
                    "[ch%d] 丢弃：quote 在原文中找不到（疑似改写/编造）: %r", chapter, quote[:60]
                )
                dropped += 1
                continue
            try:
                kept.append(Fact.from_dict(item))
            except HandoffError as exc:
                self._log.warning("[ch%d] 丢弃：字段不合法 %s", chapter, exc)
                dropped += 1

        questions = list(
            str(q).strip() for q in (data.get("open_questions") or []) if str(q).strip()
        )
        if dropped and not kept:
            questions.append(f"{dropped} 条引用无法在原文中核验，已全部丢弃")
        packet = ResearchPacket.from_dict(
            {
                "chapter": chapter,
                "summary": data.get("summary", ""),
                "key_facts": [f.to_dict() for f in kept],
                "open_questions": questions,
                "dropped_facts": dropped,
            },
            chapter=chapter,
        )
        return packet
