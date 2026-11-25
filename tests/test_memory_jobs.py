"""M4 proposal, budget and worker races with a temporary SQLite database."""

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app.config import config
from app.models.memory import MemoryReviewRequest, ProfilePatch, SessionExtractionRequest
from app.services.memory_extraction_service import InvalidProposalError, MemoryExtractionService, ProfileProposal, candidate_kinds
from app.services.memory_retrieval_service import MemoryRetrievalService
from app.services.memory_worker import MemoryWorker
from app.storage.memory_incidents import IncidentConflictError
from app.storage.memory_jobs import INCIDENT_JOB, INDEX_JOB, PROFILE_JOB, PURGE_JOB, MemoryJobRepository
from test_memory_api import headers
from test_memory_incidents import ALICE, BOB, confirm, create, stores  # noqa: F401
from test_session_api import events, integrated_app  # noqa: F401


def m4_settings(**changes):
    return config.model_copy(update={
        "ltm_enabled": True, "ltm_auto_extract_enabled": True, "ltm_paid_calls_enabled": True,
        "ltm_daily_input_token_budget": 100_000, "ltm_daily_output_token_budget": 10_000,
        **changes,
    })


def turn(sessions, db, jobs, question="以后默认先给结论", sid="chat", *, use_enabled=False):
    db.patch_profile(ALICE, ProfilePatch(expected_revision=0, auto_extract_enabled=True, use_enabled=use_enabled))
    profile = db.read_runtime_profile(ALICE)
    user_id = sessions.record_message(sid, "user", question, ALICE)
    ids = []
    final_id = sessions.record_message(
        sid, "assistant", "我会按你的要求处理。", ALICE,
        after_insert=lambda conn, end: ids.extend(jobs.enqueue_automatic(
            conn, ALICE, sid, user_id, end, profile, (PROFILE_JOB,),
        )),
    )
    return ids[0], user_id, final_id


def response(proposal):
    return {"raw": SimpleNamespace(usage_metadata={"input_tokens": 30, "output_tokens": 20}),
            "parsed": proposal, "parsing_error": None}


@pytest.mark.asyncio
async def test_profile_job_captures_evidence_requires_review_and_keeps_other_user_empty(stores):
    sessions, db, incidents = stores
    jobs = MemoryJobRepository(db, incidents, m4_settings())
    job_id, message_id, _ = turn(sessions, db, jobs)
    worker = MemoryWorker(jobs, Mock(), jobs.settings)
    worker.extractor.extract = AsyncMock(return_value=response(ProfileProposal.model_validate({
        "changes": {"preferred_output_order": "summary_first"},
        "evidence": [{"field": "preferred_output_order", "message_id": message_id, "quote": "以后默认先给结论"}],
    })))
    await worker.process(jobs.claim((PROFILE_JOB,)))
    assert db.get_profile(ALICE).profile.preferred_output_order is None
    result = jobs.get(ALICE, job_id)
    assert result["requires_confirmation"] and result["proposal"]["operations"] == {"preferred_output_order": "ADD"}
    assert jobs.list(BOB)["jobs"] == []
    assert jobs.list(ALICE)["usage"]["input_tokens"] == 30
    jobs.review(ALICE, job_id, MemoryReviewRequest(action="apply", fields=["preferred_output_order"]))
    assert db.get_profile(ALICE).profile.preferred_output_order == "summary_first"
    assert db.get_profile(BOB).profile.preferred_output_order is None
    with pytest.raises(IncidentConflictError):
        jobs.review(ALICE, job_id, MemoryReviewRequest(action="apply"))


