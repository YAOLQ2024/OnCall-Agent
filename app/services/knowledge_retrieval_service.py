"""有界历史改写、条件 HyDE 双路父子召回及千问 API 精排的 LCEL 入口。"""

import hashlib
import json
import re
from threading import Lock

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableBranch, RunnableLambda, RunnableParallel, RunnablePassthrough
from langsmith import traceable
from loguru import logger

from app.config import Settings, config
from app.core.llm_factory import llm_factory
from app.services.parent_document_service import parent_document_service
from app.services.rerank_service import RerankService
from app.services.retrieval_prompts import HYDE_PROMPT, REWRITE_PROMPT


# 保守路由和字面量保护，不宣称识别所有专业词或歧义。
LITERALS = re.compile(r"`[^`]+`|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b|\bv?\d+(?:\.\d+)+\b")
EXACT = re.compile(r"`|\b(?:kubectl|helm|docker|systemctl|curl|ImagePullBackOff|ErrImagePull|CrashLoopBackOff|OOMKilled)\b|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b|\bv?\d+(?:\.\d+)+\b|\b\w+(?:\.\w+){2,}\b", re.I)
OPEN = re.compile(r"为什么|为何|原因|原理|如何排查|怎么排查|如何定位|怎么定位|why\b|troubleshoot", re.I)

# 显式指代与依赖词：出现时必须触发改写以消除指代
DEPENDENT_PATTERN = re.compile(
    r"它|他们|她们|它们|这个|那个|刚才|上面|之前|前者|后者|继续|还有呢|为什么会这样|原因呢|怎么定位|如何解决|为什么这样|怎么这样|为何这样|\b(?:it|that|this|these|those)\b",
    re.I
)

# 独立运维命令模式：自包含 CLI 命令且无代词时支持快速放行
SELF_CONTAINED_COMMAND = re.compile(r"^\s*(?:kubectl|helm|docker|systemctl|curl|git)\b", re.I)


def _needs_rewrite(query: str, chat_history: list) -> tuple[bool, str]:
    """判断是否真正需要改写：返回 (是否需要, 决策原因)。

    层级判定：
    1. 强依赖特征：只要包含指代代词或省略词，必须改写；
    2. 自包含特征：以独立运维命令开头，且无指代代词，快速放行（0ms）；
    3. 极短疑问句（<=10字）：往往省略主语，判定为需要改写；
    4. 独立长句（>25字）：无任何指代词，自包含性强，快速放行。
    """
    if not chat_history:
        return False, "no_history"

    # 显式指代特征优先检查（即使句子带命令或较长，只要有代词也必须改写）
    if DEPENDENT_PATTERN.search(query):
        return True, "dependent_reference"

    # 无指代词的前提下，CLI 命令直接放行（0ms）
    if SELF_CONTAINED_COMMAND.search(query):
        return False, "fast_pass_command"

    # 无指代词的短句（<=10字），主语容易省略（如“怎么解决？”），需要改写
    if len(query.strip()) <= 10:
        return True, "short_ellipsis_query"

    # 无指代词的长句（>25字），信息量完整，自包含性高，快速放行
    if len(query.strip()) > 25:
        return False, "fast_pass_long_self_contained"

    # 默认保守策略：其余中等长度句子仍进行改写以确保召回质量
    return True, "potential_dependency"


def history_before_current_turn(messages):
    """只取最新用户消息之前的文本；工具中间消息不进入改写。"""
    latest = next((i for i in range(len(messages) - 1, -1, -1)
                   if isinstance(messages[i], HumanMessage)), 0)
    return [message for message in messages[:latest]
            if isinstance(message, (HumanMessage, AIMessage))
            and not getattr(message, "tool_calls", None)]


