"""Real JWT identity and M1 APIs against temporary SQLite; no external services."""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth, memory
from app.config import config
from app.services.memory_service import MemoryService
from app.storage.memory_db import MemoryDatabase
from app.storage.session_db import SessionDatabaseManager
from test_session_api import integrated_app  # noqa: F401 -- shared pytest fixture


@pytest.fixture(autouse=True)
def disable_memory_runtime_for_m1(monkeypatch):
    monkeypatch.setattr(config, "ltm_enabled", False)


@pytest.fixture
def profile_api(tmp_path, monkeypatch):
    sessions = SessionDatabaseManager(str(tmp_path / "api.db"))
    repository = MemoryDatabase(sessions.db_path)
    monkeypatch.setattr(auth, "session_db_manager", sessions)
    monkeypatch.setattr(memory, "memory_service", MemoryService(repository))
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(memory.router, prefix="/api")
    return app, repository


def headers(uid="sre_alice", role="sre"):
    return {"Authorization": "Bearer " + auth.create_access_token(uid, role)}


def test_anonymous_has_no_migration_side_effect(profile_api):
    app, db = profile_api
    with TestClient(app) as client:
        assert client.get("/api/memory/profile").status_code == 401
        assert client.patch("/api/memory/profile", json={"expected_revision": 0}).status_code == 401
        assert client.get("/api/memory/profile", headers={"X-User-Id": "admin"}).status_code == 401
    with sqlite3.connect(db.db_path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'ltm_%'").fetchall() == []


def test_profile_api_contract_conflicts_deletion_and_owner_isolation(profile_api):
    app, db = profile_api
    with TestClient(app) as client:
        first = client.get("/api/memory/profile", headers=headers())
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        assert first.json()["data"]["profile"] == {}
        assert first.json()["data"]["runtime_connected"] is False
        saved = client.patch("/api/memory/profile", headers=headers(), json={
            "expected_revision": 0, "changes": {"answer_detail": "concise", "default_namespace": "prod-pay"}, "use_enabled": True})
        assert saved.status_code == 200
        assert saved.json()["data"]["revision"] == 1
        conflict = client.patch("/api/memory/profile", headers=headers(), json={"expected_revision": 0, "changes": {"answer_detail": "detailed"}})
        assert conflict.status_code == 409
        assert db.get_profile("sre_alice").profile.answer_detail == "concise"
        for uid in ("dev_bob", "admin"):
            own = client.get("/api/memory/profile", headers=headers(uid))
            assert own.json()["data"]["profile"] == {}
            assert client.get("/api/memory/profile?user_id=sre_alice", headers=headers(uid)).status_code == 422
            assert client.get("/api/memory/profile/sre_alice", headers=headers(uid)).status_code == 404
        removed = client.patch("/api/memory/profile", headers=headers(), json={"expected_revision": 1, "changes": {"default_namespace": None}})
        assert removed.status_code == 200
        assert removed.json()["data"]["profile"] == {"answer_detail": "concise"}
        assert client.get("/api/memory/profile", headers=headers("dev_bob")).json()["data"]["profile"] == {}


@pytest.mark.parametrize("payload", [
    {"expected_revision": 0, "owner_id": "dev_bob"},
    {"expected_revision": 0, "changes": {"user_id": "dev_bob"}},
    {"expected_revision": 0, "changes": {"default_namespace": "bad namespace"}},
])
def test_invalid_patch_does_not_create_memory_tables(profile_api, payload):
    app, db = profile_api
    with TestClient(app) as client:
        assert client.patch("/api/memory/profile", headers=headers(), json=payload).status_code == 422
    with sqlite3.connect(db.db_path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'ltm_%'").fetchall() == []


def test_manual_response_preferences_replace_and_clear_only_owned_profile(profile_api):
    app, _ = profile_api
    with TestClient(app) as client:
        def patch(revision, changes):
            return client.patch("/api/memory/profile", headers=headers(), json={
                "expected_revision": revision, "changes": changes,
            })

        assert patch(0, {"preferred_language": "zh-CN"}).status_code == 200
        first = patch(1, {"response_preferences": [" 英文术语附中文翻译 ", "英文术语附中文翻译", "先给例子"]})
        assert first.json()["data"]["profile"] == {"preferred_language": "zh-CN",
                                                    "response_preferences": ["英文术语附中文翻译", "先给例子"]}
        assert patch(1, {"response_preferences": ["过期修改"]}).status_code == 409
        assert patch(2, {"response_preferences": ["a" * 501]}).status_code == 422
        assert patch(2, {"response_preferences": ["一行\n另一行"]}).status_code == 422
        assert patch(2, {"response_preferences": ["只在首次出现时翻译"]}).status_code == 200
        assert client.get("/api/memory/profile", headers=headers("dev_bob")).json()["data"]["profile"] == {}
        assert patch(3, {"response_preferences": []}).json()["data"]["profile"] == {"preferred_language": "zh-CN"}


def test_complete_app_m1_profile_does_not_write_from_chat(integrated_app):
    app, sessions, saver, rag, aiops = integrated_app
    with TestClient(app) as client:
        assert client.get("/api/memory/profile").status_code == 401
        token = auth.create_access_token("sre_alice", "sre")
        client.headers["Authorization"] = f"Bearer {token}"
        before = client.get("/api/memory/profile").json()["data"]
        assert before["profile"] == {}
        saved = client.patch("/api/memory/profile", json={"expected_revision": 0, "changes": {"default_namespace": "prod-pay"}})
        assert saved.status_code == 200
        response = client.post("/api/chat", json={"Id": "m1-chat", "Question": "chat 这次临时使用 staging，不要长期记录"})
        assert response.status_code == 200
        after = client.get("/api/memory/profile").json()["data"]
        assert after["profile"] == {"default_namespace": "prod-pay"}
        assert after["revision"] == 1
        assert after["runtime_connected"] is False
