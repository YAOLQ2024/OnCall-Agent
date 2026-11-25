"""单元测试：持久化会话管理器 (SessionDatabaseManager) 与 SQLite Checkpointer (SqliteSaver)。"""

import sqlite3
import pytest

from app.storage.session_db import SessionDatabaseManager, SessionOwnershipError, checkpoint_thread_id
from app.storage.sqlite_saver import SqliteSaver


@pytest.fixture
def temp_db(tmp_path):
    return str(tmp_path / "sessions.db")


def test_user_management_and_isolation(temp_db):
    """验证多用户身份切换与会话租户严格隔离。"""
    mgr = SessionDatabaseManager(db_path=temp_db)

    # 1. 验证默认预设用户
    users = mgr.list_users()
    user_ids = [u.user_id for u in users]
    assert "sre_alice" in user_ids
    assert "dev_bob" in user_ids
    assert "admin" in user_ids

    # 2. 初始默认激活 sre_alice
    assert mgr.get_current_user_id() == "sre_alice"

    # 3. Alice 创建会话
    s_alice = mgr.create_or_touch_session("sess_alice_001", title="支付网关超时排障", user_id="sre_alice")
    assert s_alice.user_id == "sre_alice"

    # 4. Bob 创建会话
    s_bob = mgr.create_or_touch_session("sess_bob_001", title="订单服务压测咨询", user_id="dev_bob")
    assert s_bob.user_id == "dev_bob"

    # 5. 租户隔离校验：Alice 只能查到自己的会话，绝对看不到 Bob 的会话
    alice_sessions = mgr.list_sessions(user_id="sre_alice")
    assert len(alice_sessions) == 1
    assert alice_sessions[0].session_id == "sess_alice_001"

    bob_sessions = mgr.list_sessions(user_id="dev_bob")
    assert len(bob_sessions) == 1
    assert bob_sessions[0].session_id == "sess_bob_001"

    # 6. 切换当前全局用户
    assert mgr.set_current_user_id("dev_bob") is True
    assert mgr.get_current_user_id() == "dev_bob"
    assert mgr.list_sessions()[0].session_id == "sess_bob_001"


def test_persistent_message_recording_and_auto_title(temp_db):
    """验证会话消息记录、自动语义标题生成与级联删除。"""
    mgr = SessionDatabaseManager(db_path=temp_db)
    session_id = "test_sess_flow"

    # 1. 记录首条用户问题 -> 自动生成标题
    mgr.record_message(
        session_id=session_id,
        role="user",
        content="线上核心网关大面积 504 紧急排障处理",
        user_id="sre_alice",
    )

    meta = mgr.get_session(session_id, user_id="sre_alice")
    assert meta is not None
    assert "504" in meta.title
    assert meta.message_count == 1

    # 2. 记录助手响应
    mgr.record_message(
        session_id=session_id,
        role="assistant",
        content="收到告警，正在排查 ingress 网关与 upstream 链路...",
        user_id="sre_alice",
    )

    # 3. 读取消息历史
    messages = mgr.get_messages(session_id, user_id="sre_alice")
    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[1].role == "assistant"
    assert "ingress" in messages[1].content

    # 4. 级联删除会话
    assert mgr.delete_session(session_id, user_id="sre_alice") is True
    assert mgr.get_session(session_id, user_id="sre_alice") is None
    assert len(mgr.get_messages(session_id, user_id="sre_alice")) == 0


