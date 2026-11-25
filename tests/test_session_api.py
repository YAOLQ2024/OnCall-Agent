"""Full application routes, real graphs and SQLite; external dependencies are deterministic fakes."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI

from app.storage.session_db import SessionDatabaseManager, checkpoint_thread_id
from app.storage.sqlite_saver import SqliteSaver


class SessionModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "session-test-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _answer(self, messages):
        human = [m for m in messages if isinstance(m, HumanMessage)]
        if "FAIL" in str(human[-1].content):
            raise RuntimeError("simulated model failure")
        return f"turn={len(human)}; question={human[-1].content}"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self._answer(messages)))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        answer = self._answer(messages)
        for word in (answer[:7], answer[7:]):
            yield ChatGenerationChunk(message=AIMessageChunk(content=word))


@pytest.fixture
def integrated_app(tmp_path, monkeypatch):
    from app.core.llm_factory import llm_factory
    monkeypatch.setattr(llm_factory, "create_chat_model", lambda **kwargs: SessionModel())
    from app import main
    from app.api import auth, sessions, chat, aiops, memory
    from app.services import rag_agent_service as rag_module
    from app.services import aiops_service as aiops_module
    from app.agent.supervisor import IntentType, SupervisorResult

    db = SessionDatabaseManager(str(tmp_path / "integration.db"))
    saver = SqliteSaver(db.db_path)
    from app.services.memory_service import MemoryService
    from app.storage.memory_db import MemoryDatabase
    memory_backend = MemoryService(MemoryDatabase(db.db_path))
    for module in (memory, chat, aiops):
        monkeypatch.setattr(module, "memory_service", memory_backend)
    for module in (auth, sessions, chat, aiops, rag_module):
        monkeypatch.setattr(module, "session_db_manager", db)
    rag = rag_module.rag_agent_service
    monkeypatch.setattr(rag, "model", SessionModel())
    monkeypatch.setattr(rag, "agent", None)
    monkeypatch.setattr(rag, "checkpointer", saver)

    async def route(question):
        intent = (IntentType.AIOPS_DIAGNOSIS if question.startswith("aiops") else
                  IntentType.GENERAL_CHAT if question.startswith("chat") else IntentType.KNOWLEDGE_QA)
        return SupervisorResult(intent=intent, source="rule_fast_pass", confidence=1.0,
                                reason="deterministic integration test", latency_ms=0)

    monkeypatch.setattr(chat.supervisor_router, "route", route)

    async def planner(state):
        return {"plan": [state["input"]]}

    async def executor(state):
        return {"plan": [], "past_steps": [(state["plan"][0], "checked")]}

    async def replanner(state):
        return {"response": f"report: {state['input']}; steps={len(state['past_steps'])}"}

    monkeypatch.setattr(aiops_module, "planner", planner)
    monkeypatch.setattr(aiops_module, "executor", executor)
    monkeypatch.setattr(aiops_module, "replanner", replanner)
    service = aiops_module.aiops_service
    monkeypatch.setattr(service, "checkpointer", saver)
    monkeypatch.setattr(service, "graph", service._build_graph())

    # Exercise actual lifespan wiring while replacing only external infrastructure.
    monkeypatch.setattr(main.milvus_manager, "connect", Mock())
    monkeypatch.setattr(main.milvus_manager, "health_check", Mock(return_value=True))
    monkeypatch.setattr(main.milvus_manager, "close", Mock())
    monkeypatch.setattr(main.vector_store_manager, "initialize", AsyncMock())
    monkeypatch.setattr(main.vector_store_manager, "close", AsyncMock())
    monkeypatch.setattr(main.parent_document_service, "initialize", Mock())
    monkeypatch.setattr(main.supervisor_router, "initialize", Mock())
    monkeypatch.setattr(main.mcp_registry, "start", AsyncMock())
    monkeypatch.setattr(main.mcp_registry, "close", AsyncMock())
    monkeypatch.setattr(main, "flush_langsmith", Mock())
    return main.app, db, saver, rag, service


def events(response):
    assert response.status_code == 200, response.text
    assert "text/event-stream" in response.headers["content-type"]
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


@pytest.mark.parametrize("mode", ["chat", "chat_stream"])
@pytest.mark.parametrize("prefix", ["rag", "chat", "aiops"])
def test_all_chat_routes_persist_final_answer(integrated_app, mode, prefix):
    app, db, saver, rag, service = integrated_app
    sid = f"{prefix}-{mode}"
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "alice", "password": "123456"}).status_code == 200
        assert client.get("/").status_code == 200
        assert client.get("/health").status_code == 200
        response = client.post(f"/api/{mode}", json={"Id": sid, "Question": f"{prefix} first"})
        if mode == "chat":
            assert response.status_code == 200, response.text
            answer = response.json()["data"]["answer"]
        else:
            stream = events(response)
            assert not [event for event in stream if event["type"] == "error"], stream
            if prefix == "aiops":
                assert [event["type"] for event in stream] == ["route", "aiops_progress", "aiops_progress", "done"]
                assert [event["data"]["type"] for event in stream[1:3]] == ["plan", "step_complete"]
            done = [event for event in stream if event["type"] == "done"]
            assert len(done) == 1
            answer = done[0]["data"]["answer"]
        assert answer
        detail = client.get(f"/api/sessions/{sid}").json()["data"]
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
        assert detail["messages"][-1]["content"] == answer
        assert detail["messages"][0]["execution_trace"] is None
        trace = detail["messages"][-1]["execution_trace"]
        assert trace[0]["type"] == "route"
        if prefix == "aiops":
            assert [event["data"]["type"] for event in trace[1:]] == ["plan", "step_complete"]
        if mode == "chat":
            assert response.json()["data"]["execution_trace"] == trace
        assert SessionDatabaseManager(db.db_path).get_messages(sid, "sre_alice")[-1].execution_trace == trace
        assert detail["session"]["message_count"] == 2
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        assert client.get(f"/api/sessions/{sid}").status_code == 404
        with db._get_connection() as connection:
            assert not connection.execute("SELECT 1 FROM message_execution_traces").fetchone()
        for worker in ("rag", "aiops"):
            assert saver.get_tuple({"configurable": {"thread_id": checkpoint_thread_id("sre_alice", sid, worker)}}) is None


@pytest.mark.parametrize("mode", ["chat", "chat_stream"])
def test_rag_resume_after_agent_rebuild(integrated_app, monkeypatch, mode):
    app, db, saver, rag, service = integrated_app
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "alice", "password": "123456"}).status_code == 200
        first = client.post(f"/api/{mode}", json={"Id": "resume", "Question": "rag first"})
        assert first.status_code == 200
        monkeypatch.setattr(rag, "checkpointer", SqliteSaver(db.db_path))
        monkeypatch.setattr(rag, "agent", None)
        second = client.post(f"/api/{mode}", json={"Id": "resume", "Question": "rag second"})
        if mode == "chat":
            assert "turn=2" in second.json()["data"]["answer"]
        else:
            done = [e for e in events(second) if e["type"] == "done"][0]
            assert "turn=2" in done["data"]["answer"]
        assert len(SessionDatabaseManager(db.db_path).get_messages("resume", "sre_alice")) == 4


def test_all_legacy_and_new_routes_reject_other_users(integrated_app):
    app, db, saver, rag, service = integrated_app
    db.record_message("private", "user", "secret", "sre_alice")
    db.record_message("private", "assistant", "report", "sre_alice",
                      execution_trace=[{"type": "route", "data": {"reason": "private route"}}])
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "alice", "password": "123456"}).status_code == 200
        client.post("/api/auth/login", json={"username": "bob", "password": "123456"})
        for url, payload in (("/api/chat", {"Id": "private", "Question": "rag intrude"}),
                             ("/api/chat_stream", {"Id": "private", "Question": "rag intrude"}),
                             ("/api/chat/clear", {"sessionId": "private"}),
                             ("/api/aiops", {"session_id": "private"})):
            assert client.post(url, json=payload).status_code == 404
        assert client.get("/api/chat/session/private").status_code == 404
        assert client.get("/api/sessions/private").status_code == 404
        assert client.delete("/api/sessions/private").status_code == 404
        assert db.get_messages("private", "sre_alice")[0].content == "secret"


def test_worker_switch_and_standalone_aiops_history(integrated_app):
    app, db, saver, rag, service = integrated_app
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "alice", "password": "123456"}).status_code == 200
        for question in ("rag one", "aiops one", "rag two", "aiops two"):
            response = client.post("/api/chat", json={"Id": "switch", "Question": question})
            assert response.status_code == 200, response.text
            expected = "turn=2" if question == "rag two" else "steps=1" if question.startswith("aiops") else "turn=1"
            assert expected in response.json()["data"]["answer"]
        stream = events(client.post("/api/aiops", json={"session_id": "standalone"}))
        done = [e for e in stream if e["type"] == "complete"][0]
        messages = client.get("/api/sessions/standalone").json()["data"]["messages"]
        assert messages[-1]["content"] == done["diagnosis"]["report"]
        assert [event["data"]["type"] for event in messages[-1]["execution_trace"]] == ["plan", "step_complete"]


@pytest.mark.parametrize("mode", ["chat", "chat_stream"])
def test_model_failure_leaves_no_false_answer_and_releases_lock(integrated_app, mode):
    app, db, saver, rag, service = integrated_app
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "alice", "password": "123456"}).status_code == 200
        response = client.post(f"/api/{mode}", json={"Id": "fail", "Question": "rag FAIL"})
        if mode == "chat":
            assert response.status_code == 500
        else:
            stream = events(response)
            assert len([e for e in stream if e["type"] == "error"]) == 1
            assert not [e for e in stream if e["type"] == "done"]
        assert [m.role for m in db.get_messages("fail", "sre_alice")] == ["user"]
        assert client.delete("/api/sessions/fail").status_code == 200


@pytest.mark.parametrize("mode", ["chat", "chat_stream", "aiops"])
def test_aiops_planner_quota_failure_reports_reason_without_saving_an_answer(integrated_app, monkeypatch, mode):
    from app.agent.aiops import planner
    from app.core.llm_factory import llm_factory
    from app.services import aiops_service as aiops_module
    from app.tools import retrieve_knowledge

    app, db, _, _, service = integrated_app
    monkeypatch.setattr(aiops_module, "planner", planner)
    monkeypatch.setattr(service, "graph", service._build_graph())
    monkeypatch.setattr(type(retrieve_knowledge), "ainvoke", AsyncMock(return_value="找到相关文档"))
    model_requests = []

    def reject_quota(request):
        model_requests.append(request)
        return httpx.Response(403, json={"error": {"code": "insufficient_quota", "message": "Free quota exhausted."}})

    model = ChatOpenAI(model="qwen3.8-max", api_key="fake-test-key", base_url="https://test.invalid/v1",
                       max_retries=0, streaming=True,
                       http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(reject_quota)))
    monkeypatch.setattr(llm_factory, "create_chat_model", lambda **kwargs: model)

    payload = {"session_id": "quota"} if mode == "aiops" else {"Id": "quota", "Question": "aiops Pod OOMKilled"}
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "alice", "password": "123456"}).status_code == 200
        response = client.post(f"/api/{mode}", json=payload)
        if mode == "chat":
            assert response.status_code == 500
            message = response.json()["data"]["errorMessage"]
        else:
            stream = events(response)
            assert [event["type"] for event in stream] == (["error"] if mode == "aiops" else ["route", "error"])
            message = stream[-1]["message" if mode == "aiops" else "data"]
        assert "千问 API 可用额度已用尽" in message
        assert len(model_requests) == 1
        assert [message.role for message in db.get_messages("quota", "sre_alice")] == ["user"]


def test_general_chat_history_window_keeps_more_than_forty_messages(integrated_app, monkeypatch):
    from types import SimpleNamespace
    from app.api import chat

    history = [SimpleNamespace(role="user" if index % 2 == 0 else "assistant", content=f"turn {index}")
               for index in range(51)]
    _, db, *_ = integrated_app
    monkeypatch.setattr(db, "get_messages", lambda *_: history)
    monkeypatch.setattr(chat.config, "chat_history_estimated_tokens", 1000)
    assert [message.content for message in chat.general_chat_messages("long", "sre_alice")[1:]] == [
        message.content for message in history
    ]

    history[-1].content = "x" * 4000
    assert [message.content for message in chat.general_chat_messages("long", "sre_alice")[1:]] == [
        history[-1].content
    ]


@pytest.mark.asyncio
async def test_http_concurrent_delete_and_identity_switch(integrated_app, monkeypatch):
    app, db, saver, rag, service = integrated_app
    from app.api import chat
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_query(question, session_id, user_id, *, memory_context=None):
        started.set()
        await release.wait()
        return f"answer for {user_id}"

    monkeypatch.setattr(rag, "query", slow_query)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/auth/login", json={"username": "alice", "password": "123456"})
        alice_token = login.json()["data"]["access_token"]
        pending = asyncio.create_task(client.post("/api/chat", json={"Id": "busy-http", "Question": "rag slow"}))
        await asyncio.wait_for(started.wait(), 5)
        try:
            assert (await client.delete("/api/sessions/busy-http")).status_code == 409
            assert (await client.post("/api/chat", json={"Id": "busy-http", "Question": "rag again"})).status_code == 409
            await client.post("/api/auth/login", json={"username": "bob", "password": "123456"})
        finally:
            release.set()
        assert (await pending).status_code == 200
        assert db.get_messages("busy-http", "dev_bob") == []
        assert db.get_messages("busy-http", "sre_alice")[-1].content == "answer for sre_alice"
        assert (await client.delete("/api/sessions/busy-http", headers={"Authorization": f"Bearer {alice_token}"})).status_code == 200


def test_login_guards_cover_auxiliary_apis(integrated_app):
    app, db, saver, rag, service = integrated_app
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/tools/status").status_code == 401
        assert client.post("/api/tools/refresh").status_code == 401
        assert client.post("/api/knowledge/search", json={"query": "Pending"}).status_code == 401
        assert client.post("/api/index_directory", json={"directory_path": "aiops-docs"}).status_code == 401
        assert client.post("/api/upload", files={"file": ("auth-test.md", b"must not index")}).status_code == 401
        assert client.post("/api/chat", json={"Id": "not-created", "Question": "rag"}).status_code == 401
        assert db.list_sessions("sre_alice") == []
        assert client.post("/api/auth/login", json={"username": "alice", "password": "123456"}).status_code == 200
        assert client.get("/api/tools/status").status_code == 200