@pytest.mark.asyncio
async def test_response_preference_requires_review_before_cross_session_recall(stores):
    sessions, db, incidents = stores
    settings = m4_settings(ltm_vector_enabled=False)
    jobs = MemoryJobRepository(db, incidents, settings)
    preference = "英文术语旁附上中文翻译"
    question = f"以后回答都用中文，{preference}"
    job_id, message_id, _ = turn(sessions, db, jobs, question, use_enabled=True)
    worker = MemoryWorker(jobs, Mock(), settings)
    worker.extractor.extract = AsyncMock(return_value=response(ProfileProposal.model_validate({
        "changes": {"preferred_language": "zh-CN", "answer_detail": "detailed"},
        "evidence": [
            {"field": "preferred_language", "message_id": message_id, "quote": "以后回答都用中文"},
            {"field": "answer_detail", "message_id": message_id, "quote": preference},
        ],
        "preference_changes": [{"text": preference, "evidence": {"message_id": message_id, "quote": preference}}],
    })))
    await worker.process(jobs.claim((PROFILE_JOB,)))
    job = jobs.get(ALICE, job_id)
    assert job["requires_confirmation"]
    assert job["proposal"]["changes"] == {"preferred_language": "zh-CN", "response_preferences": [preference]}
    assert {entry["field"] for entry in job["proposal"]["evidence"]} == {"preferred_language", "response_preferences"}
    retrieval = MemoryRetrievalService(incidents, settings)
    pending = await retrieval.prepare(ALICE, "before-review", "DNS 是什么？", general=True)
    assert not pending.reference
    opt_in_bob = ProfilePatch(expected_revision=0, use_enabled=True, changes={"response_preferences": ["给出一个例子"]})
    db.patch_profile(BOB, opt_in_bob)
    jobs.review(ALICE, job_id, MemoryReviewRequest(action="apply", fields=["response_preferences"]))
    for session in ("later", "another"):
        memory = await retrieval.prepare(ALICE, session, "DNS 是什么？", general=True)
        assert json.loads(memory.reference)["profile"] == {"response_preferences": [preference]}
    other = await retrieval.prepare(BOB, "later", "DNS 是什么？", general=True)
    assert json.loads(other.reference)["profile"] == {"response_preferences": ["给出一个例子"]}
    assert not candidate_kinds("仅这次请用英文回答", "OK")
    jobs.forget(ALICE, 0)
    assert db.get_profile(ALICE).profile.response_preferences is None
    assert db.get_profile(BOB).profile.response_preferences == ["给出一个例子"]


def test_proposed_response_preference_updates_and_deletes_require_matching_source():
    old, keep, remove = "每次翻译英文术语", "先给一个例子", "使用表格"
    new = "只在首次出现时翻译英文术语"
    source = f"以后{new}，并且不再使用表格"
    operations = [
        {"previous": old, "text": new, "evidence": {"message_id": 1, "quote": new}},
        {"previous": remove, "evidence": {"message_id": 1, "quote": "不再使用表格"}},
    ]
    sources = {1: {"role": "user", "text": source}}
    profile = {"response_preferences": [old, keep, remove]}
    result = MemoryExtractionService.proposal(PROFILE_JOB, ProfileProposal(preference_changes=operations),
                                              sources, profile, "source")
    assert result["changes"] == {"response_preferences": [new, keep]}
    assert result["before"] == profile
    operations[0]["text"] = "添加原话从未要求的格式"
    with pytest.raises(InvalidProposalError):
        MemoryExtractionService.proposal(PROFILE_JOB, ProfileProposal(preference_changes=operations),
                                         sources, profile, "source")


