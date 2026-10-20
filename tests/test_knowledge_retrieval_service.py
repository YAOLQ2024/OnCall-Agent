"""真实 LCEL、原生 ToolRuntime 和 CrossEncoderReranker；不联网或下载模型。"""

import threading
from unittest.mock import Mock

import pytest
from langchain.agents import create_agent
from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import FakeListChatModel, FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langsmith import tracing_context

from app.config import Settings
from app.services.knowledge_retrieval_service import KnowledgeRetrievalService, history_before_current_turn
from app.services.rerank_service import RerankService


def doc(key):
    return Document(id=key, page_content=f"真实正文 {key}", metadata={"source": f"{key}.md"})


def make_service(*, scores=None, rewrite="payment-service 为什么 Pending", hyde="假设检查方向", **values):
    # _env_file=None 仍会读取进程环境；测试场景不能受部署开关影响。
    defaults = {"rag_hyde_enabled": False, "rag_rerank_enabled": False,
                "rag_query_rewrite_enabled": True}
    settings = Settings(_env_file=None, **(defaults | values))
    parent = Mock()
    parent.retrieve.return_value = [doc("a")]
    encoder = Mock()
    encoder.score.side_effect = lambda pairs: [scores.get(text, 0) for _, text in pairs] if scores else [1] * len(pairs)
    rerank = RerankService(settings, model_provider=lambda: encoder)
    provider = Mock(side_effect=lambda role: FakeListChatModel(responses=[rewrite if role == "rewrite" else hyde]))
    return KnowledgeRetrievalService(settings, parent, provider, rerank), parent, provider, encoder


def history():
    return [HumanMessage(content="payment-service 的 Pod Pending"), AIMessage(content="先查看事件")]


def test_no_history_uses_original_without_generation():
    service, parent, provider, _ = make_service()
    assert service.retrieve(" 为什么 Pod Pending ") == [doc("a")]
    parent.retrieve.assert_called_once_with("为什么 Pod Pending", None)
    provider.assert_not_called()


def test_history_rewrite_is_one_standalone_query_and_reuses_model():
    service, parent, provider, _ = make_service()
    service.retrieve("它为什么这样", 5, chat_history=history())
    parent.retrieve.assert_called_once_with("payment-service 为什么 Pending", 5)
    service.retrieve("它为什么这样", chat_history=history())
    provider.assert_called_once_with("rewrite")


def test_self_contained_query_with_history_fast_passes_without_rewrite():
    service, parent, provider, _ = make_service()
    # 含有历史，但问题是以 CLI 独立命令开头且无代词，直接 0ms 放行
    service.retrieve("kubectl get pods -n kube-system", chat_history=history())
    parent.retrieve.assert_called_once_with("kubectl get pods -n kube-system", None)
    provider.assert_not_called()

    # 含有历史，长句且完全无指代词，自包含性强，快速放行
    parent.reset_mock()
    long_query = "请详细说明 Kubernetes 生产集群中 Ingress 控制器和 NodePort Service 的本质区别与适用场景"
    service.retrieve(long_query, chat_history=history())
    parent.retrieve.assert_called_once_with(long_query, None)
    provider.assert_not_called()


def test_history_filters_intermediate_messages_and_is_bounded_without_mutation():
    service, _, _, _ = make_service(rag_history_max_messages=2, rag_history_max_chars=100)
    messages = [SystemMessage(content="系统"), *history(),
                ToolMessage(content="工具正文", tool_call_id="old"),
                AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "x"}])]
    before = [message.model_dump() for message in messages]
    assert service._input("问题", None, messages)["chat_history"] == history()
    assert [message.model_dump() for message in messages] == before
    assert service._input("另一个请求", None, [])["chat_history"] == []
    assert service._input("问题", None, [HumanMessage(content="x" * 101)])["chat_history"] == []


