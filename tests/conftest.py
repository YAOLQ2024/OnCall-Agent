"""测试默认禁用远程追踪，避免把测试数据发送到实际 LangSmith 项目。"""

import pytest
from langsmith import tracing_context


@pytest.fixture(autouse=True)
def disable_remote_tracing():
    with tracing_context(enabled=False):
        yield
