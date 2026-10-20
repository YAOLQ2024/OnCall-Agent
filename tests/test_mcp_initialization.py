"""MCP 自动托管、按服务降级和恢复；不启动子进程，不调用真实模型。"""

import asyncio
import inspect
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import anyio
from langchain_core.messages import AIMessage
from langsmith import traceable, tracing_context
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from app.agent.mcp_client import MCPToolRegistry
from app.config import Settings


@pytest.fixture(autouse=True)
async def close_test_registries(monkeypatch):
    registries = []
    original = MCPToolRegistry.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        registries.append(self)

    monkeypatch.setattr(MCPToolRegistry, "__init__", initialize)
    yield
    for registry in registries:
        await registry.close()


def tool(name):
    return SimpleNamespace(name=name, description="test tool", args={})


def registry_with(discover, **settings):
    @asynccontextmanager
    async def session(server_name):
        yield SimpleNamespace(server_name=server_name, send_ping=AsyncMock())

    async def load_tools(session, *, server_name, **kwargs):
        result = discover(server_name=server_name)
        return await result if inspect.isawaitable(result) else result

    client = SimpleNamespace(
        session=session, callbacks=None, tool_interceptors=[], tool_name_prefix=False
    )
    registry = MCPToolRegistry(
        Settings(_env_file=None, **settings), Mock(return_value=client), tool_loader=load_tools
    )
    return registry, client


async def offline(**kwargs):
    raise ConnectionError("offline")


def test_local_defaults_use_framework_managed_stdio():
    settings = Settings(_env_file=None)
    for name, connection in settings.mcp_servers.items():
        assert connection["transport"] == "stdio"
        assert connection["command"] == sys.executable
        assert connection["args"][-1] == "--stdio"
        assert connection["args"][-2].endswith(f"{name}_server.py")


def test_external_http_is_preserved_and_local_management_can_be_disabled():
    settings = Settings(_env_file=None, mcp_cls_url="https://mcp.example.invalid/mcp")
    assert settings.mcp_servers["cls"]["transport"] == "streamable-http"
    external = Settings(_env_file=None, mcp_auto_start=False)
    assert all(connection["transport"] == "streamable-http" for connection in external.mcp_servers.values())


def test_invalid_mcp_url_does_not_prevent_local_tools_initialization():
    settings = Settings(_env_file=None, mcp_cls_url="http://localhost:invalid/mcp")
    registry = MCPToolRegistry(settings, Mock())
    assert registry.snapshot() == (0, [])
    assert settings.mcp_servers["cls"]["transport"] == "streamable-http"


@pytest.mark.asyncio
async def test_partial_discovery_keeps_healthy_server_and_reports_leaf_error():
    async def discover(*, server_name):
        if server_name == "cls":
            raise ExceptionGroup("task group", [ConnectionError("connection refused")])
        return [tool("monitor_status")]

    registry, _ = registry_with(discover)
    await registry.refresh()
    assert [item.name for item in registry.snapshot()[1]] == ["monitor_status"]
    states = {item["name"]: item for item in registry.status()["servers"]}
    assert states["cls"]["status"] == "unavailable"
    assert "ConnectionError: connection refused" in states["cls"]["last_error"]
    assert states["monitor"]["status"] == "available"
    assert registry.snapshot()[1][0].handle_tool_error is True


@pytest.mark.asyncio
async def test_discovery_failure_does_not_block_knowledge_chat(monkeypatch):
    from app.services import rag_agent_service as module

    registry, _ = registry_with(offline)
    await registry.refresh()
    graph = SimpleNamespace(ainvoke=AsyncMock(return_value={"messages": [AIMessage(content="知识库回答")]}))
    build = Mock(return_value=graph)
    monkeypatch.setattr(module, "mcp_registry", registry)
    monkeypatch.setattr(module.llm_factory, "create_chat_model", Mock(return_value=object()))
    monkeypatch.setattr(module, "create_agent", build)
    service = module.RagAgentService()
    assert await service.query("Pod Pending", "test-degraded") == "知识库回答"
    assert {item.name for item in build.call_args.kwargs["tools"]} == {
        "retrieve_knowledge", "get_current_time",
    }
    assert "尚不可用" in graph.ainvoke.call_args.kwargs["input"]["messages"][0].content


@pytest.mark.asyncio
async def test_degraded_knowledge_chat_also_supports_streaming(monkeypatch):
    from app.services import rag_agent_service as module

    registry, _ = registry_with(offline)
    await registry.refresh()

    async def stream(**kwargs):
        yield AIMessage(content="知识库回答"), {"langgraph_node": "model"}

    monkeypatch.setattr(module, "mcp_registry", registry)
    monkeypatch.setattr(module.llm_factory, "create_chat_model", Mock(return_value=object()))
    monkeypatch.setattr(module, "create_agent", Mock(return_value=SimpleNamespace(astream=stream)))
    events = [event async for event in module.RagAgentService().query_stream("Pod Pending", "test-stream")]
    assert any(event.get("data") == "知识库回答" for event in events)
    assert events[-1]["type"] == "complete"


