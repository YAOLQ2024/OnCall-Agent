"""M2 lifecycle, provenance, SQLite migration/rollback, and real authenticated HTTP."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app.models.memory import IncidentConfirm, IncidentCreate, IncidentPatch, ProfilePatch
from app.storage import memory_db
from app.storage.memory_db import MemoryDatabase
from app.storage.memory_incidents import IncidentConflictError, IncidentNotFoundError, IncidentRepository
from app.storage.session_db import SessionDatabaseManager
from test_memory_api import headers, profile_api  # noqa: F401 -- shared fixtures
from test_session_api import integrated_app  # noqa: F401 -- shared fixture

ALICE = "sre_alice"
BOB = "dev_bob"
BASE = "/api/memory/incidents"
FIELDS = {"title": "支付连接池耗尽", "symptoms": "连接池告警 ERR-503", "services": ["pay-api"],
          "error_codes": ["ERR-503"], "environment": "production", "cluster": "primary",
          "occurred_at": "2025-09-01T10:00:00+08:00"}


@pytest.fixture
def stores(tmp_path):
    sessions = SessionDatabaseManager(str(tmp_path / "incident.db"))
    repository = MemoryDatabase(sessions.db_path)
    return sessions, repository, IncidentRepository(repository)


def create(incidents, uid=ALICE, **overrides):
    return incidents.create(uid, IncidentCreate.model_validate({**FIELDS, **overrides}))


def confirm(incidents, record, uid=ALICE):
    return incidents.confirm(uid, record["memory_id"], IncidentConfirm(
        expected_revision=record["revision"], outcome="unresolved"))


def matches(incidents, query="连接池", uid=ALICE, **filters):
    return [item["memory_id"] for item in incidents.search(uid, query, 20, **filters)["items"]]


def test_m2_upgrades_m1_with_backup_and_retains_profiles(stores):
    sessions, db, incidents = stores
    sessions.record_message("existing", "user", "history", ALICE)
    saved = db.patch_profile(ALICE, ProfilePatch(expected_revision=0, changes={"answer_detail": "concise"}))
    create(incidents)
    backup = db.last_backup_path
    assert backup.name.startswith("ltm-m2-")
    with closing(sqlite3.connect(backup)) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT version FROM ltm_schema_migrations").fetchall() == [(1,)]
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    assert db.get_profile(ALICE) == saved
    assert db.patch_profile(ALICE, ProfilePatch(expected_revision=1, changes={"answer_detail": "normal"})).revision == 2
    reopened = MemoryDatabase(sessions.db_path)
    assert len(IncidentRepository(reopened).list(ALICE, None, 20, None)["items"]) == 1
    assert reopened.last_backup_path is None


def test_confirm_edit_invalidate_withdraw_and_lexical_rebuild(stores):
    _, db, incidents = stores
    draft = create(incidents)
    mid = draft["memory_id"]
    assert matches(incidents) == []
    confirmed = confirm(incidents, draft)
    assert confirmed["outcome"] == "unresolved" and confirmed["confirmed_by"] == ALICE
    assert matches(incidents) == [mid]
    assert matches(incidents, "PAY-API") == matches(incidents, '"ERR-503"') == [mid]
    assert matches(incidents, "pay") == matches(incidents, "namespace:*") == []
    assert matches(incidents, environment="staging") == matches(incidents, cluster="other") == []
    assert matches(incidents, environment="production", cluster="primary") == [mid]
    assert matches(incidents, uid=BOB) == []
    with closing(sqlite3.connect(db.db_path)) as conn, conn:
        conn.execute("UPDATE ltm_incidents_fts SET revision=1 WHERE memory_id=?", (mid,))
    assert matches(incidents) == []
    incidents.rebuild_index(ALICE)
    assert matches(incidents) == [mid]
    assert incidents.patch(ALICE, mid, IncidentPatch(expected_revision=2, changes={}))["revision"] == 2
    edited = incidents.patch(ALICE, mid, IncidentPatch(expected_revision=2, changes={"root_cause": "空闲连接未关闭"}))
    assert edited["status"] == "draft" and edited["outcome"] is None
    assert matches(incidents) == []
    confirm(incidents, edited)
    withdrawn = incidents.withdraw(ALICE, mid, 4)
    assert withdrawn["status"] == "withdrawn" and "incident" not in withdrawn
    assert matches(incidents, "ERR-503") == []
    with closing(sqlite3.connect(db.db_path)) as conn:
        assert conn.execute("SELECT title, incident_json, content_hash, source_session_id FROM ltm_incidents").fetchone() == (None,) * 4
        assert conn.execute("SELECT count(*) FROM ltm_incidents_fts").fetchone()[0] == 0
        jobs = conn.execute("SELECT kind, expected_revision, status, source_hash FROM ltm_jobs ORDER BY rowid").fetchall()
        assert jobs == [("index_incident", 2, "cancelled", None), ("purge_incident_index", 3, "pending", None),
                        ("index_incident", 4, "cancelled", None), ("purge_incident_index", 5, "pending", None)]
        assert "空闲连接" not in json.dumps(conn.execute("SELECT * FROM ltm_audit").fetchall(), ensure_ascii=False)


def test_filter_before_limit_and_check_expiry_generation_and_distinct_incidents(stores):
    _, db, incidents = stores
    first = confirm(incidents, create(incidents))
    second = confirm(incidents, create(incidents, occurred_at="2025-09-02T10:00:00+08:00"))
    confirm(incidents, create(incidents, BOB), BOB)
    confirm(incidents, create(incidents, environment="staging"))
    assert set(matches(incidents)) >= {first["memory_id"], second["memory_id"]}
    assert set(matches(incidents, environment="production")) == {first["memory_id"], second["memory_id"]}
    with closing(sqlite3.connect(db.db_path)) as conn, conn:
        conn.execute("UPDATE ltm_incidents SET expires_at='2025-01-01T00:00:00+00:00' WHERE memory_id=?", (first["memory_id"],))
    assert incidents.search(ALICE, "连接池", 1, environment="production")["items"][0]["memory_id"] == second["memory_id"]
    db.get_profile(ALICE)
    with closing(sqlite3.connect(db.db_path)) as conn, conn:
        conn.execute("UPDATE ltm_profiles SET generation=1 WHERE user_id=?", (ALICE,))
    assert matches(incidents) == []


def test_lexical_fallback_reports_degraded_and_matches_only_exact_identifiers(stores, monkeypatch):
    _, _, incidents = stores
    monkeypatch.setattr(memory_db, "create_fts", lambda conn: None)
    entry = confirm(incidents, create(incidents))
    assert incidents.search(ALICE, "ERR-503", 1)["lexical_mode"] == "degraded"
    assert matches(incidents, "ERR-503") == [entry["memory_id"]]
    assert matches(incidents, "ERR") == matches(incidents) == matches(incidents, "ERR-503", BOB) == []
    incidents.withdraw(ALICE, entry["memory_id"], 2)
    assert matches(incidents, "ERR-503") == []


def test_two_concurrent_confirmations_enqueue_only_one_revision(stores):
    sessions, db, incidents = stores
    draft = create(incidents)
    barrier = Barrier(2)

    def worker(_):
        other = IncidentRepository(MemoryDatabase(sessions.db_path))
        barrier.wait(timeout=5)
        try:
            return confirm(other, draft)["status"]
        except IncidentConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, range(2))) == ["confirmed", "conflict"]
    with closing(sqlite3.connect(db.db_path)) as conn:
        assert conn.execute("SELECT count(*) FROM ltm_jobs").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM ltm_incidents_fts").fetchone()[0] == 1


def test_confirmation_and_session_delete_roll_back_on_outbox_failure(stores):
    sessions, db, incidents = stores
    sessions.record_message("origin", "user", "observation", ALICE)
    draft = create(incidents, source_session_id="origin")
    with closing(sqlite3.connect(db.db_path)) as conn, conn:
        conn.execute("""CREATE TRIGGER fail_queue BEFORE INSERT ON ltm_jobs
                     BEGIN SELECT RAISE(ABORT, 'outbox down'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="outbox down"):
        confirm(incidents, draft)
    assert incidents.get(ALICE, draft["memory_id"])["status"] == "draft"
    assert matches(incidents) == []
    with closing(sqlite3.connect(db.db_path)) as conn, conn:
        assert conn.execute("SELECT count(*) FROM ltm_incidents_fts").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_queue")
    confirm(incidents, draft)
    with closing(sqlite3.connect(db.db_path)) as conn, conn:
        conn.execute("""CREATE TRIGGER fail_purge BEFORE INSERT ON ltm_jobs WHEN new.kind='purge_incident_index'
                     BEGIN SELECT RAISE(ABORT, 'outbox down'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="outbox down"):
        sessions.delete_session("origin", ALICE)
    assert sessions.get_messages("origin", ALICE)
    assert matches(incidents) == [draft["memory_id"]]


def test_incident_http_auth_private_pagination_revision_and_invalidation(profile_api):
    app, _ = profile_api
    with TestClient(app) as client:
        assert client.get(BASE).status_code == client.post(BASE, json=FIELDS).status_code == 401
        assert client.post(BASE, headers=headers(), json={**FIELDS, "user_id": BOB}).status_code == 422
        first_response = client.post(BASE, headers=headers(), json=FIELDS)
        assert first_response.status_code == 201 and first_response.headers["cache-control"] == "no-store"
        first = first_response.json()["data"]["memory_id"]
        second = client.post(BASE, headers=headers(), json=FIELDS).json()["data"]["memory_id"]
        page = client.get(BASE, headers=headers(), params={"limit": 1, "status": "draft"}).json()["data"]
        assert page["items"][0]["memory_id"] == page["next_cursor"] == second
        assert client.get(BASE, headers=headers(), params={"cursor": second}).json()["data"]["items"][0]["memory_id"] == first
        assert client.get(BASE, headers=headers(), params={"user_id": BOB}).status_code == 422
        assert client.get(BASE + "/search", headers=headers(), params={"q": "连接池", "user_id": BOB}).status_code == 422
        path = f"{BASE}/{first}"
        for uid in (BOB, "admin"):
            assert client.get(path, headers=headers(uid)).status_code == 404
            assert client.patch(path, headers=headers(uid), json={"expected_revision": 1, "changes": {"title": "private"}}).status_code == 404
            assert client.post(path + "/confirm", headers=headers(uid), json={"expected_revision": 1, "outcome": "unresolved"}).status_code == 404
            assert client.delete(path, headers=headers(uid), params={"expected_revision": 1}).status_code == 404
            assert client.get(BASE, headers=headers(uid), params={"cursor": second}).json()["data"]["items"] == []
        assert client.post(path + "/confirm", headers=headers(), json={"expected_revision": 1, "outcome": "resolved"}).status_code == 422
        assert client.post(path + "/confirm", headers=headers(), json={
            "expected_revision": 1, "outcome": "resolved", "recovery_evidence": {
                "summary": "错误率恢复基线", "reference_url": "https://monitor.example/recovery",
            }}).status_code == 200
        assert client.get(BASE + "/search?q=ERR-503", headers=headers(BOB)).json()["data"]["items"] == []
        assert client.delete(path, headers=headers(), params={"expected_revision": 1}).status_code == 409
        assert client.post(path + "/invalidate", headers=headers(), json={"expected_revision": 2, "reason": "已更换版本"}).json()["data"]["status"] == "invalidated"
        assert client.get(BASE + "/search?q=ERR-503", headers=headers()).json()["data"]["items"] == []
        assert client.patch(path, headers=headers(), json={"expected_revision": 3, "changes": {"version_constraints": "v2"}}).json()["data"]["status"] == "draft"
        assert client.delete(path, headers=headers(), params={"expected_revision": 4}).json()["data"]["status"] == "withdrawn"
        assert client.patch(path, headers=headers(), json={"expected_revision": 5, "changes": {"title": "restore"}}).status_code == 404


def test_source_provenance_and_real_session_route_delete(integrated_app):
    app, sessions, *_ = integrated_app
    sessions.record_message("source", "user", "verified" * 40, ALICE)
    sessions.record_message("source", "assistant", "future", ALICE)
    sessions.record_message("other", "user", "other user", BOB)
    with closing(sqlite3.connect(sessions.db_path)) as conn, conn:
        conn.execute("UPDATE messages SET timestamp='2099-01-01T00:00:00+00:00' WHERE content='future'")
        future, foreign = [r[0] for r in conn.execute("SELECT id FROM messages WHERE content IN ('future','other user') ORDER BY id")]
    with TestClient(app) as client:
        reference = client.get("/api/sessions/source/message-references", headers=headers()).json()["data"]["messages"][0]
        assert len(reference["preview"]) == 100
        assert client.get("/api/sessions/source/message-references", headers=headers(BOB)).status_code == 404
        assert client.post(BASE, headers=headers(), json={**FIELDS, "source_session_id": "other"}).status_code == 404
        for message in (foreign, 1000000000):
            assert client.post(BASE, headers=headers(), json={**FIELDS, "source_session_id": "source", "source_message_ids": [message]}).status_code == 404
        assert client.post(BASE, headers=headers(), json={**FIELDS, "source_session_id": "source", "source_message_ids": [future]}).status_code == 422
        source = {**FIELDS, "source_session_id": "source", "source_message_ids": [reference["id"]]}
        draft = client.post(BASE, headers=headers(), json=source).json()["data"]
        path = f"{BASE}/{draft['memory_id']}"
        for missing in (future, foreign):
            assert client.post(path + "/confirm", headers=headers(), json={
                "expected_revision": 1, "outcome": "resolved",
                "recovery_evidence": {"summary": "test", "message_ids": [missing]},
            }).status_code == 422
        assert client.post(path + "/confirm", headers=headers(), json={
            "expected_revision": 1, "outcome": "resolved",
            "recovery_evidence": {"summary": "verified", "message_ids": [reference["id"]]},
        }).status_code == 200
        assert client.patch("/api/memory/profile", headers=headers(), json={
            "expected_revision": 0, "changes": {"answer_detail": "concise"},
        }).status_code == 200
        assert client.delete("/api/sessions/source", headers=headers(BOB)).status_code == 404
        assert client.delete("/api/sessions/source", headers=headers()).status_code == 200
        assert client.get(path, headers=headers()).json()["data"]["status"] == "withdrawn"
        assert client.get(BASE + "/search?q=ERR-503", headers=headers()).json()["data"]["items"] == []
        assert client.get("/api/memory/profile", headers=headers()).json()["data"]["profile"]["answer_detail"] == "concise"


def test_source_delete_racing_confirmation_never_leaves_searchable_content(stores):
    sessions, db, incidents = stores
    sessions.record_message("race", "user", "recovery", ALICE)
    draft = create(incidents, source_session_id="race")
    barrier = Barrier(2)

    def confirm_draft():
        barrier.wait(timeout=5)
        try:
            confirm(incidents, draft)
        except IncidentNotFoundError:
            pass  # Delete obtained the write lock first.

    def delete_source():
        barrier.wait(timeout=5)
        assert sessions.delete_session("race", ALICE)

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(confirm_draft), pool.submit(delete_source)]
        for task in tasks:
            task.result(timeout=10)
    assert incidents.get(ALICE, draft["memory_id"])["status"] == "withdrawn"
    assert matches(incidents) == []
    with closing(sqlite3.connect(db.db_path)) as conn:
        assert conn.execute("SELECT count(*) FROM ltm_jobs WHERE kind='purge_incident_index'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM ltm_jobs WHERE kind='index_incident' AND status='pending'").fetchone()[0] == 0