def test_sqlite_saver_checkpoint_lifecycle(temp_db):
    """验证 SqliteSaver 的完整生命周期：写入 Checkpoint、读取 Tuple、遍历 List、级联删除。"""
    saver = SqliteSaver(db_path=temp_db)

    config = {
        "configurable": {
            "thread_id": "thread_sre_101",
            "checkpoint_ns": "aiops",
        }
    }

    checkpoint = {
        "v": 1,
        "id": "chk_0001",
        "ts": "2026-10-24T12:00:00",
        "channel_values": {
            "messages": ["user: Pod pending", "assistant: Checking scheduler logs"],
            "plan_step": 2,
        },
        "channel_versions": {
            "messages": "v1",
            "plan_step": "v1",
        },
        "versions_seen": {},
    }

    metadata = {
        "source": "input",
        "step": 1,
        "writes": {},
    }

    # 1. 写入 Checkpoint
    ret_config = saver.put(config, checkpoint, metadata, new_versions={"messages": "v1", "plan_step": "v1"})
    assert ret_config["configurable"]["checkpoint_id"] == "chk_0001"

    # 2. 读取最新 CheckpointTuple
    tuple_res = saver.get_tuple(config)
    assert tuple_res is not None
    assert tuple_res.checkpoint["id"] == "chk_0001"
    assert tuple_res.checkpoint["channel_values"]["plan_step"] == 2
    assert len(tuple_res.checkpoint["channel_values"]["messages"]) == 2

    # 3. 写入 Writes 并读取
    saver.put_writes(
        config={
            "configurable": {
                "thread_id": "thread_sre_101",
                "checkpoint_ns": "aiops",
                "checkpoint_id": "chk_0001",
            }
        },
        writes=[("messages", "assistant: step 2 done")],
        task_id="task_execute_step",
    )
    tuple_res2 = saver.get_tuple(config)
    assert len(tuple_res2.pending_writes) == 1
    assert tuple_res2.pending_writes[0][0] == "task_execute_step"

    # 4. 删除 thread
    saver.delete_thread("thread_sre_101")
    assert saver.get_tuple(config) is None


def test_cross_user_write_and_touch_rejected(temp_db):
    mgr = SessionDatabaseManager(temp_db)
    mgr.record_message("owned", "user", "Alice question", "sre_alice")
    before = mgr.get_session("owned", "sre_alice")
    with pytest.raises(SessionOwnershipError):
        mgr.create_or_touch_session("owned", user_id="dev_bob")
    with pytest.raises(SessionOwnershipError):
        mgr.record_message("owned", "assistant", "injected", "dev_bob")
    assert mgr.get_session("owned", "sre_alice") == before
    assert mgr.get_messages("owned", "dev_bob") == []
    assert len(mgr.get_messages("owned", "sre_alice")) == 1


def checkpoint_fixture(checkpoint_id="001", value="hello"):
    return {"v": 1, "id": checkpoint_id, "ts": "2026-10-24T00:00:00Z",
            "channel_values": {"messages": value},
            "channel_versions": {"messages": 1}, "versions_seen": {}}