@pytest.mark.parametrize("rewrite", ["", "丢失代码的改写", "x" * 4001])
def test_invalid_rewrite_falls_back_and_disables_hyde(rewrite):
    service, parent, provider, _ = make_service(rewrite=rewrite, rag_hyde_enabled=True, rag_rerank_enabled=True)
    service.retrieve("它为什么报 `ERR_PAYMENT_9021`", chat_history=history())
    parent.retrieve.assert_called_once_with("它为什么报 `ERR_PAYMENT_9021`", None)
    provider.assert_called_once_with("rewrite")


def test_rewrite_failure_keeps_direct_query():
    service, parent, provider, _ = make_service()

    def fail(value):
        raise TimeoutError("fake")

    provider.side_effect = lambda role: RunnableLambda(fail)
    service.retrieve("它为什么这样", chat_history=history())
    parent.retrieve.assert_called_once_with("它为什么这样", None)


@pytest.mark.parametrize("query", ["为什么报 ERR_PAYMENT_9021", "如何排查 kubectl get pods", "为什么 ImagePullBackOff", "为什么 v1.2.3 失败", "它为什么这样"])
def test_precise_or_unresolved_queries_skip_hyde(query):
    service, parent, provider, _ = make_service(rag_hyde_enabled=True, rag_rerank_enabled=True)
    service.retrieve(query)
    parent.retrieve.assert_called_once_with(query, None)
    provider.assert_not_called()


def test_dual_routes_run_concurrently_merge_parents_and_rank_with_standalone_query():
    a, b = doc("a"), doc("b")
    service, parent, provider, encoder = make_service(
        rag_hyde_enabled=True, rag_rerank_enabled=True, scores={a.page_content: 0.1, b.page_content: 0.9}
    )
    barrier = threading.Barrier(2)

    def retrieve(query, top_k):
        barrier.wait(timeout=5)
        return [a, b] if query == "假设检查方向" else [a]

    parent.retrieve.side_effect = retrieve
    result = service.retrieve("为什么集群级联故障")
    assert result == [b, a]
    provider.assert_called_once_with("hyde")
    encoder.score.assert_called_once_with([
        ("为什么集群级联故障", a.page_content), ("为什么集群级联故障", b.page_content)
    ])
    assert all("假设" not in document.page_content for document in result)
    assert a.metadata == {"source": "a.md"}


def test_hyde_generation_failure_preserves_direct_documents():
    service, parent, provider, _ = make_service(rag_hyde_enabled=True, rag_rerank_enabled=True)
    provider.side_effect = RuntimeError("offline")
    assert service.retrieve("为什么集群故障") == [doc("a")]
    parent.retrieve.assert_called_once_with("为什么集群故障", None)


def test_reranker_load_failure_disables_hyde_and_retries_with_cooldown():
    service, parent, provider, _ = make_service(rag_hyde_enabled=True, rag_rerank_enabled=True)
    loader = Mock(side_effect=ImportError("missing dependency"))
    service.reranker._provider = loader
    assert service.retrieve("为什么集群故障") == [doc("a")]
    service.retrieve("为什么集群故障")
    loader.assert_called_once()
    provider.assert_not_called()


def test_reranker_inference_failure_discards_hyde_only_candidates():
    service, parent, _, encoder = make_service(rag_hyde_enabled=True, rag_rerank_enabled=True)
    parent.retrieve.side_effect = lambda query, k: [doc("b")] if query == "假设检查方向" else [doc("a")]
    encoder.score.side_effect = RuntimeError("inference failed")
    assert service.retrieve("为什么集群故障") == [doc("a")]


def test_threshold_can_return_empty_and_does_not_restore_filtered_documents():
    service, _, _, _ = make_service(rag_rerank_enabled=True, rag_rerank_score_threshold=2)
    assert service.retrieve("Pod Pending") == []


def test_direct_storage_failure_is_not_hidden():
    service, parent, _, _ = make_service()
    parent.retrieve.side_effect = RuntimeError("Milvus offline")
    with pytest.raises(RuntimeError, match="Milvus offline"):
        service.retrieve("Pod Pending")