@pytest.mark.asyncio
async def test_manual_incident_is_idempotent_redacted_and_review_creates_only_draft(stores):
    sessions, db, incidents = stores
    jobs = MemoryJobRepository(db, incidents, m4_settings())
    start = sessions.record_message("src", "user", '连接池告警 Authorization: Bearer abc-secret-password', ALICE)
    end = sessions.record_message("src", "assistant", "监控显示 ERR-503。", ALICE)
    request = SessionExtractionRequest(session_id="src", request_id="request", start_message_id=start, end_message_id=end)
    job = jobs.enqueue_manual(ALICE, request)
    assert jobs.enqueue_manual(ALICE, request)["job_id"] == job["job_id"]
    worker = MemoryWorker(jobs, Mock(), jobs.settings)

    async def extract(schema, messages):
        assert "abc-secret-password" not in messages[-1].content
        return response(schema.model_validate({"draft": {"title": "连接池故障", "symptoms": "ERR-503"},
            "evidence": [{"message_id": end, "quote": "监控显示 ERR-503"}]}))

    worker.extractor.extract = extract
    await worker.process(jobs.claim((INCIDENT_JOB,)))
    proposal = jobs.get(ALICE, job["job_id"])["proposal"]
    assert proposal["draft"]["source_message_ids"] == [end]
    assert not incidents.list(ALICE, None, 20, None)["items"]
    result = jobs.review(ALICE, job["job_id"], MemoryReviewRequest(action="apply"))
    draft = incidents.get(ALICE, result["memory_id"])
    assert draft["status"] == "draft" and not draft["outcome"]
    assert not incidents.search(ALICE, "ERR-503", 20)["items"]
    assert sessions.delete_session("src", ALICE)
    assert incidents.get(ALICE, result["memory_id"])["status"] == "withdrawn"


