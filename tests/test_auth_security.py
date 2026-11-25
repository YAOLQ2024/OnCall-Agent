"""Authentication regression: bypasses, JWT claims/revocation and password migration."""
import hashlib
import hmac
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from test_session_storage import api_app, temp_db
from app.api import auth
from app.storage.session_db import SessionDatabaseManager


def login(client, username="alice"):
    response = client.post("/api/auth/login", json={"username": username, "password": "123456"})
    assert response.status_code == 200
    return response.json()["data"]["access_token"]


def signed_token(payload, header=None):
    head = auth._b64url_encode(json.dumps(header or {"alg": "HS256", "typ": "JWT"}).encode())
    body = auth._b64url_encode(json.dumps(payload).encode())
    signature = hmac.new(auth.JWT_SECRET_KEY.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{auth._b64url_encode(signature)}"


@pytest.mark.parametrize("headers,cookies", [({}, {}), ({"X-User-Id": "admin"}, {}),
    ({}, {"oncall_user_id": "admin"}), ({}, {"oncall_token": "expired", "oncall_user_id": "admin"})])
def test_no_default_or_legacy_identity_bypass(api_app, headers, cookies):
    app, db = api_app
    with TestClient(app, headers=headers, cookies=cookies) as client:
        for path in ("/api/sessions", "/api/auth/me", "/api/auth/current", "/api/auth/users"):
            assert client.get(path).status_code == 401
        assert client.post("/api/auth/switch", json={"user_id": "admin"}).status_code == 410


@pytest.mark.parametrize("change", ["missing_exp", "zero_exp", "expired", "boundary", "missing_sub",
                                    "sub_list", "missing_jti", "future_iat", "float_exp", "wrong_aud", "alg_none"])
def test_invalid_signed_claims_are_rejected(api_app, change):
    app, db = api_app
    now = int(time.time())
    payload = {"sub": "sre_alice", "role": "sre", "iat": now, "exp": now + 1000,
               "jti": "a" * 48, "iss": "oncall-agent", "aud": "oncall-ui"}
    header = None
    if change == "missing_exp": payload.pop("exp")
    if change == "zero_exp": payload["exp"] = 0
    if change == "expired": payload["exp"] = now - 1
    if change == "boundary": payload["exp"] = now
    if change == "missing_sub": payload.pop("sub")
    if change == "sub_list": payload["sub"] = ["admin"]
    if change == "missing_jti": payload.pop("jti")
    if change == "future_iat": payload["iat"] = now + 60
    if change == "float_exp": payload["exp"] = now + 1000.0
    if change == "wrong_aud": payload["aud"] = "another-app"
    if change == "alg_none": header = {"alg": "none", "typ": "JWT"}
    with TestClient(app) as client:
        assert client.get("/api/sessions", headers={"Authorization": "Bearer " + signed_token(payload, header)}).status_code == 401


def test_logout_revokes_only_current_token_and_persists(api_app):
    app, db = api_app
    with TestClient(app) as client:
        token1 = login(client)
        token2 = login(client)
        assert token1 != token2
        headers1 = {"Authorization": f"Bearer {token1}"}
        headers2 = {"Authorization": f"Bearer {token2}"}
        assert client.post("/api/auth/logout", headers=headers1).status_code == 200
        assert client.get("/api/auth/me", headers=headers1).status_code == 401
        assert client.get("/api/auth/me", headers=headers2).status_code == 200
        claims = json.loads(auth._b64url_decode(token1.split('.')[1]))
        assert SessionDatabaseManager(db.db_path).is_token_revoked(claims["jti"])
        assert client.get("/api/sessions").status_code == 401


def test_bad_authorization_never_falls_back_to_valid_cookie(api_app):
    app, db = api_app
    with TestClient(app) as client:
        login(client)
        for value in ("Basic anything", "Bearer", "Bearer invalid", "Bearer a b"):
            for path in ("/api/auth/me", "/api/sessions"):
                assert client.get(path, headers={"Authorization": value}).status_code == 401
        assert client.get("/api/auth/me").status_code == 200
        assert client.get("/api/auth/me", headers={"X-User-Id": "admin"}).json()["data"]["user_id"] == "sre_alice"


def test_password_random_salt_and_legacy_hash_upgrade(temp_db):
    db = SessionDatabaseManager(temp_db)
    with sqlite3.connect(temp_db) as conn:
        rows = conn.execute("SELECT password_hash FROM users").fetchall()
        assert len(set(row[0] for row in rows)) == 3
        legacy = hashlib.pbkdf2_hmac("sha256", b"custom-password", b"oncall_sre_salt_v1", 10000).hex()
        conn.execute("UPDATE users SET password_hash = ? WHERE user_id = 'sre_alice'", (legacy,))
    reopened = SessionDatabaseManager(temp_db)
    assert reopened.authenticate_user("alice", "123456") is None
    assert reopened.authenticate_user("alice", "custom-password").user_id == "sre_alice"
    with sqlite3.connect(temp_db) as conn:
        upgraded = conn.execute("SELECT password_hash FROM users WHERE user_id = 'sre_alice'").fetchone()[0]
    assert upgraded.startswith("pbkdf2_sha256$600000$")
    assert upgraded != legacy


def test_local_signing_secret_survives_manager_rebuild(temp_db):
    db = SessionDatabaseManager(temp_db)
    secret = db.get_or_create_secret("test-signing-key")
    assert len(secret) >= 64
    assert SessionDatabaseManager(temp_db).get_or_create_secret("test-signing-key") == secret