@pytest.mark.parametrize("query,top_k", [(" ", None), ("x" * 4001, None), ("Pending", 0), ("Pending", 101)])
def test_invalid_request_does_not_call_dependencies(query, top_k):
    service, parent, provider, _ = make_service()
    with pytest.raises(ValueError):
        service.retrieve(query, top_k)
    parent.retrieve.assert_not_called()
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_async_pipeline_offloads_sync_storage_and_matches_sync_result():
    service, parent, _, _ = make_service()
    main_thread = threading.get_ident()
    threads = []

    def retrieve(query, k):
        threads.append(threading.get_ident())
        return [doc("a")]

    parent.retrieve.side_effect = retrieve
    result = await service.aretrieve("Pod Pending")
    assert result == [doc("a")]
    assert threads and main_thread not in threads


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.asyncio
async def test_native_agent_injects_history_and_hides_runtime_from_model_schema(monkeypatch):
    from app.tools import knowledge_tool

    retrieve = Mock(return_value=[doc("a")])
    monkeypatch.setattr(knowledge_tool.knowledge_retrieval_service, "retrieve", retrieve)
    assert set(knowledge_tool.retrieve_knowledge.tool_call_schema.model_json_schema()["properties"]) == {"query"}
    call = {"name": "retrieve_knowledge", "args": {"query": "它为什么这样"}, "id": "rag-call", "type": "tool_call"}
    model = ToolModel(responses=[AIMessage(content="", tool_calls=[call]), AIMessage(content="完成")])
    agent = create_agent(model, tools=[knowledge_tool.retrieve_knowledge])
    messages = [SystemMessage(content="系统"), *history(), HumanMessage(content="它为什么这样")]
    state = await agent.ainvoke({"messages": messages})
    retrieve.assert_called_once()
    assert retrieve.call_args.args == ("它为什么这样",)
    assert [message.content for message in retrieve.call_args.kwargs["chat_history"]] == [message.content for message in history()]
    tool_message = next(message for message in state["messages"] if isinstance(message, ToolMessage))
    assert tool_message.artifact == [doc("a")]
    assert [message.content for message in history_before_current_turn(state["messages"])] == [message.content for message in history()]


def test_rerank_trace_has_once_computed_scores_top_n_and_unchanged_documents():
    a, b, c = doc("a"), doc("b"), doc("c")
    service, _, _, encoder = make_service(
        rag_rerank_enabled=True, rag_rerank_top_n=2,
        scores={a.page_content: -1, b.page_content: 2, c.page_content: 1},
    )
    availability = service.reranker.availability()
    runs = []
    with tracing_context(enabled="local"):
        result = service.reranker.rerank(
            [a, b, c], "独立查询", [a], availability,
            langsmith_extra={"on_end": runs.append},
        )
    assert result["documents"] == [b, c]
    assert runs[0].outputs["before"] == ["a", "b", "c"]
    assert runs[0].outputs["after"] == ["b", "c"]
    assert [item["score"] for item in runs[0].outputs["scores"]] == [-1, 2, 1]
    encoder.score.assert_called_once()
    assert all(document.metadata == {"source": f"{document.id}.md"} for document in [a, b, c])


@pytest.mark.parametrize("scores", [[float("nan")], [], [1, 2]])
def test_invalid_scores_fall_back_without_adding_hyde_evidence(scores):
    service, _, _, encoder = make_service(rag_rerank_enabled=True)
    availability = service.reranker.availability()
    encoder.score.side_effect = None
    encoder.score.return_value = scores
    result = service.reranker.rerank([doc("b")], "问题", [doc("a")], availability)
    assert result["degraded"]
    assert result["documents"] == [doc("a")]


def test_merge_keeps_changed_parent_version_and_different_provenance():
    a = doc("a")
    updated = a.model_copy(update={"page_content": "新版正文"})
    same_body_other_parent = a.model_copy(update={"id": "b"})
    result = KnowledgeRetrievalService._merge({
        "direct": [a], "hyde": {"documents": [a.model_copy(), updated, same_body_other_parent]},
    })
    assert result["candidates"] == [a, updated, same_body_other_parent]
