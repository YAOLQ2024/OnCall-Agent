"""真实 SDK + MockTransport 验证 DashScope 协议，不发外部请求。"""

import json

import httpx
import pytest
from openai import OpenAI, APITimeoutError
from langsmith import tracing_context

from app.config import Settings
from app.services.qwen_rerank import QwenRerankCrossEncoder
from app.services.rerank_service import RerankService
from langchain_core.documents import Document


def adapter(handler):
    settings = Settings(_env_file=None, qwen_api_key="fake-test-key", rag_rerank_enabled=True)
    sdk = OpenAI(
        api_key=settings.qwen_api_key, base_url=settings.qwen_api_base,
        timeout=settings.qwen_rerank_timeout, max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return QwenRerankCrossEncoder(settings, client=sdk), settings


def test_api_payload_uses_native_url_and_maps_sorted_indices_back_to_input_order():
    requests = []

    def respond(request):
        requests.append(request)
        assert str(request.url) == "https://maas.qianwenaiapi.com/api/v1/services/rerank/text-rerank/text-rerank"
        assert request.headers["authorization"] == "Bearer fake-test-key"
        assert json.loads(request.content) == {
            "model": "qwen3.7-text-rerank", "input": {"query": "问题", "documents": ["正文 A", "正文 B"]},
            "parameters": {"top_n": 2, "return_documents": False},
        }
        return httpx.Response(200, json={"output": {"results": [
            {"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1},
        ]}})

    model, _ = adapter(respond)
    try:
        runs = []
        with tracing_context(enabled="local"):
            assert model.score([("问题", "正文 A"), ("问题", "正文 B")],
                               langsmith_extra={"on_end": runs.append}) == [0.1, 0.9]
        assert runs[0].extra["metadata"]["model"] == "qwen3.7-text-rerank"
        assert model.score([]) == []
        assert len(requests) == 1
    finally:
        model.close()


@pytest.mark.parametrize("results", [
    [], [{"index": 0, "relevance_score": 1}],
    [{"index": 0, "relevance_score": 1}, {"index": 0, "relevance_score": 2}],
    [{"index": -1, "relevance_score": 1}, {"index": 1, "relevance_score": 2}],
    [{"index": 0, "relevance_score": "0.2"}, {"index": 1, "relevance_score": 2}],
    [{"index": True, "relevance_score": 1}, {"index": 1, "relevance_score": 2}],
])
def test_invalid_or_partial_results_are_rejected(results):
    model, _ = adapter(lambda request: httpx.Response(200, json={"output": {"results": results}}))
    try:
        with pytest.raises(ValueError):
            model.score([("问题", "A"), ("问题", "B")])
    finally:
        model.close()


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_failures_fall_back_to_direct_documents_without_retry(status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"code": "test_failure", "message": "fake"})

    model, settings = adapter(respond)
    service = RerankService(settings, model_provider=lambda: model)
    direct, hyde = Document(id="direct", page_content="直接路"), Document(id="hyde", page_content="HyDE 路")
    try:
        result = service.rerank([direct, hyde], "问题", [direct], service.availability())
        assert result["degraded"]
        assert result["documents"] == [direct]
        assert len(requests) == 1
    finally:
        service.close()
        service.close()
    assert model.client.is_closed()


def test_request_timeout_is_propagated_without_retry():
    requests = []

    def respond(request):
        requests.append(request)
        assert request.extensions["timeout"]["read"] == 20
        raise httpx.ReadTimeout("fake timeout", request=request)

    model, _ = adapter(respond)
    try:
        with pytest.raises(APITimeoutError):
            model.score([("问题", "正文")])
        assert len(requests) == 1
    finally:
        model.close()
