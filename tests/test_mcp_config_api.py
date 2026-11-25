"""MCP 动态配置接口与热重载测试"""

import json
from pathlib import Path
import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from app.api import auth, tools
from app.config import config
from app.storage.session_db import SessionDatabaseManager


@pytest.fixture
def mcp_api(tmp_path, monkeypatch):
    sessions = SessionDatabaseManager(str(tmp_path / "test_sessions.db"))
    monkeypatch.setattr(auth, "session_db_manager", sessions)
    
    test_json_file = tmp_path / "mcp_servers.json"
    monkeypatch.setattr(config, "mcp_servers_file", test_json_file)

    app = FastAPI()
    app.include_router(
        tools.router,
        prefix="/api",
        dependencies=[Depends(auth.resolve_user_id)],
    )
    return app, test_json_file


def auth_headers():
    token = auth.create_access_token("sre_alice", "sre")
    return {"Authorization": f"Bearer {token}"}


def test_get_config_when_empty(mcp_api):
    app, test_json_file = mcp_api
    with TestClient(app) as client:
        res = client.get("/api/tools/config", headers=auth_headers())
        assert res.status_code == 200
        data = res.json()["data"]
        assert "servers" in data["status"]
        assert data["parsed"] == {}


def test_save_and_update_mcp_config(mcp_api):
    app, test_json_file = mcp_api
    with TestClient(app) as client:
        payload = {
            "mcpServers": {
                "custom-echo": {
                    "transport": "stdio",
                    "command": "python",
                    "args": ["-c", "print('hello')"]
                }
            }
        }
        res = client.post(
            "/api/tools/config",
            headers=auth_headers(),
            json={"content": json.dumps(payload)}
        )
        assert res.status_code == 200
        assert test_json_file.exists()

        # 读取验证
        get_res = client.get("/api/tools/config", headers=auth_headers())
        assert "custom-echo" in get_res.json()["data"]["parsed"]["mcpServers"]


def test_add_and_delete_server(mcp_api):
    app, test_json_file = mcp_api
    with TestClient(app) as client:
        # 添加单个服务
        res = client.post(
            "/api/tools/servers",
            headers=auth_headers(),
            json={
                "name": "weather-test",
                "server": {
                    "transport": "stdio",
                    "command": "python",
                    "args": ["test.py"]
                }
            }
        )
        assert res.status_code == 200

        # 删除该服务
        del_res = client.delete(
            "/api/tools/servers/weather-test",
            headers=auth_headers()
        )
        assert del_res.status_code == 200

        # 再次读取验证
        get_res = client.get("/api/tools/config", headers=auth_headers())
        parsed = get_res.json()["data"]["parsed"]
        servers = parsed.get("mcpServers", parsed)
        assert "weather-test" not in servers
