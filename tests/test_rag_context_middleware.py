"""本轮父文档上下文去重；原生 Agent 回归使用内存工具与假模型，不调用外部服务。"""

from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest
from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langsmith import tracing_context
from pydantic import PrivateAttr

from app.agent.rag_context_middleware import RagContextDedupMiddleware, prepare_rag_context
from app.tools.knowledge_tool import format_docs


def parent(key, text=None, **metadata):
    return Document(id=key, page_content=text or f"正文标记_{key}_结束", metadata=metadata)


def result(call_id, documents):
    return ToolMessage(
        id=f"message-{call_id}", name="retrieve_knowledge", tool_call_id=call_id,
        content=format_docs(documents), artifact=documents,
    )


def context_text(messages):
    return "\n".join(message.content for message in messages if isinstance(message, ToolMessage))


def test_multiple_queries_share_one_numbering_and_keep_original_results():
    a, b, c = parent("a"), parent("b"), parent("c")
    messages = [HumanMessage(content="问题"), result("one", [a, b]), result("two", [b, c]), result("three", [a, b])]
    before = [message.model_dump() for message in messages]
    prepared = prepare_rag_context(messages)
    text = context_text(prepared.messages)
    assert prepared.input_parent_occurrences == 6
    assert prepared.unique_parents == 3
    assert prepared.duplicate_occurrences == 3
    assert all(text.count(document.page_content) == 1 for document in [a, b, c])
    assert "【参考资料 3】" in prepared.messages[2].content
    assert "无新增资料" in prepared.messages[3].content
    assert [message.model_dump() for message in messages] == before
    for original, modified in zip(messages[1:], prepared.messages[1:], strict=True):
        assert modified.id == original.id
        assert modified.tool_call_id == original.tool_call_id
        assert modified.artifact == original.artifact


def test_new_human_message_resets_scope_and_keeps_history():
    a = parent("a")
    old = result("old", [a])
    current = result("new", [a])
    messages = [HumanMessage(content="上一轮"), old, AIMessage(content="上一轮回答"), HumanMessage(content="新问题"), current]
    prepared = prepare_rag_context(messages)
    assert prepared.duplicate_occurrences == 0
    assert prepared.messages[1] is old
    assert a.page_content in prepared.messages[-1].content


def test_history_is_deduplicated_per_turn_without_suppressing_new_turn_evidence():
    a = parent("a")
    messages = [
        HumanMessage(content="上一轮"), result("old-one", [a]), result("old-two", [a]),
        AIMessage(content="上一轮回答"), HumanMessage(content="新问题"), result("new", [a]),
    ]
    prepared = prepare_rag_context(messages)
    assert context_text(prepared.messages).count(a.page_content) == 2
    assert prepared.input_parent_occurrences == 1
    assert prepared.duplicate_occurrences == 0
    assert a.page_content in prepared.messages[-1].content


def test_parent_identity_keeps_different_sources_and_updated_content():
    first = parent("a", "相同正文", source="one.md")
    another = parent("b", "相同正文", source="two.md")
    updated = parent("a", "同 ID 的新正文", source="one.md")
    prepared = prepare_rag_context([
        HumanMessage(content="问题"), result("one", [first, another, updated]),
    ])
    assert prepared.unique_parents == 3
    assert "one.md" in prepared.messages[-1].content
    assert "two.md" in prepared.messages[-1].content


def test_legacy_documents_require_identical_content_and_metadata_to_merge():
    a = Document(page_content="相同正文", metadata={"source": "one.md"})
    b = Document(page_content="相同正文", metadata={"source": "two.md"})
    prepared = prepare_rag_context([
        HumanMessage(content="问题"), result("one", [a]), result("two", [a.model_copy(), b]),
    ])
    assert prepared.unique_parents == 2
    assert prepared.duplicate_occurrences == 1


def test_other_tools_errors_empty_results_and_unknown_artifacts_are_preserved():
    a = parent("a")
    messages = [
        HumanMessage(content="问题"),
        ToolMessage(content="实时指标", name="query_cpu_metrics", tool_call_id="mcp", artifact=[a]),
        ToolMessage(content="检索失败", name="retrieve_knowledge", tool_call_id="error", status="error", artifact=[a]),
        ToolMessage(content="未命中", name="retrieve_knowledge", tool_call_id="empty", artifact=[]),
        ToolMessage(content="未知格式", name="retrieve_knowledge", tool_call_id="unknown", artifact={"documents": [a]}),
        result("ok", [a]),
    ]
    prepared = prepare_rag_context(messages)
    assert prepared.input_parent_occurrences == 1
    assert all(prepared.messages[index] is messages[index] for index in range(1, 5))