@pytest.mark.asyncio
async def test_lease_recovery_and_forget_racing_an_old_result(stores):
    sessions, db, incidents = stores
    jobs = MemoryJobRepository(db, incidents, m4_settings())
    job_id, message_id, _ = turn(sessions, db, jobs)
    first = jobs.claim((PROFILE_JOB,))
    with closing(sqlite3.connect(db.db_path)) as conn, conn:
        conn.execute("UPDATE ltm_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job_id,))
    reopened = MemoryJobRepository(type(db)(db.db_path), incidents, jobs.settings)
    second = reopened.claim((PROFILE_JOB,))
    assert second["attempt_count"] == 2 and second["lease_token"] != first["lease_token"]
    reopened.finish(first, "succeeded", result={"secret": "late result"})
    assert reopened.get(ALICE, job_id)["status"] == "running"

    own = confirm(incidents, create(incidents))
    other = confirm(incidents, create(incidents, BOB), BOB)
    worker = MemoryWorker(reopened, Mock(), jobs.settings)

    async def complete_after_forget(schema, messages):
        assert reopened.forget(ALICE, 0)["generation"] == 1
        return response(schema.model_validate({"changes": {"preferred_output_order": "summary_first"},
            "evidence": [{"field": "preferred_output_order", "message_id": message_id, "quote": "以后默认先给结论"}]}))

    worker.extractor.extract = complete_after_forget
    await worker.process(second)
    stale = reopened.get(ALICE, job_id)
    assert stale["status"] == "cancelled" and "proposal" not in stale
    assert db.get_profile(ALICE).profile.preferred_output_order is None
    assert db.get_profile(ALICE).generation == 1
    assert incidents.get(ALICE, own["memory_id"])["status"] == "withdrawn"
    assert incidents.get(BOB, other["memory_id"])["status"] == "confirmed"
    assert not incidents.search(ALICE, "ERR-503", 20)["items"]
    assert sessions.get_messages("chat", ALICE)
    assert reopened.claim((PURGE_JOB,))["memory_id"] == own["memory_id"]


@pytest.mark.asyncio
async def test_budget_and_invalid_evidence_prevent_proposal(stores):
    sessions, db, incidents = stores
    jobs = MemoryJobRepository(db, incidents, m4_settings(ltm_daily_input_token_budget=1))
    job_id, message_id, _ = turn(sessions, db, jobs)
    worker = MemoryWorker(jobs, Mock(), jobs.settings)
    worker.extractor.extract = AsyncMock()
    await worker.process(jobs.claim((PROFILE_JOB,)))
    worker.extractor.extract.assert_not_called()
    assert jobs.get(ALICE, job_id)["error_code"] == "budget_exhausted"

    jobs.settings.ltm_daily_input_token_budget = 100_000
    jobs.retry(ALICE, job_id)
    worker.extractor.extract.return_value = response(ProfileProposal.model_validate({
        "changes": {"preferred_output_order": "summary_first"},
        "evidence": [{"field": "preferred_output_order", "message_id": message_id, "quote": "伪造的原话"}],
    }))
    await worker.process(jobs.claim((PROFILE_JOB,)))
    assert jobs.get(ALICE, job_id)["error_code"] == "invalid_proposal"


@pytest.mark.asyncio
async def test_index_failure_retries_without_affecting_lexical_recall(stores):
    _, db, incidents = stores
    db.patch_profile(ALICE, ProfilePatch(expected_revision=0, use_enabled=True))
    record = confirm(incidents, create(incidents))
    jobs = MemoryJobRepository(db, incidents, m4_settings())
    vector = Mock()
    vector.index_job.side_effect = TimeoutError
    worker = MemoryWorker(jobs, vector, jobs.settings)
    lease = jobs.claim((INDEX_JOB,))
    await worker.process(lease)
    assert jobs.get(ALICE, lease["job_id"])["retry_scheduled"]
    assert incidents.search(ALICE, "ERR-503", 20)["items"][0]["memory_id"] == record["memory_id"]


def test_gating_temporary_and_hypothetical_messages():
    assert candidate_kinds("这次先给结论，以后不一定", "已收到") == ()
    assert candidate_kinds("假设数据库故障是什么", "检查监控", diagnostic=True) == ()
    assert candidate_kinds("以后默认先给结论", "已收到") == (PROFILE_JOB,)
    assert candidate_kinds("线上支付报错 ERR-503", "检查日志后观察到连接池告警") == (INCIDENT_JOB,)


def test_m4_http_sse_candidate_and_forget_isolation(integrated_app, monkeypatch):
    from app.api import memory
    from app.services.memory_worker import MemoryWorker

    app, _, *_ = integrated_app
    for key, value in {
        "ltm_enabled": True, "ltm_auto_extract_enabled": True, "ltm_paid_calls_enabled": True,
        "ltm_daily_input_token_budget": 100_000, "ltm_daily_output_token_budget": 10_000,
    }.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(MemoryWorker, "_kinds", lambda self: [])
    with TestClient(app) as client:
        assert client.get("/api/memory/jobs").status_code == 401
        assert client.patch("/api/memory/profile", headers=headers(), json={
            "expected_revision": 0, "auto_extract_enabled": True,
        }).status_code == 200
        temporary = events(client.post("/api/chat_stream", headers=headers(), json={
            "Id": "tmp", "Question": "chat 这次回答详细一点",
        }))
        assert "memory_job_id" not in temporary[-1]["data"]
        failed = events(client.post("/api/chat_stream", headers=headers(), json={
            "Id": "failed", "Question": "chat 以后默认先给结论 FAIL",
        }))
        assert failed[-1]["type"] == "error"
        assert memory.memory_service.jobs.list(ALICE)["jobs"] == []
        complete = events(client.post("/api/chat_stream", headers=headers(), json={
            "Id": "profile", "Question": "chat 以后默认先给结论",
        }))
        job_id = complete[-1]["data"]["memory_job_id"]
        assert memory.memory_service.jobs.get(ALICE, job_id)["kind"] == PROFILE_JOB
        assert client.get(f"/api/memory/jobs/{job_id}", headers=headers(BOB)).status_code == 404
        assert client.post("/api/memory/incidents/from-session", headers=headers(BOB), json={
            "session_id": "profile", "request_id": "bob",
        }).status_code == 404
        assert client.post("/api/memory/forget", headers=headers(), json={
            "expected_generation": 0, "confirm": False,
        }).status_code == 422
        assert client.post("/api/memory/forget", headers=headers(), json={
            "expected_generation": 0, "confirm": True,
        }).status_code == 200
        assert client.get(f"/api/memory/jobs/{job_id}", headers=headers()).json()["data"]["status"] == "cancelled"
        assert client.get("/api/sessions/profile", headers=headers()).status_code == 200
