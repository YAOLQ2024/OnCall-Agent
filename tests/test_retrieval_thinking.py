"""验证 LangChain 经真实 OpenAI SDK 发出的请求体，不连接外部服务。"""

import json

import httpx
import pytest
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

from app.config import Settings
from app.core import llm_factory as factory_module
from app.services.knowledge_retrieval_service import KnowledgeRetrievalService


@pytest.fixture
def recorded_requests(monkeypatch):
    requests, models = [], []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "fake-response", "object": "chat.completion", "created": 0,
            "model": "qwen3.8-max",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "独立查询或假设手记"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    def create(**kwargs):
        model = ChatOpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        models.append(model)
        return model

    monkeypatch.setattr(factory_module, "ChatOpenAI", create)
    original = factory_module.llm_factory.create_chat_model

    def fake_credentials(**kwargs):
        return original(**kwargs, api_key="fake-test-key", base_url="https://test.invalid/v1")

    monkeypatch.setattr(factory_module.llm_factory, "create_chat_model", fake_credentials)
    yield requests
    for model in models:
        model.root_client.close()


@pytest.mark.parametrize("role", ["rewrite", "hyde"])
def test_retrieval_generation_disables_thinking_in_actual_request(role, recorded_requests):
    service = KnowledgeRetrievalService(Settings(_env_file=None))
    result = service._generation_chain(role).invoke({
        "query": "它为什么 Pending", "chat_history": [HumanMessage(content="payment-service 的 Pod")],
    })
    assert result == "独立查询或假设手记"
    assert len(recorded_requests) == 1
    payload = recorded_requests[0]
    assert payload["enable_thinking"] is False
    assert "extra_body" not in payload  # SDK 将额外参数放入 HTTP JSON 顶层。
    assert payload.get("max_completion_tokens", payload.get("max_tokens")) == 512
    assert payload["temperature"] == 0
    assert payload["stream"] is False


def test_factory_default_does_not_change_agent_thinking(recorded_requests):
    model = factory_module.llm_factory.create_chat_model(streaming=False)
    model.invoke("普通回答")
    assert "enable_thinking" not in recorded_requests[0]