def test_stateless_views_are_repeatable_and_isolated_between_requests():
    a = parent("a")
    messages = [HumanMessage(content="问题"), result("one", [a]), result("two", [a])]
    first = prepare_rag_context(messages)
    second = prepare_rag_context(messages)
    assert [message.content for message in first.messages] == [message.content for message in second.messages]
    another = prepare_rag_context([HumanMessage(content="其他会话"), result("other", [a])])
    assert another.duplicate_occurrences == 0
    assert a.page_content in another.messages[-1].content


def test_sync_model_middleware_uses_request_override():
    messages = [HumanMessage(content="问题"), result("one", [parent("a")]), result("two", [parent("a")])]
    request = ModelRequest(model=FakeMessagesListChatModel(responses=[AIMessage(content="done")]), messages=messages)
    answer = AIMessage(content="done")
    handler = Mock(return_value=answer)
    assert RagContextDedupMiddleware().wrap_model_call(request, handler) is answer
    modified = handler.call_args.args[0]
    assert context_text(modified.messages).count("正文标记_a_结束") == 1
    assert context_text(request.messages).count("正文标记_a_结束") == 2


@pytest.mark.asyncio
async def test_async_model_middleware_uses_same_deduplication():
    messages = [HumanMessage(content="问题"), result("one", [parent("a")]), result("two", [parent("a")])]
    request = ModelRequest(model=FakeMessagesListChatModel(responses=[AIMessage(content="done")]), messages=messages)
    answer = AIMessage(content="done")
    handler = AsyncMock(return_value=answer)
    assert await RagContextDedupMiddleware().awrap_model_call(request, handler) is answer
    assert context_text(handler.call_args.args[0].messages).count("正文标记_a_结束") == 1


def test_trace_reports_counts_and_parent_references():
    runs = []
    messages = [HumanMessage(content="问题"), result("one", [parent("a")]), result("two", [parent("a")])]
    with tracing_context(enabled="local"):
        prepare_rag_context(messages, langsmith_extra={"on_end": runs.append})
    assert runs[0].name == "rag.deduplicate_context"
    assert runs[0].outputs["duplicate_occurrences"] == 1
    assert runs[0].outputs["references"][0]["parent_id"] == "a"


class RecordingToolModel(FakeMessagesListChatModel):
    _inputs: list = PrivateAttr(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._inputs.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.mark.asyncio
async def test_native_agent_two_rounds_five_tool_calls_keep_full_state_and_dedup_model_input():
    a, b, c, d = [parent(key) for key in "abcd"]
    answers = {"q1": [a, b], "q2": [b, c, a], "q3": [a, c, d], "q4": [c, d], "q5": [a, d]}

    @tool("retrieve_knowledge", response_format="content_and_artifact")
    def knowledge(query: str):
        """Return in-memory parent documents."""
        documents = answers[query]
        return format_docs(documents), documents

    def call(index):
        return {"name": "retrieve_knowledge", "args": {"query": f"q{index}"}, "id": f"call-{index}", "type": "tool_call"}

    model = RecordingToolModel(responses=[
        AIMessage(content="", tool_calls=[call(1), call(2), call(3)]),
        AIMessage(content="", tool_calls=[call(4), call(5)]),
        AIMessage(content="完成"),
    ])
    agent = create_agent(model, tools=[knowledge], middleware=[RagContextDedupMiddleware()], checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "dedup-test"}}
    state = await agent.ainvoke({"messages": [HumanMessage(content="问题")]}, config=config)
    model_text = context_text(model._inputs[-1])
    assert all(model_text.count(document.page_content) == 1 for document in [a, b, c, d])
    raw = [message for message in state["messages"] if isinstance(message, ToolMessage)]
    assert len(raw) == 5
    assert sum(len(message.artifact) for message in raw) == 12
    assert context_text(raw).count(a.page_content) > 1
    checkpoint = await agent.aget_state(config)
    assert sum(len(message.artifact) for message in checkpoint.values["messages"] if isinstance(message, ToolMessage)) == 12