def test_unauthorized_delete_keeps_all_graph_stores(temp_db):
    mgr = SessionDatabaseManager(temp_db)
    saver = SqliteSaver(temp_db)
    mgr.record_message("owned", "user", "hello", "sre_alice")
    for worker in ("rag", "aiops"):
        config = {"configurable": {"thread_id": checkpoint_thread_id("sre_alice", "owned", worker)}}
        config = saver.put(config, checkpoint_fixture(), {"source": "input", "step": 0}, {"messages": 1})
        saver.put_writes(config, [("messages", "pending")], "task")
    assert mgr.delete_session("owned", "dev_bob") is False
    with sqlite3.connect(temp_db) as conn:
        assert conn.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 2
    assert mgr.delete_session("owned", "sre_alice") is True
    with sqlite3.connect(temp_db) as conn:
        for table in ("sessions", "messages", "checkpoints", "writes", "blobs"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_restart_and_pending_write_retry(temp_db):
    saver = SqliteSaver(temp_db)
    cfg = saver.put({"configurable": {"thread_id": "t"}}, checkpoint_fixture(),
                    {"source": "input", "step": 0}, {"messages": 1})
    saver.put_writes(cfg, [("messages", "first")], "task")
    saver.put_writes(cfg, [("messages", "retry")], "task")
    restored = SqliteSaver(temp_db).get_tuple(cfg)
    assert restored.checkpoint["channel_values"]["messages"] == "hello"
    assert restored.pending_writes == [("task", "messages", "first")]
    assert SessionDatabaseManager(temp_db).list_users()


def test_list_filter_namespace_before_and_zero_limit(temp_db):
    saver = SqliteSaver(temp_db)
    for checkpoint_id, namespace, source in (("001", "", "input"), ("002", "child", "loop")):
        cfg = {"configurable": {"thread_id": "t", "checkpoint_ns": namespace}}
        saver.put(cfg, checkpoint_fixture(checkpoint_id), {"source": source, "step": 0}, {"messages": 1})
    assert len(list(saver.list(None))) == 2
    assert len(list(saver.list({"configurable": {"thread_id": "t"}}))) == 2
    assert len(list(saver.list(None, filter={"source": "loop"}))) == 1
    assert list(saver.list(None, limit=0)) == []
    assert len(list(saver.list(None, before={"configurable": {"checkpoint_id": "002"}}))) == 1


@pytest.mark.asyncio
async def test_async_checkpoint_roundtrip(temp_db):
    saver = SqliteSaver(temp_db)
    cfg = await saver.aput({"configurable": {"thread_id": "async"}}, checkpoint_fixture(),
                           {"source": "input", "step": 0}, {"messages": 1})
    await saver.aput_writes(cfg, [("messages", "pending")], "task")
    assert (await saver.aget_tuple(cfg)).pending_writes
    assert len([item async for item in saver.alist(None)]) == 1
    await saver.adelete_thread("async")
    assert await saver.aget_tuple(cfg) is None


@pytest.fixture
def api_app(temp_db, monkeypatch):
    from fastapi import FastAPI
    from app.api import auth, sessions
    mgr = SessionDatabaseManager(temp_db)
    monkeypatch.setattr(auth, "session_db_manager", mgr)
    monkeypatch.setattr(sessions, "session_db_manager", mgr)
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
    return app, mgr


def test_browser_identities_and_api_ownership(api_app):
    from fastapi.testclient import TestClient
    app, mgr = api_app
    with TestClient(app) as alice, TestClient(app) as bob:
        assert alice.post("/api/auth/login", json={"username": "alice", "password": "123456"}).status_code == 200
        assert bob.post("/api/auth/login", json={"username": "bob", "password": "123456"}).status_code == 200
        assert alice.get("/api/auth/current").json()["data"]["user_id"] == "sre_alice"
        assert bob.get("/api/auth/current").json()["data"]["user_id"] == "dev_bob"
        assert alice.post("/api/sessions", json={"session_id": "owned"}).status_code == 200
        assert bob.get("/api/sessions").json()["data"]["sessions"] == []
        assert bob.get("/api/sessions/owned").status_code == 404
        assert bob.post("/api/sessions", json={"session_id": "owned"}).status_code == 404
        assert bob.put("/api/sessions/owned/title", json={"title": "bad"}).status_code == 404
        assert bob.delete("/api/sessions/owned").status_code == 404
        assert mgr.get_session("owned", "sre_alice") is not None
        assert alice.get("/api/sessions/missing").status_code == 404
        assert mgr.get_session("missing", "sre_alice") is None
        assert alice.get("/api/sessions/owned").json()["data"]["messages"] == []
        assert alice.get("/api/sessions", headers={"X-User-Id": "dev_bob"}).json()["data"]["user_id"] == "sre_alice"
        assert alice.delete("/api/sessions/owned").status_code == 200


def test_jwt_login_me_logout_and_bearer_isolation(api_app):
    """验证账号密码登录、HS256 JWT Token 签发、/api/auth/me 路由守卫与多租户 Bearer 隔离。"""
    from fastapi.testclient import TestClient
    app, mgr = api_app
    with TestClient(app) as client:
        # 1. 未登录访问 /api/auth/me 应返回 401
        assert client.get("/api/auth/me").status_code == 401

        # 2. 错误密码登录应返回 401
        bad_resp = client.post("/api/auth/login", json={"username": "alice", "password": "wrong_password"})
        assert bad_resp.status_code == 401

        # 3. 正确账号密码登录 Alice 与 Bob
        alice_login = client.post("/api/auth/login", json={"username": "alice", "password": "123456"})
        assert alice_login.status_code == 200
        alice_token = alice_login.json()["data"]["access_token"]
        assert alice_token.count(".") == 2

        bob_login = client.post("/api/auth/login", json={"username": "bob", "password": "123456"})
        assert bob_login.status_code == 200
        bob_token = bob_login.json()["data"]["access_token"]

        # 4. 使用各自的 Bearer Token 并发访问，即使共享同一客户端也不串号
        alice_headers = {"Authorization": f"Bearer {alice_token}"}
        bob_headers = {"Authorization": f"Bearer {bob_token}"}

        assert client.get("/api/auth/me", headers=alice_headers).json()["data"]["user_id"] == "sre_alice"
        assert client.get("/api/auth/me", headers=bob_headers).json()["data"]["user_id"] == "dev_bob"

        # 5. 篡改 Token 签名应被 401 拦截
        tampered_token = alice_token[:-4] + "xxxx"
        assert client.get("/api/sessions", headers={"Authorization": f"Bearer {tampered_token}"}).status_code == 401

        # 6. 退出登录清理 Cookie
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/auth/me").status_code == 401


@pytest.mark.asyncio
async def test_session_guard_rejects_delete_during_stream_and_releases():
    from fastapi import HTTPException
    from sse_starlette.sse import EventSourceResponse
    from app.api.session_guard import guard_session

    @guard_session
    async def stream(session_id, uid):
        return EventSourceResponse(iter([]))

    @guard_session
    async def delete(session_id, uid):
        return True

    response = await stream("busy", "sre_alice")
    with pytest.raises(HTTPException) as exc:
        await delete("busy", "sre_alice")
    assert exc.value.status_code == 409
    assert await delete("other", "sre_alice") is True

    async def fail_response(scope, receive, send):
        raise RuntimeError("disconnect")

    response.response = fail_response
    with pytest.raises(RuntimeError):
        await response({}, None, None)
    assert await delete("busy", "sre_alice") is True


@pytest.mark.asyncio
async def test_real_langgraph_resume_without_model_calls(temp_db):
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.graph import StateGraph, MessagesState, START, END

    def reply(state):
        turns = sum(isinstance(m, HumanMessage) for m in state["messages"])
        return {"messages": [AIMessage(content=f"turn={turns}")]}

    def build():
        graph = StateGraph(MessagesState)
        graph.add_node("reply", reply)
        graph.add_edge(START, "reply")
        graph.add_edge("reply", END)
        return graph.compile(checkpointer=SqliteSaver(temp_db))

    cfg = {"configurable": {"thread_id": checkpoint_thread_id("sre_alice", "chat", "rag")}}
    first = await build().ainvoke({"messages": [HumanMessage(content="one")]}, cfg)
    assert first["messages"][-1].content == "turn=1"
    second = await build().ainvoke({"messages": [HumanMessage(content="two")]}, cfg)
    assert second["messages"][-1].content == "turn=2"
    isolated = {"configurable": {"thread_id": checkpoint_thread_id("dev_bob", "chat", "rag")}}
    assert not (await build().aget_state(isolated)).values


def test_checkpoint_and_messages_survive_separate_python_processes(temp_db):
    import os
    from pathlib import Path
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent('''
        import asyncio
        import sys
        from langchain_core.messages import AIMessage, HumanMessage
        from langgraph.graph import StateGraph, MessagesState, START, END
        from app.storage.sqlite_saver import SqliteSaver
        from app.storage.session_db import SessionDatabaseManager, checkpoint_thread_id

        async def run():
            path, mode = sys.argv[1:]
            saver = SqliteSaver(path)
            db = SessionDatabaseManager(path)
            graph = StateGraph(MessagesState)
            graph.add_node("reply", lambda state: {"messages": [AIMessage(content="answer")]})
            graph.add_edge(START, "reply")
            graph.add_edge("reply", END)
            agent = graph.compile(checkpointer=saver)
            cfg = {"configurable": {"thread_id": checkpoint_thread_id("sre_alice", "restart", "rag")}}
            if mode == "write":
                db.record_message("restart", "user", "persist me", "sre_alice")
                await agent.ainvoke({"messages": [HumanMessage(content="first")]}, cfg)
            else:
                assert db.get_messages("restart", "sre_alice")[0].content == "persist me"
                state = await agent.ainvoke({"messages": [HumanMessage(content="second")]}, cfg)
                assert [m.content for m in state["messages"] if isinstance(m, HumanMessage)] == ["first", "second"]
                print("PROCESS_RESTART_OK")
        asyncio.run(run())
    ''')
    env = dict(os.environ, LANGSMITH_TRACING="false", LANGSMITH_TRACING_V2="false", PYTHONUTF8="1")
    for mode in ("write", "read"):
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", code, temp_db, mode],
                                cwd=Path(__file__).resolve().parents[1], env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        if mode == "read":
            assert "PROCESS_RESTART_OK" in result.stdout
