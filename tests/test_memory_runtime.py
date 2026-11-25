"""Runtime paths with real SQLite/LangGraph and fake model and vector calls."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from threading import Event
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from langgraph.runtime import Runtime
from pydantic import PrivateAttr

from app.config import config
from app.models.memory import MemoryScope, ProfilePatch
from app.models.memory_runtime import MemoryRunContext
from app.services.memory_retrieval_service import MemoryRetrievalService
from app.storage.memory_incidents import IncidentIndexRef
from app.storage.session_db import checkpoint_thread_id
from test_memory_api import headers
from test_memory_incidents import ALICE, BOB, confirm, create, stores  # noqa: F401 -- shared fixture
from test_session_api import SessionModel, events, integrated_app  # noqa: F401 -- shared fixture


def settings(**overrides):
    return config.model_copy(update={"ltm_enabled": True, "ltm_vector_enabled": False, **overrides})


def opt_in(repository, uid=ALICE, **fields):
    return repository.patch_profile(uid, ProfilePatch(expected_revision=0, use_enabled=True, changes=fields))


@pytest.mark.asyncio
async def test_chat_lookup_does_not_create_profiles_or_schema_when_unconfigured(stores):
    _, repository, incidents = stores
    service = MemoryRetrievalService(incidents, settings())
    assert await service.prepare(ALICE, "chat", "production ERR-503") is None
    with closing(sqlite3.connect(repository.db_path)) as conn:
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'ltm_%'").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_current_target_overrides_defaults_and_only_verified_records_are_used(stores):
    _, repository, incidents = stores
    opt_in(repository, default_environment="production", default_namespace="pay-prod",
           preferred_language="zh-CN", answer_detail="concise")
    prod = confirm(incidents, create(incidents, namespace="pay-prod", title="prod case"))
    staging = confirm(incidents, create(incidents, environment="staging", namespace="pay-stage", title="stage case",
                                        excluded_hypotheses="SKIP_CPU", actions_taken="DANGEROUS_COMMAND"))
    create(incidents, environment="staging", title="draft case")
    confirm(incidents, create(incidents, BOB, environment="staging", title="Bob private"), BOB)
    service = MemoryRetrievalService(incidents, settings())
    default = await service.prepare(ALICE, "one", "排查连接池 ERR-503")
    assert [item.memory_id for item in default.incidents] == [prod["memory_id"]]
    target = await service.prepare(ALICE, "two", "staging 环境连接池 ERR-503", explicit=MemoryScope(namespace="pay-stage"))
    assert [item.memory_id for item in target.incidents] == [staging["memory_id"]]
    payload = json.loads(target.reference)
    assert payload["profile"]["target_to_verify"] == {"environment": "staging", "namespace": "pay-stage"}
    assert payload["historical_incidents"][0]["outcome"] == "unresolved"
    assert "SKIP_CPU" not in target.reference and "DANGEROUS_COMMAND" not in target.reference
    assert "Bob private" not in target.reference
    assert repository.get_profile(ALICE).profile.default_environment == "production"

    ambiguous = await service.prepare(ALICE, "three", "比较 production 与 staging 的连接池")
    assert not ambiguous.incidents and ambiguous.reason == "scope_unavailable"
    excluded = await service.prepare(ALICE, "three", "不要在 production 查 ERR-503")
    assert not excluded.incidents and excluded.reason == "scope_unavailable"
    scope_only = await service.prepare(ALICE, "three", "环境: production")
    assert not scope_only.incidents
    changed_cluster = await service.prepare(ALICE, "three", "集群 other ERR-503")
    assert not changed_cluster.incidents and changed_cluster.reason == "scope_unavailable"

    general = await service.prepare(ALICE, "two", "chat ERR-503", general=True)
    assert json.loads(general.reference) == {"profile": {"preferred_language": "zh-CN", "answer_detail": "concise"},
                                             "historical_incidents": []}
    assert await service.prepare(BOB, "bob", "staging ERR-503") is None


@pytest.mark.asyncio
async def test_large_profile_and_three_full_incident_references_fit(stores):
    _, repository, incidents = stores
    services = [f"pay-service-{i:02}-" + "a" * 45 for i in range(40)]
    opt_in(repository, default_environment="production", responsible_services=services)
    symptoms = "连接池告警 ERR-503，" * 90 + "症状末尾"
    root = "根因分析与证据。" * 90 + "根因末尾"
    recovery = "恢复观察与校验。" * 90 + "恢复证据末尾"
    lessons = "复盘结论与验证。" * 90 + "复盘末尾"
    for i in range(3):
        confirm(incidents, create(
            incidents, title=f"事故 {i}", symptoms=symptoms, root_cause=root, lessons=lessons,
            recovery_evidence={"summary": recovery, "reference_url": "https://monitor.example/recovery"},
        ))

    result = await MemoryRetrievalService(
        incidents, settings(ltm_recall_top_k=3, ltm_context_estimated_tokens=32_768),
    ).prepare(ALICE, "chat", "ERR-503")
    payload = json.loads(result.reference)
    assert payload["profile"]["responsible_services"] == services
    assert len(result.incidents) == len(payload["historical_incidents"]) == 3
    for case in payload["historical_incidents"]:
        assert case["symptoms"] == symptoms
        assert case["root_cause"] == root
        assert case["recovery_observation"] == recovery
        assert case["lessons"] == lessons


@pytest.mark.asyncio
async def test_long_paste_keeps_environment_and_late_error_code(stores):
    _, repository, incidents = stores
    opt_in(repository, default_environment="production")
    confirm(incidents, create(incidents, title="production incident"))
    stage = confirm(incidents, create(incidents, environment="staging", title="staging incident"))
    logs = "\n".join(f"noise{i:04}" for i in range(3000))
    result = await MemoryRetrievalService(incidents, settings()).prepare(
        ALICE, "chat", logs + "\n环境: staging\nERR-503",
    )
    assert [case.memory_id for case in result.incidents] == [stage["memory_id"]]


@pytest.mark.asyncio
async def test_vector_gate_stale_hash_revision_other_user_and_failure_fall_back(stores):
    _, db, incidents = stores
    opt_in(db, default_environment="production")
    confirmed = confirm(incidents, create(incidents))
    other = confirm(incidents, create(incidents, BOB), BOB)
    lex, _ = incidents.runtime_candidates(ALICE, "ERR-503", 10, MemoryScope(environment="production"))
    foreign, _ = incidents.runtime_candidates(BOB, "ERR-503", 10, MemoryScope(environment="production"))
    assert foreign[0].memory_id == other["memory_id"]
    ref = lex[0]
    vector = Mock()
    vector.search.return_value = [
        IncidentIndexRef(**{**asdict(ref), "content_hash": "stale-hash"}),
        IncidentIndexRef(**{**asdict(ref), "revision": 1}),
        *foreign, ref,
    ]
    disabled = MemoryRetrievalService(incidents, settings(ltm_vector_enabled=True, ltm_daily_embedding_item_budget=0), vector)
    degraded = await disabled.prepare(ALICE, "chat", "ERR-503")
    assert degraded.reason == "vector_budget_disabled" and len(degraded.incidents) == 1
    vector.search.assert_not_called()

    service = MemoryRetrievalService(incidents, settings(
        ltm_vector_enabled=True, ltm_paid_calls_enabled=True, ltm_daily_embedding_item_budget=2), vector)
    result = await service.prepare(ALICE, "chat", "ERR-503")
    assert [item.memory_id for item in result.incidents] == [confirmed["memory_id"]]
    vector.search.assert_called_once()

    vector.search.side_effect = TimeoutError
    degraded = await service.prepare(ALICE, "chat", "ERR-503")
    assert degraded.status == "degraded"
    assert [item.memory_id for item in degraded.incidents] == [confirmed["memory_id"]]

    incidents.withdraw(ALICE, confirmed["memory_id"], confirmed["revision"])
    vector.search.side_effect = None
    assert not (await service.prepare(ALICE, "chat", "ERR-503")).incidents


@pytest.mark.asyncio
async def test_overall_timeout_uses_only_profile_already_read(stores, monkeypatch):
    _, db, incidents = stores
    opt_in(db, answer_detail="detailed", default_environment="production")
    service = MemoryRetrievalService(incidents, settings(ltm_recall_timeout_seconds=0.2))
    release = Event()

    def block(*args, **kwargs):
        release.wait(2)
        return [], "fts5"

    monkeypatch.setattr(incidents, "runtime_candidates", block)
    try:
        result = await service.prepare(ALICE, "chat", "ERR-503")
    finally:
        release.set()
    assert result.status == "degraded" and result.reason == "timeout"
    assert json.loads(result.reference)["profile"]["answer_detail"] == "detailed"


class CapturingModel(SessionModel):
    _calls: list = PrivateAttr(default_factory=list)

    def _answer(self, messages):
        self._calls.append([message.content for message in messages])
        return super()._answer(messages)


def test_http_rag_general_and_aiops_use_one_private_context_per_turn(integrated_app, monkeypatch):
    from app.api import chat, aiops
    from app.core.llm_factory import llm_factory
    from app.services import aiops_service as aiops_module

    app, _, saver, rag, aiops_worker = integrated_app
    retrieval = chat.memory_service.retrieval
    monkeypatch.setattr(retrieval, "settings", settings())
    preference = "英文术语旁附中文翻译"
    opt_in(retrieval.incidents.db, preferred_language="zh-CN", answer_detail="concise", default_environment="production",
           response_preferences=[preference])
    alice = confirm(retrieval.incidents, create(retrieval.incidents, title="ALICE-ONLY", symptoms="ERR-503"))
    opt_in(retrieval.incidents.db, BOB, preferred_language="en", default_environment="production")
    confirm(retrieval.incidents, create(retrieval.incidents, BOB, title="BOB-ONLY"), BOB)
    model = CapturingModel()
    monkeypatch.setattr(rag, "model", model)
    monkeypatch.setattr(llm_factory, "create_chat_model", lambda **kwargs: model)
    observed = []

    async def planner(state, runtime: Runtime[MemoryRunContext]):
        observed.append(runtime.context)
        return {"plan": [state["input"]]}

    async def executor(state, runtime: Runtime[MemoryRunContext]):
        observed.append(runtime.context)
        return {"plan": [], "past_steps": [(state["plan"][0], "checked")]}

    async def replanner(state, runtime: Runtime[MemoryRunContext]):
        observed.append(runtime.context)
        return {"response": "report"}

    for name, handler in (("planner", planner), ("executor", executor), ("replanner", replanner)):
        monkeypatch.setattr(aiops_module, name, handler)
    monkeypatch.setattr(aiops_worker, "graph", aiops_worker._build_graph())

    with TestClient(app) as client:
        response = client.post("/api/chat", headers=headers(), json={"Id": "m3-rag", "Question": "ERR-503"})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["memory"]["incidents"][0]["title"] == "ALICE-ONLY"
        assert sum("ALICE-ONLY" in str(item) for item in model._calls[-1]) == 1
        assert preference in str(model._calls[-1])
        assert "BOB-ONLY" not in str(model._calls[-1])
        config_dict = {"configurable": {"thread_id": checkpoint_thread_id(ALICE, "m3-rag", "rag")}}
        checkpoint = str(saver.get_tuple(config_dict).checkpoint["channel_values"])
        assert "ALICE-ONLY" not in checkpoint and preference not in checkpoint

        stream = events(client.post("/api/chat_stream", headers=headers(BOB), json={"Id": "m3-bob", "Question": "ERR-503"}))
        assert [event["type"] for event in stream[:2]] == ["route", "memory_recall"]
        assert stream[1]["data"]["incidents"][0]["title"] == "BOB-ONLY"
        assert "ALICE-ONLY" not in str(stream)
        assert preference not in str(model._calls[-1])
        assert stream[-1]["type"] == "done"

        retrieval.incidents.db.patch_profile(ALICE, ProfilePatch(expected_revision=1, changes={"answer_detail": "detailed"}))
        general = client.post("/api/chat", headers=headers(), json={"Id": "m3-general", "Question": "chat hello"}).json()["data"]
        assert general["memory"]["profile_revision"] == 2
        assert general["memory"]["incidents"] == []
        assert '"answer_detail":"detailed"' in str(model._calls[-1])
        assert preference in str(model._calls[-1])
        assert "ALICE-ONLY" not in str(model._calls[-1])

        stream = events(client.post("/api/chat_stream", headers=headers(), json={"Id": "m3-aiops", "Question": "aiops ERR-503"}))
        assert stream[1]["data"]["incidents"][0]["title"] == "ALICE-ONLY"
        assert stream[-1]["type"] == "done" and stream[-1]["data"]["answer"] == "report"
        assert len(observed) == 3 and all(context is observed[0] for context in observed)
        assert observed[0].incidents[0].memory_id == alice["memory_id"]
        assert preference in observed[0].reference

        observed.clear()
        standalone = events(client.post("/api/aiops", headers=headers(), json={
            "session_id": "m3-direct", "symptoms": "ERR-503", "memory_scope": {"environment": "production"},
        }))
        assert standalone[0]["type"] == "memory_recall" and standalone[-1]["diagnosis"]["report"] == "report"
        assert len(observed) == 3 and observed[0].incidents[0].memory_id == alice["memory_id"]

        retrieval.incidents.withdraw(ALICE, alice["memory_id"], alice["revision"])
        repeated = events(client.post("/api/chat_stream", headers=headers(), json={"Id": "m3-rag", "Question": "ERR-503"}))
        assert repeated[1]["data"]["incidents"] == []
        assert "ALICE-ONLY" not in str(model._calls[-1])
        assert aiops.memory_service is chat.memory_service

        monkeypatch.setattr(retrieval.incidents.db, "read_runtime_profile", Mock(side_effect=RuntimeError("private SQLite detail")))
        degraded = client.post("/api/chat", headers=headers(), json={"Id": "m3-error", "Question": "ERR-503"}).json()["data"]
        assert degraded["success"] and degraded["memory"]["reason"] == "memory_unavailable"
        assert "private SQLite detail" not in str(degraded)