@pytest.mark.asyncio
async def test_agent_initialization_never_waits_for_discovery(monkeypatch):
    from app.services import rag_agent_service as module

    factory = Mock(side_effect=AssertionError("Agent 初始化不应创建 MCP 连接"))
    registry = MCPToolRegistry(Settings(_env_file=None), factory)
    monkeypatch.setattr(module, "mcp_registry", registry)
    monkeypatch.setattr(module.llm_factory, "create_chat_model", Mock(return_value=object()))
    monkeypatch.setattr(module, "create_agent", Mock(return_value=object()))
    await module.RagAgentService()._initialize_agent()
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_rebuilds_agent_tools_and_keeps_checkpointer(monkeypatch):
    from app.services import rag_agent_service as module

    online = False

    async def discover(*, server_name):
        if not online:
            raise ConnectionError("offline")
        return [tool(f"{server_name}_query")]

    registry, _ = registry_with(discover)
    await registry.refresh()
    build = Mock(side_effect=[object(), object()])
    monkeypatch.setattr(module, "mcp_registry", registry)
    monkeypatch.setattr(module.llm_factory, "create_chat_model", Mock(return_value=object()))
    monkeypatch.setattr(module, "create_agent", build)
    service = module.RagAgentService()
    first = await service._initialize_agent()
    checkpointer = build.call_args.kwargs["checkpointer"]
    online = True
    await registry.refresh(force=True)
    second = await service._initialize_agent()
    assert first is not second
    assert build.call_args.kwargs["checkpointer"] is checkpointer
    assert {item.name for item in build.call_args.kwargs["tools"]} == {
        "retrieve_knowledge", "get_current_time", "cls_query", "monitor_query",
    }


@pytest.mark.asyncio
async def test_runtime_disconnect_removes_only_failed_service():
    registry, _ = registry_with(lambda *, server_name: [tool(f"{server_name}_query")])
    await registry.refresh()
    result = await registry._invoke_tool(
        SimpleNamespace(server_name="cls"), AsyncMock(side_effect=ConnectionError("lost connection"))
    )
    assert result.isError
    assert [item.name for item in registry.snapshot()[1]] == ["monitor_query"]
    states = {item["name"]: item for item in registry.status()["servers"]}
    assert states["cls"]["tools"] == ["cls_query"]


@pytest.mark.asyncio
async def test_discovery_timeout_and_background_shutdown():
    async def discover(**kwargs):
        await asyncio.Event().wait()

    registry, _ = registry_with(discover, mcp_discovery_timeout=0.01)
    await registry.refresh()
    assert all(item["status"] == "unavailable" for item in registry.status()["servers"])
    await registry.start()
    worker = registry._worker
    await registry.close()
    assert worker.done()


@pytest.mark.asyncio
async def test_failed_discovery_is_visible_in_langsmith(monkeypatch):
    registry, _ = registry_with(offline)
    runs = []
    children = []
    original = registry._discover

    async def capture(server_name):
        return await original(server_name, langsmith_extra={"on_end": children.append})

    monkeypatch.setattr(registry, "_discover", capture)

    @traceable(name="test.mcp_discovery")
    async def discover():
        await registry.refresh()

    with tracing_context(enabled="local"):
        await discover(langsmith_extra={"on_end": runs.append})
    assert len(children) == 2
    assert all(item.parent_run_id == runs[0].id for item in children)
    assert {item.inputs["server_name"] for item in children} == {"cls", "monitor"}
    assert all("offline" in item.error for item in children)


@pytest.mark.asyncio
async def test_status_api_reads_snapshot_and_refresh_does_not_wait(monkeypatch):
    from app.api import tools

    registry = SimpleNamespace(
        status=Mock(return_value={"servers": []}), start=AsyncMock(), request_refresh=Mock()
    )
    monkeypatch.setattr(tools, "mcp_registry", registry)
    assert (await tools.tools_status())["data"] == {"servers": []}
    response = await tools.refresh_tools()
    assert response.status_code == 202
    registry.request_refresh.assert_called_once()


