"""RAG Agent 服务 - 基于 LangGraph 和千问 OpenAI 兼容接口。"""

import asyncio
import re
from typing import Annotated, Any, AsyncGenerator, Dict, Sequence

from langchain.agents import create_agent
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from app.storage.sqlite_saver import sqlite_checkpointer
from app.storage.session_db import session_db_manager, checkpoint_thread_id
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from loguru import logger
from langsmith import traceable
from typing_extensions import TypedDict
from app.config import config
from app.core.llm_factory import llm_factory, model_error_message
from app.tools import get_current_time, retrieve_knowledge, search_web
from app.agent.mcp_client import mcp_registry
from app.agent.rag_context_middleware import RagContextDedupMiddleware
from app.agent.memory_context_middleware import MemoryContextMiddleware
from app.models.memory_runtime import MemoryRunContext, bound_context
from app.agent.compactor import (
    ContextCompactorMiddleware,
    compaction_event_emitter,
    context_compactor,
)

class AgentState(TypedDict):
    """Agent 状态"""
    messages: Annotated[Sequence[BaseMessage], add_messages]


async def trim_messages_middleware(state: AgentState) -> dict[str, Any] | None:
    """
    工业级三级上下文压缩状态更新入口（替代早期粗糙的固定条数截断）。

    策略：
    - Stage 1：最近 2 轮热数据 100% 原始保留；温数据超过配置阈值的 ToolMessage 原位结构化剪枝（OpenHands + SWE-agent 算法）
    - Stage 2：达到配置的 token 高水位时，按需触发单次轻量模型提取 SREIncidentMemory
    - Stage 3：按原子对话轮次整体演进状态，严防拆散 AIMessage(tool_calls) 与 ToolMessage
    """
    return await context_compactor.compact_langgraph_state(state)


def _extract_web_references(content: str) -> list[dict[str, str]]:
    """从 perform_web_search 的输出文本中提取条目 (title, url)"""
    refs = []
    current_title = ""
    for line in content.splitlines():
        line = line.strip()
        m_title = re.match(r"^\[\d+\]\s*(.+?)(?:\s*\(相关度:[^)]+\))?$", line)
        if m_title:
            current_title = m_title.group(1).strip()
            continue
        if line.startswith("来源链接:") and current_title:
            url = line.replace("来源链接:", "").strip()
            if url:
                refs.append({"title": current_title, "url": url})
            current_title = ""
    return refs


def _extract_doc_references(docs_or_content: Any) -> list[dict[str, str]]:
    """提取内部知识库文档的来源文件名与章节路径"""
    refs = []
    if isinstance(docs_or_content, list):
        for doc in docs_or_content:
            meta = getattr(doc, "metadata", {}) if hasattr(doc, "metadata") else {}
            source = meta.get("source") or meta.get("_source") or "内部知识文档"
            filename = source.replace("\\", "/").split("/")[-1]
            headers = [meta[k] for k in ["h1", "h2", "h3"] if k in meta and meta[k]]
            header_str = " > ".join(headers) if headers else meta.get("title", "")
            refs.append({"filename": filename, "header": header_str})
    elif isinstance(docs_or_content, str):
        current_header = ""
        for line in docs_or_content.splitlines():
            line = line.strip()
            if line.startswith("标题:"):
                current_header = line.replace("标题:", "").strip()
            elif line.startswith("来源:"):
                src = line.replace("来源:", "").strip()
                filename = src.replace("\\", "/").split("/")[-1]
                refs.append({"filename": filename, "header": current_header})
                current_header = ""

    unique_refs = []
    seen = set()
    for r in refs:
        k = (r["filename"], r["header"])
        if k not in seen:
            seen.add(k)
            unique_refs.append(r)
    return unique_refs


def _append_citations_if_missing(
    answer: str,
    web_references: list[dict[str, str]],
    doc_references: list[dict[str, str]],
) -> tuple[str, str]:
    """若模型回答末尾缺少结构化引用，自动生成并追加溯源 Markdown 块。
    返回: (完整答案, 仅新增的溯源文本块)
    """
    appended = ""
    if web_references:
        has_web_citation = ("参考公网" in answer or "参考网页" in answer) and "http" in answer
        if not has_web_citation:
            block = "\n\n---\n### 🌐 参考公网资料\n"
            seen_urls = set()
            idx = 1
            for r in web_references:
                url = r.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    block += f"- [{idx}] [{r.get('title', '网页链接')}]({url})\n"
                    idx += 1
            appended += block

    if doc_references:
        has_doc_citation = "参考内部知识库" in answer or "参考知识库" in answer or "### 📚 参考" in answer
        if not has_doc_citation:
            block = "\n\n---\n### 📚 参考内部知识库\n"
            seen_docs = set()
            idx = 1
            for r in doc_references:
                fn = r.get("filename", "")
                if fn and fn not in seen_docs:
                    seen_docs.add(fn)
                    h = f"（{r['header']}）" if r.get("header") else ""
                    block += f"- [{idx}] `{fn}` {h}\n"
                    idx += 1
            appended += block

    return answer + appended, appended


