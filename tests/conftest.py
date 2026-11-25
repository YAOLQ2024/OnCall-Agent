"""测试默认禁用远程追踪，避免把测试数据发送到实际 LangSmith 项目。"""

import pytest
from langsmith import tracing_context


@pytest.fixture(autouse=True)
def disable_remote_tracing():
    with tracing_context(enabled=False):
        yield


@pytest.fixture(autouse=True)
def block_provider_network(monkeypatch):
    """Real HTTP transports must never charge model APIs during local regression tests."""
    import httpx

    def reject_request(*args, **kwargs):
        raise AssertionError("Real HTTP is disabled in tests; use MockTransport or a fake model")

    async def reject_async_request(*args, **kwargs):
        raise AssertionError("Real HTTP is disabled in tests; use MockTransport or a fake model")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_async_request)
