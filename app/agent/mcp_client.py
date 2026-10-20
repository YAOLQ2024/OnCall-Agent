"""MCP 工具后台发现与可用性管理；知识库工具不依赖 MCP 连接。"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langsmith import traceable
from loguru import logger
from mcp.types import CallToolResult, TextContent

from app.config import Settings, config


def _exception_details(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(dict.fromkeys(_exception_details(item) for item in error.exceptions))
    return f"{type(error).__name__}: {error}"


def _tool_trace_output(tools: list | None) -> dict:
    return {"tools": [
        {"name": tool.name, "description": tool.description, "args": tool.args}
        for tool in tools or []
    ]}


@dataclass
class _ManagedSession:
    ready: asyncio.Future
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    session: object | None = None


class MCPToolRegistry:
    """缓存可用工具；后台发现失败、超时及调用断连仅隔离对应服务。"""

    def __init__(
        self,
        settings: Settings = config,
        client_factory: Callable = MultiServerMCPClient,
        tool_loader: Callable[..., Awaitable[list]] = load_mcp_tools,
    ):
        self.settings = settings
        self._connections = settings.mcp_servers
        self._client_factory = client_factory
        self._tool_loader = tool_loader
        self._client = None
        # 每个服务有独立的会话拥有者任务，SDK 上下文在同一任务内成对进出。
        # 不能在一个任务里交错进入多个 AnyIO 会话后再按任意顺序关闭。
        self._sessions: dict[str, _ManagedSession] = {}
        self._tools: dict[str, list] = {name: [] for name in self._connections}
        self._states = {
            name: {
                "name": name,
                "label": {"cls": "CLS 日志工具", "monitor": "Monitor 监控工具"}.get(name, name),
                "status": "checking", "transport": connection["transport"],
                "tools": [], "last_error": "", "last_checked": None,
            }
            for name, connection in self._connections.items()
        }
        self.version = 0
        self._refresh_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._force_refresh = False
        self._reconnect_requested: set[str] = set()
        self._stopping = False
        self._worker: asyncio.Task | None = None

    def snapshot(self) -> tuple[int, list]:
        """同步读取快照，不等待连接或探测；每次调用返回独立列表。"""
        return self.version, [tool for tools in self._tools.values() for tool in tools]

    def status(self) -> dict:
        return {
            "local_tools": ["retrieve_knowledge", "get_current_time"],
            "refreshing": self._refresh_lock.locked(),
            "auto_start": self.settings.mcp_auto_start,
            "servers": [{**state, "tools": list(state["tools"])} for state in self._states.values()],
        }

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._stopping = False
            self._wake = asyncio.Event()
            self._force_refresh = True
            self._worker = asyncio.create_task(self._run(), name="mcp-tool-discovery")

    def availability_message(self) -> str:
        missing = [state["label"] for state in self._states.values() if state["status"] != "available"]
        if not missing:
            return ""
        return (
            "当前尚不可用的能力：" + "、".join(missing) + "。"
            "知识库检索和时间工具仍可使用。涉及缺失能力的实时日志、指标时，"
            "必须说明无法获取；知识库中的处理建议不能当成已经查询到的实时数据。"
        )

    def request_refresh(self) -> None:
        self._force_refresh = True
        self._wake.set()

    async def close(self) -> None:
        self._stopping = True
        if self._worker is not None:
            self._wake.set()
            await self._worker
            self._worker = None
        else:
            for name in list(self._sessions):
                await self._close_session(name)

    async def _run(self) -> None:
        try:
            while not self._stopping:
                self._wake.clear()
                force = self._force_refresh
                self._force_refresh = False
                try:
                    await self.refresh(force=force)
                except Exception as error:
                    logger.error("MCP 后台发现失败，知识库问答继续可用：{}", _exception_details(error))
                if self._stopping:
                    break
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.settings.mcp_refresh_interval)
                except TimeoutError:
                    pass
        finally:
            for name in list(self._sessions):
                await self._close_session(name)

    @staticmethod
    def _signature(tools: list) -> str:
        return json.dumps(
            [(tool.name, tool.description, tool.args) for tool in tools],
            ensure_ascii=False, sort_keys=True, default=str,
        )

    def _publish(self, name: str, tools: list, error: str = "") -> None:
        previous = self._tools[name]
        if self._signature(previous) != self._signature(tools) or any(
            old is not new for old, new in zip(previous, tools)
        ):
            self.version += 1
        # 即使 schema 相同，也必须替换工具对象：对象可能绑定了不同的 session。
        self._tools[name] = list(tools)
        state = self._states[name]
        state.update(
            status="unavailable" if error else "available", last_error=error,
            last_checked=datetime.now(timezone.utc).isoformat(),
        )
        if not error:
            state["tools"] = [tool.name for tool in tools]

    async def refresh(self, *, force: bool = False) -> None:
        async with self._refresh_lock:
            if self._client is None:
                try:
                    self._client = self._client_factory(self._connections)
                except Exception as error:
                    for name in self._connections:
                        self._publish(name, [], _exception_details(error))
                    return
            reconnect = set(self._reconnect_requested)
            self._reconnect_requested.difference_update(reconnect)
            for name in reconnect:
                await self._close_session(name)

            # 健康会话持续复用；手动刷新只重新读取工具列表，不重启进程。
            names = [
                name for name in self._connections
                if force or name in reconnect or self._states[name]["status"] != "available"
            ]
            for name in names:
                try:
                    await self._discover(name)
                except Exception:
                    # 某一个服务故障不影响其余 MCP 服务和知识库。
                    continue

    async def _close_session(self, server_name: str) -> None:
        managed = self._sessions.pop(server_name, None)
        if managed is not None:
            self._publish(server_name, [], "MCP 会话已关闭")
            managed.stop.set()
            connected = (
                managed.ready.done() and not managed.ready.cancelled()
                and managed.ready.exception() is None
            )
            if managed.task is None:
                return
            if not connected:
                managed.task.cancel()
            try:
                await managed.task
            except asyncio.CancelledError:
                pass
            except Exception as error:
                logger.warning("关闭 MCP 服务 {} 会话失败：{}", server_name, _exception_details(error))

    async def _read_tools(self, server_name: str, managed: _ManagedSession) -> list:
        async def invoke(request, handler):
            return await self._invoke_tool(request, handler, owner=managed)

        return await self._tool_loader(
            managed.session,
            callbacks=self._client.callbacks,
            tool_interceptors=[invoke],
            server_name=server_name,
            tool_name_prefix=self._client.tool_name_prefix,
        )

    async def _hold_session(self, server_name: str, managed: _ManagedSession) -> None:
        """SDK 会话的创建、持续运行和退出均在此任务中完成。"""
        try:
            async with self._client.session(server_name) as session:
                managed.session = session
                tools = await self._read_tools(server_name, managed)
                if not managed.ready.done():
                    managed.ready.set_result(tools)
                while not managed.stop.is_set():
                    try:
                        await asyncio.wait_for(
                            managed.stop.wait(), timeout=self.settings.mcp_refresh_interval
                        )
                    except TimeoutError:
                        # 心跳只复用现有会话；不重新启动健康的 stdio 子进程。
                        await asyncio.wait_for(
                            session.send_ping(), timeout=self.settings.mcp_discovery_timeout
                        )
                        self._states[server_name]["last_checked"] = datetime.now(timezone.utc).isoformat()
        except (Exception, asyncio.CancelledError) as error:
            established = (
                managed.ready.done() and not managed.ready.cancelled()
                and managed.ready.exception() is None
            )
            if not managed.ready.done():
                managed.ready.set_exception(
                    error if isinstance(error, Exception) else RuntimeError("MCP 会话意外取消")
                )
            if established and not managed.stop.is_set() and not self._stopping:
                message = _exception_details(error)
                self._publish(server_name, [], message)
                self._reconnect_requested.add(server_name)
                self._wake.set()
                logger.warning("MCP 服务 {} 会话中断，已隔离并安排重连：{}", server_name, message)

    @traceable(name="agent.load_mcp_tools", run_type="chain", process_outputs=_tool_trace_output)
    async def _discover(self, server_name: str) -> list:
        try:
            managed = self._sessions.get(server_name)
            if managed is None or managed.task is None or managed.task.done():
                await self._close_session(server_name)
                managed = _ManagedSession(asyncio.get_running_loop().create_future())
                self._sessions[server_name] = managed
                managed.task = asyncio.create_task(
                    self._hold_session(server_name, managed), name=f"mcp-session-{server_name}"
                )
                tools = await asyncio.wait_for(
                    managed.ready, timeout=self.settings.mcp_discovery_timeout
                )
            else:
                tools = await asyncio.wait_for(
                    self._read_tools(server_name, managed), timeout=self.settings.mcp_discovery_timeout
                )
            for tool in tools:
                # MCP 的 isError 会被适配器转换为 ToolException。
                # 用框架原生错误消息交回模型，避免中断整个 Agent。
                tool.handle_tool_error = True
            self._publish(server_name, tools)
            logger.info("MCP 服务 {} 已就绪，工具 {} 个", server_name, len(tools))
            return tools
        except Exception as error:
            await self._close_session(server_name)
            message = _exception_details(error)
            self._publish(server_name, [], message)
            logger.warning("MCP 服务 {} 不可用，知识库问答不受影响：{}", server_name, message)
            # 单个失败节点在 trace 中保留实际错误；外层负责降级。
            raise

    async def _invoke_tool(self, request, handler, *, owner: _ManagedSession | None = None) -> CallToolResult:
        name = request.server_name
        if owner is not None and self._sessions.get(name) is not owner:
            return CallToolResult(isError=True, content=[TextContent(
                type="text", text=f"MCP 服务 {name} 的旧会话已失效，请在后续请求中使用恢复后的工具。"
            )])
        if self._states[name]["status"] != "available":
            return CallToolResult(isError=True, content=[TextContent(
                type="text", text=f"MCP 服务 {name} 当前不可用；请说明无法获取该实时数据，不要编造结果。"
            )])
        try:
            return await asyncio.wait_for(handler(request), timeout=self.settings.mcp_tool_timeout)
        except Exception as error:
            message = _exception_details(error)
            # 并行调用中的旧会话可能在新会话就绪后才报错，不能再次隔离新连接。
            if owner is None or self._sessions.get(name) is owner:
                self._publish(name, [], message)
                self._reconnect_requested.add(name)
                self._wake.set()
            return CallToolResult(isError=True, content=[TextContent(
                type="text", text=f"MCP 服务 {name} 调用失败：{message}。知识库仍可使用，请勿编造实时数据。"
            )])


mcp_registry = MCPToolRegistry()