class RagAgentService:
    """RAG Agent 服务 - 使用 LangGraph + 千问 OpenAI 兼容接口"""

    def __init__(self, streaming: bool = True):
        """初始化 RAG Agent 服务

        Args:
            streaming: 是否启用流式输出，默认为 True
        """
        self.model_name = config.qwen_chat_model
        self.streaming = streaming
        self.system_prompt = self._build_system_prompt()


        self.model = llm_factory.create_chat_model(
            model=self.model_name,
            temperature=0.7,
            streaming=streaming,
        )

        # 定义基础本地核心工具（知识检索 + 时间锚定 + 联网搜索）
        self.tools = [retrieve_knowledge, get_current_time, search_web]

        # MCP 客户端（延迟初始化，使用全局管理）
        self.mcp_tools: list = []

        # 创建持久化检查点（基于 SQLite 持久化会话状态）
        self.checkpointer = sqlite_checkpointer

        # Agent 初始化（会在异步方法中完成）
        self.agent = None
        self._tools_version = -1

        logger.info(f"RAG Agent 服务初始化完成 (Qwen-Persistent), model={self.model_name}, streaming={streaming}")

    @traceable(
        name="agent.initialize", run_type="chain",
        process_outputs=lambda result: {"initialized": result is not None},
    )
    async def _initialize_agent(self):
        """按可用工具快照构建 Agent，不等待 MCP；更新工具时复用会话检查点。"""
        version, mcp_tools = mcp_registry.snapshot()
        if self.agent is not None and self._tools_version == version:
            return self.agent

        # 将 MCP 工具添加到实例变量中
        self.mcp_tools = mcp_tools

        # 合并所有工具
        all_tools = self.tools + self.mcp_tools

        self.agent = create_agent(
            self.model,
            tools=all_tools,
            middleware=[ContextCompactorMiddleware(), RagContextDedupMiddleware(), MemoryContextMiddleware()],
            context_schema=MemoryRunContext,
            checkpointer=self.checkpointer,
        )

        self._tools_version = version


        if all_tools:
            tool_names = [tool.name if hasattr(tool, "name") else str(tool) for tool in all_tools]
            logger.info(f"可用工具列表: {', '.join(tool_names)}")
        return self.agent

    def _build_system_prompt(self) -> str:
        """
        构建系统提示词

        注意：LangChain 框架会自动将工具信息传递给 LLM，
        因此系统提示词中无需列举具体的工具列表。

        Returns:
            str: 系统提示词
        """
        from textwrap import dedent

        return dedent("""
            你是一个专业的智能运维 (SRE) 诊断与问答 AI 助手，能够使用多种工具协同帮助用户解决问题。

            工具使用协同准则:
            1. 【内部知识优先】涉及内部运维 SOP、系统架构规范、告警定义、处理流程等内部问题时，优先调用 `retrieve_knowledge` 工具；
            2. 【按需联网补充】当内部知识库未检索到相关内容、或用户明确要求查询开源社区最新动态、外部 CVE 漏洞、第三方库最新版本特性时，调用 `search_web` 进行公网搜索；
            3. 【时间基准锚定】当需要明确当前日期、时间或计算故障时间窗时，调用 `get_current_time` 获取权威当前时间；
            4. 【客观严谨回答】所有结论基于工具返回的真实内容进行归纳分析，严禁无中生有编造事实。

            信息溯源强制规范:
            - 若调用了 `search_web` 联网搜索，必须在回答的最末尾单开一行输出 `### 🌐 参考公网资料`，逐条以 Markdown 列表格式列出所参考网页的完整链接与标题，格式如：
              - [1] [网页标题](完整URL)
            - 若调用了 `retrieve_knowledge` 知识检索，必须在回答的最末尾单开一行输出 `### 📚 参考内部知识库`，列出所引用的内部文档名称与章节，格式如：
              - [1] `文档名称.md` (章节路径)

            回答要求:
            - 保持专业、严谨、沉稳的 SRE 工程师语气
            - 结构清晰，核心结论先行，随后附带技术依据与操作建议
        """).strip()

    async def query(
        self,
        question: str,
        session_id: str,
        user_id: str = "sre_alice",
        *,
        memory_context: MemoryRunContext | None = None,
    ) -> str:
        """
        非流式处理用户问题（一次性返回完整答案）

        Args:
            question: 用户问题
            session_id: 会话ID（作为 thread_id）

        Returns:
            str: 完整答案
        """
        try:
            agent = await self._initialize_agent()

            logger.info(f"[会话 {session_id}] RAG Agent 收到查询（非流式）: {question}")

            # 构建消息列表（系统提示 + 用户问题）
            messages = [
                SystemMessage(content=self.system_prompt + "\n\n" + mcp_registry.availability_message()),
                HumanMessage(content=question)
            ]

            # 构建 Agent 输入
            agent_input = {"messages": messages}

            # 配置 thread_id（用于会话持久化）
            config_dict = {
                "run_name": "agent.chat",
                "tags": ["oncall", "chat"],
                "metadata": {"session_id": session_id, "thread_id": session_id},
                "configurable": {
                    "thread_id": checkpoint_thread_id(user_id, session_id, "rag")
                }
            }

            memory = bound_context(memory_context, user_id, session_id)
            result = await agent.ainvoke(input=agent_input, config=config_dict, context=memory)

            # 提取最终答案
            messages_result = result.get("messages", [])
            if messages_result:
                last_message = messages_result[-1]
                answer = last_message.content if hasattr(last_message, 'content') else str(last_message)

                # 收集工具调用中的网页引用与知识库文档引用
                web_refs: list[dict[str, str]] = []
                doc_refs: list[dict[str, str]] = []
                for m in messages_result:
                    if type(m).__name__ == "ToolMessage":
                        tname = getattr(m, "name", "")
                        if tname == "search_web":
                            web_refs.extend(_extract_web_references(str(getattr(m, "content", ""))))
                        elif tname == "retrieve_knowledge":
                            doc_refs.extend(_extract_doc_references(getattr(m, "artifact", None) or str(getattr(m, "content", ""))))

                answer, _ = _append_citations_if_missing(answer, web_refs, doc_refs)
                logger.info(f"[会话 {session_id}] RAG Agent 查询完成（非流式）")
                return answer

            logger.warning(f"[会话 {session_id}] Agent 返回结果为空")
            return ""

        except Exception as e:
            logger.error(f"[会话 {session_id}] RAG Agent 查询失败（非流式）: {e}")
            raise

    async def query_stream(
        self,
        question: str,
        session_id: str,
        user_id: str = "sre_alice",
        *,
        memory_context: MemoryRunContext | None = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        流式处理用户问题（逐步返回答案片段）

        Args:
            question: 用户问题
            session_id: 会话ID（作为 thread_id）

        Yields:
            Dict[str, Any]: 包含流式数据的字典
                - type: "content" | "tool_call" | "search_results" | "complete" | "error"
                - data: 具体内容
        """
        try:
            agent = await self._initialize_agent()

            logger.info(f"[会话 {session_id}] RAG Agent 收到查询（流式）: {question}")

            # 构建消息列表（系统提示 + 用户问题）
            messages = [
                SystemMessage(content=self.system_prompt + "\n\n" + mcp_registry.availability_message()),
                HumanMessage(content=question)
            ]

            # 构建 Agent 输入
            agent_input = {"messages": messages}

            # 配置 thread_id（用于会话持久化）
            config_dict = {
                "run_name": "agent.chat_stream",
                "tags": ["oncall", "chat", "stream"],
                "metadata": {"session_id": session_id, "thread_id": session_id},
                "configurable": {
                    "thread_id": checkpoint_thread_id(user_id, session_id, "rag")
                }
            }

            # 注册上下文压缩通知队列，用于捕获微观剪枝与宏观归纳事件
            compaction_queue: asyncio.Queue = asyncio.Queue()
            compaction_token = compaction_event_emitter.set(compaction_queue)

            seen_tool_calls: set[str] = set()
            web_references: list[dict[str, str]] = []
            doc_references: list[dict[str, str]] = []
            rag_full_answer = ""

            try:
                memory = bound_context(memory_context, user_id, session_id)
                async for token, metadata in agent.astream(
                    input=agent_input, config=config_dict, stream_mode="messages", context=memory,
                ):
                    # 若触发了上下文压缩或剪枝，优先向下游透传通知事件
                    while not compaction_queue.empty():
                        c_event = compaction_queue.get_nowait()
                        yield {
                            "type": "compaction",
                            "data": c_event
                        }

                    node_name = metadata.get('langgraph_node', 'unknown') if isinstance(metadata, dict) else 'unknown'
                    message_type = type(token).__name__

                    # 1. 拦截模型规划发起的工具调用
                    if message_type in ("AIMessage", "AIMessageChunk"):
                        tool_calls = getattr(token, "tool_calls", None)
                        if tool_calls and isinstance(tool_calls, list):
                            for tc in tool_calls:
                                tc_name = tc.get("name")
                                tc_id = tc.get("id") or tc_name
                                if tc_name and tc_id not in seen_tool_calls:
                                    seen_tool_calls.add(tc_id)
                                    query_arg = (tc.get("args") or {}).get("query", "")
                                    yield {
                                        "type": "tool_call",
                                        "data": {
                                            "tool": tc_name,
                                            "status": "调用中",
                                            "query": query_arg,
                                            "details": f"参数: {query_arg}" if query_arg else "正在准备执行...",
                                        }
                                    }

                        # 文本片段流式输出
                        text_piece = ""
                        content_blocks = getattr(token, 'content_blocks', None)
                        if content_blocks and isinstance(content_blocks, list):
                            for block in content_blocks:
                                if isinstance(block, dict) and block.get('type') == 'text':
                                    text_piece += block.get('text', '')
                        if not text_piece and isinstance(getattr(token, 'content', None), str):
                            text_piece = token.content

                        if text_piece:
                            rag_full_answer += text_piece
                            yield {
                                "type": "content",
                                "data": text_piece,
                                "node": node_name
                            }

                    # 2. 拦截工具执行完成事件 (ToolMessage)
                    elif message_type == "ToolMessage":
                        tool_name = getattr(token, "name", "") or "tool"
                        raw_content = str(getattr(token, "content", ""))
                        artifact = getattr(token, "artifact", None)

                        if tool_name == "search_web":
                            parsed_refs = _extract_web_references(raw_content)
                            web_references.extend(parsed_refs)
                            details = f"检索完成，找到 {len(parsed_refs)} 条公网参考资料" if parsed_refs else "公网检索完成"
                            yield {
                                "type": "tool_call",
                                "data": {
                                    "tool": "search_web",
                                    "status": "已完成",
                                    "details": details,
                                }
                            }
                        elif tool_name == "retrieve_knowledge":
                            parsed_docs = _extract_doc_references(artifact if artifact else raw_content)
                            doc_references.extend(parsed_docs)
                            doc_count = len(parsed_docs)
                            doc_names = [d["filename"] for d in parsed_docs][:3]
                            yield {
                                "type": "tool_call",
                                "data": {
                                    "tool": "retrieve_knowledge",
                                    "status": "已完成",
                                    "details": f"内部知识库检索完成，召回 {doc_count} 篇相关文档",
                                }
                            }
                            yield {
                                "type": "search_results",
                                "data": {
                                    "count": doc_count,
                                    "docs": doc_names,
                                }
                            }
                        elif tool_name == "get_current_time":
                            yield {
                                "type": "tool_call",
                                "data": {
                                    "tool": "get_current_time",
                                    "status": "已完成",
                                    "details": f"当前时间: {raw_content.strip()}",
                                }
                            }
                        else:
                            yield {
                                "type": "tool_call",
                                "data": {
                                    "tool": tool_name,
                                    "status": "已完成",
                                    "details": "执行完成",
                                }
                            }

                # 消费任何尾部遗留的压缩通知
                while not compaction_queue.empty():
                    c_event = compaction_queue.get_nowait()
                    yield {
                        "type": "compaction",
                        "data": c_event
                    }

                # 3. 结果溯源自动兜底补全
                rag_full_answer, appended_citations = _append_citations_if_missing(
                    rag_full_answer, web_references, doc_references
                )
                if appended_citations:
                    yield {
                        "type": "content",
                        "data": appended_citations,
                        "node": "citations",
                    }

            finally:
                compaction_event_emitter.reset(compaction_token)

            logger.info(f"[会话 {session_id}] RAG Agent 查询完成（流式）")
            yield {"type": "complete", "data": {"answer": rag_full_answer}}

        except Exception as e:
            logger.error(f"[会话 {session_id}] RAG Agent 查询失败（流式）: {e}")
            yield {
                "type": "error",
                "data": model_error_message(e)
            }
            raise

    def get_session_history(self, session_id: str, user_id: str = "sre_alice") -> list:
        # The message store is the UI source of truth; never fall back to an unscoped graph.
        return [r.model_dump() for r in session_db_manager.get_messages(session_id, user_id)]

    def clear_session(self, session_id: str, user_id: str = "sre_alice") -> bool:
        return session_db_manager.delete_session(session_id, user_id)

    async def cleanup(self):
        """清理资源"""
        try:
            logger.info("清理 RAG Agent 服务资源...")
            # MCP 客户端由全局管理器统一管理，无需手动清理
            logger.info("RAG Agent 服务资源已清理")
        except Exception as e:
            logger.error(f"清理资源失败: {e}")


# 全局单例 - 启用流式输出
rag_agent_service = RagAgentService(streaming=True)