class KnowledgeRetrievalService:
    def __init__(self, settings: Settings = config, parent_service=parent_document_service,
                 model_provider=None, reranker=None):
        self.settings = settings
        self.parent_service = parent_service
        self._model_provider = model_provider
        self._models = {}
        self._model_lock = Lock()
        self.reranker = reranker or RerankService(settings)
        routes = RunnableParallel(
            state=RunnablePassthrough(),
            direct=RunnableLambda(self._direct),
            hyde=RunnableBranch(
                (lambda state: state["use_hyde"], RunnableLambda(self._hyde)),
                RunnableLambda(lambda state: {"documents": [], "status": "skipped"}),
            ),
        ).with_config(run_name="rag.retrieval_routes", max_concurrency=2)
        self.pipeline = (
            RunnableLambda(self._rewrite).with_config(run_name="rag.query_rewrite")
            | RunnableLambda(self._route).with_config(run_name="rag.route")
            | routes
            | RunnableLambda(self._merge).with_config(run_name="rag.merge_candidates")
            | RunnableLambda(self._rank)
            | RunnableLambda(lambda result: result["documents"])
        )

    def _generation_chain(self, role):
        with self._model_lock:
            if role not in self._models:
                self._models[role] = (
                    self._model_provider(role) if self._model_provider else
                    llm_factory.create_chat_model(
                        temperature=0, streaming=False, max_retries=0,
                        request_timeout=(self.settings.rag_hyde_timeout if role == "hyde"
                                         else self.settings.rag_query_rewrite_timeout),
                        # 轻量生成只需要正文，避免思考过程占用有限的输出预算。
                        extra_body={"enable_thinking": False},
                    ).bind(max_tokens=512)
                )
            model = self._models[role]
        prompt = HYDE_PROMPT if role == "hyde" else REWRITE_PROMPT
        return (prompt | model | StrOutputParser()).with_config(
            run_name="rag.hyde_generation" if role == "hyde" else "rag.standalone_generation"
        )

    def _input(self, query, top_k, chat_history):
        query = query.strip()
        if not query or len(query) > 4000:
            raise ValueError("检索问题须为 1 到 4000 个字符")
        if top_k is not None and not 1 <= top_k <= 100:
            raise ValueError("top_k 必须在 1 到 100 之间")
        history, remaining = [], self.settings.rag_history_max_chars
        for message in reversed(chat_history or []):
            if not isinstance(message, (HumanMessage, AIMessage)) or getattr(message, "tool_calls", None):
                continue
            text = message.text
            if not text:
                continue
            # 不截断实体或报错；最近文本过长时不使用更旧的无关历史。
            if len(text) > remaining:
                break
            history.append(message.model_copy(update={"content": text}))
            remaining -= len(text)
            if len(history) >= self.settings.rag_history_max_messages:
                break
        return {"original_query": query, "query": query, "top_k": top_k,
                "chat_history": list(reversed(history)), "rewrite_status": "skipped"}

    def _rewrite(self, state):
        if not self.settings.rag_query_rewrite_enabled or not state["chat_history"]:
            return state

        needs_rewrite, reason = _needs_rewrite(state["query"], state["chat_history"])
        if not needs_rewrite:
            return {**state, "rewrite_status": f"skipped:{reason}"}

        try:
            result = self._generation_chain("rewrite").invoke(state).strip()
            protected = LITERALS.findall(state["query"])
            if not result or len(result) > 4000 or any(value not in result for value in protected):
                raise ValueError("改写为空、过长或丢失受保护字面量")
            return {**state, "query": result, "rewrite_status": "rewritten"}
        except Exception as error:
            logger.warning("历史改写失败，使用原查询：{}", type(error).__name__)
            return {**state, "rewrite_status": "fallback"}

    def _route(self, state):
        availability = self.reranker.availability()
        original, query = state["original_query"], state["query"]
        if not self.settings.rag_hyde_enabled:
            reason = "hyde_disabled"
        elif state["rewrite_status"] == "fallback":
            reason = "rewrite_fallback"
        elif re.search(r"它|这个服务|那个服务|刚才那个|\b(?:it|that service)\b", query, re.I):
            reason = "unresolved_reference"
        elif EXACT.search(original) or EXACT.search(query):
            reason = "precise_query"
        elif not OPEN.search(query):
            reason = "not_open_ended"
        elif not availability["available"]:
            reason = "reranker_unavailable"
        else:
            reason = "open_ended_dual_route"
        return {**state, "use_hyde": reason == "open_ended_dual_route",
                "route_reason": reason, "reranker": availability}

    def _direct(self, state):
        # 存储故障不能伪装为无文档或 HyDE 的可选失败。
        return self.parent_service.retrieve(state["query"], state["top_k"])

    def _hyde(self, state):
        try:
            hypothesis = self._generation_chain("hyde").invoke(state).strip()
            if not hypothesis or len(hypothesis) > 2000:
                raise ValueError("HyDE 文本为空或过长")
            return {"documents": self.parent_service.retrieve(hypothesis, state["top_k"]),
                    "status": "retrieved"}
        except Exception as error:
            logger.warning("HyDE 分支失败，保留直接召回：{}", type(error).__name__)
            return {"documents": [], "status": f"failed:{type(error).__name__}"}

    @staticmethod
    def _merge(routes):
        documents, seen = [], set()
        for document in [*routes["direct"], *routes["hyde"]["documents"]]:
            identity = document.id or document.metadata.get("parent_id") or json.dumps(
                document.metadata, sort_keys=True, ensure_ascii=False, default=str
            )
            key = (str(identity), hashlib.sha256(document.page_content.encode()).hexdigest())
            if key not in seen:
                seen.add(key)
                documents.append(document)
        return {**routes, "candidates": documents}

    def _rank(self, routes):
        return self.reranker.rerank(
            routes["candidates"], routes["state"]["query"],
            routes["direct"], routes["state"]["reranker"],
        )

    @traceable(name="rag.search", run_type="chain", process_inputs=lambda inputs: {
        "query": inputs.get("query"), "top_k": inputs.get("top_k"),
        "history_message_count": len(inputs.get("chat_history") or []),
    })
    def retrieve(self, query, top_k=None, *, chat_history=None) -> list[Document]:
        return self.pipeline.invoke(self._input(query, top_k, chat_history))

    @traceable(name="rag.search", run_type="chain", process_inputs=lambda inputs: {
        "query": inputs.get("query"), "top_k": inputs.get("top_k"),
        "history_message_count": len(inputs.get("chat_history") or []),
    })
    async def aretrieve(self, query, top_k=None, *, chat_history=None) -> list[Document]:
        # LCEL 同步模型/存储节点在线程池执行，不阻塞主事件循环。
        return await self.pipeline.ainvoke(self._input(query, top_k, chat_history))


knowledge_retrieval_service = KnowledgeRetrievalService()