def native_session_registry(**settings):
    """用真实适配器和 AnyIO 上下文验证复用，替换网络/子进程为内存会话。"""
    sessions = {"cls": [], "monitor": []}
    opened, closed, owners = [], [], {}

    @asynccontextmanager
    async def session(name):
        owner = asyncio.current_task()
        # 模拟 MCP SDK 的嵌套取消作用域，交错关闭或跨任务关闭都会失败。
        async with anyio.create_task_group() as group:
            current = SimpleNamespace(
                list_tools=AsyncMock(return_value=ListToolsResult(tools=[Tool(
                    name=f"{name}_read", description="test", inputSchema={"type": "object", "properties": {}}
                )])),
                call_tool=AsyncMock(return_value=CallToolResult(content=[TextContent(type="text", text="ok")])),
                send_ping=AsyncMock(),
            )
            sessions[name].append(current)
            opened.append(name)
            owners[name] = owner
            try:
                yield current
            finally:
                assert asyncio.current_task() is owner
                closed.append(name)
                group.cancel_scope.cancel()

    client = SimpleNamespace(session=session, callbacks=None, tool_name_prefix=False)
    registry = MCPToolRegistry(Settings(_env_file=None, **settings), Mock(return_value=client))
    return registry, sessions, opened, closed, owners


@pytest.mark.asyncio
async def test_native_tools_reuse_session_and_shutdown_closes_in_owner_tasks():
    registry, sessions, opened, closed, owners = native_session_registry()
    await registry.refresh()
    tools = {item.name: item for item in registry.snapshot()[1]}
    await tools["cls_read"].ainvoke({})
    await tools["cls_read"].ainvoke({})
    assert sessions["cls"][0].call_tool.await_count == 2
    assert len(sessions["cls"]) == len(sessions["monitor"]) == 1
    assert owners["cls"] is not owners["monitor"]
    assert not closed
    await registry.close()
    assert sorted(opened) == sorted(closed) == ["cls", "monitor"]
    assert not registry._sessions


@pytest.mark.asyncio
async def test_force_refresh_reuses_healthy_session_and_replaces_tool_handles():
    registry, sessions, _, closed, _ = native_session_registry()
    await registry.refresh()
    previous = {item.name: item for item in registry.snapshot()[1]}
    version = registry.version
    await registry.refresh(force=True)
    current = {item.name: item for item in registry.snapshot()[1]}
    assert current["cls_read"] is not previous["cls_read"]
    assert registry.version > version
    assert len(sessions["cls"]) == len(sessions["monitor"]) == 1
    assert not closed
    await current["cls_read"].ainvoke({})
    assert sessions["cls"][0].call_tool.await_count == 1


@pytest.mark.asyncio
async def test_reconnect_replaces_only_failed_session_and_rejects_stale_tools():
    registry, sessions, _, closed, _ = native_session_registry()
    await registry.refresh()
    previous = {item.name: item for item in registry.snapshot()[1]}
    sessions["cls"][0].call_tool.side_effect = ConnectionError("disconnected")
    await previous["cls_read"].ainvoke({})
    assert [item.name for item in registry.snapshot()[1]] == ["monitor_read"]
    await registry.refresh()
    current = {item.name: item for item in registry.snapshot()[1]}
    assert len(sessions["cls"]) == 2
    assert len(sessions["monitor"]) == 1
    assert closed == ["cls"]
    await previous["cls_read"].ainvoke({})
    assert all(state["status"] == "available" for state in registry.status()["servers"])
    await current["cls_read"].ainvoke({})
    assert sessions["cls"][1].call_tool.await_count == 1


@pytest.mark.asyncio
async def test_heartbeat_detects_idle_disconnect_and_schedules_recovery(monkeypatch):
    registry, sessions, _, _, _ = native_session_registry(mcp_refresh_interval=0.01)
    await registry.refresh()
    failed = asyncio.Event()
    publish = registry._publish

    def capture(name, tools, error=""):
        publish(name, tools, error)
        if name == "cls" and error:
            failed.set()

    monkeypatch.setattr(registry, "_publish", capture)
    sessions["cls"][0].send_ping.side_effect = ConnectionError("idle disconnect")
    await asyncio.wait_for(failed.wait(), timeout=1)
    assert "cls" in registry._reconnect_requested
    await registry.refresh()
    assert len(sessions["cls"]) == 2
    assert len(sessions["monitor"]) == 1


@pytest.mark.asyncio
async def test_late_error_from_old_session_does_not_disable_recovered_session():
    registry, _, _, _, _ = native_session_registry()
    await registry.refresh()
    owner = registry._sessions["cls"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_failure(request):
        entered.set()
        await release.wait()
        raise ConnectionError("late old-session error")

    pending = asyncio.create_task(registry._invoke_tool(
        SimpleNamespace(server_name="cls"), delayed_failure, owner=owner
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        registry._publish("cls", [], "connection lost")
        registry._reconnect_requested.add("cls")
        await registry.refresh()
    finally:
        release.set()
        await pending
    assert registry._sessions["cls"] is not owner
    assert all(state["status"] == "available" for state in registry.status()["servers"])
