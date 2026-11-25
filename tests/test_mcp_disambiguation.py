"""
MCP 工具智能冲突消歧单元测试 (Option A)
验证：
1. 全局唯一工具保留简洁短名
2. 跨服务同名工具自动添加 {safe_server_name}__{original_name} 前缀与描述说明
3. 与本地工具冲突时自动消歧，避免覆盖本地基础工具
4. 特殊服务名安全字符转换 (如 my-local-ops -> my_local_ops)
5. 复制工具的不变性与独立性
"""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

# 确保在任意环境下均可独立执行测试
sys.modules.setdefault("langchain_mcp_adapters", MagicMock())
sys.modules.setdefault("langchain_mcp_adapters.client", MagicMock())
sys.modules.setdefault("langchain_mcp_adapters.tools", MagicMock())
sys.modules.setdefault("langsmith", MagicMock())
sys.modules.setdefault("loguru", MagicMock())
sys.modules.setdefault("mcp", MagicMock())
sys.modules.setdefault("mcp.types", MagicMock())
sys.modules.setdefault("app.core.tracing", MagicMock())
sys.modules.setdefault("app.config", MagicMock())

from app.agent.mcp_client import disambiguate_tools, LOCAL_TOOL_NAMES, _clone_tool_with_name


def make_tool(name: str, description: str = "demo tool"):
    return SimpleNamespace(name=name, description=description, args={})


class TestMCPDisambiguation(unittest.TestCase):
    def test_unique_tools_remain_unchanged(self):
        tools_by_server = {
            "cls": [make_tool("search_log"), make_tool("get_topic")],
            "monitor": [make_tool("query_cpu_metrics"), make_tool("query_memory_metrics")],
            "weather": [make_tool("get_current_weather")],
        }
        flat, by_server = disambiguate_tools(tools_by_server, LOCAL_TOOL_NAMES)

        self.assertEqual(len(flat), 5)
        self.assertEqual(
            [t.name for t in flat],
            ["search_log", "get_topic", "query_cpu_metrics", "query_memory_metrics", "get_current_weather"],
        )
        self.assertEqual([t.name for t in by_server["cls"]], ["search_log", "get_topic"])
        self.assertEqual([t.name for t in by_server["monitor"]], ["query_cpu_metrics", "query_memory_metrics"])
        self.assertEqual([t.name for t in by_server["weather"]], ["get_current_weather"])

    def test_cross_server_collision_disambiguates_only_colliding_tools(self):
        tools_by_server = {
            "monitor": [
                make_tool("query_cpu_metrics", "Monitor 系统 CPU 监控"),
                make_tool("query_memory_metrics", "Monitor 内存监控"),
            ],
            "my-local-ops": [
                make_tool("query_cpu_metrics", "自定义本地 CPU 监控"),
                make_tool("restart_pod", "重启 Pod"),
            ],
            "weather": [
                make_tool("get_current_weather", "查询天气"),
            ],
        }
        flat, by_server = disambiguate_tools(tools_by_server, LOCAL_TOOL_NAMES)

        # 唯一工具保留原名
        self.assertEqual(by_server["monitor"][1].name, "query_memory_metrics")
        self.assertEqual(by_server["my-local-ops"][1].name, "restart_pod")
        self.assertEqual(by_server["weather"][0].name, "get_current_weather")

        # 冲突工具被前缀消歧，且中划线被转换为合法下划线
        self.assertEqual(by_server["monitor"][0].name, "monitor__query_cpu_metrics")
        self.assertIn("[来源服务: monitor]", by_server["monitor"][0].description)

        self.assertEqual(by_server["my-local-ops"][0].name, "my_local_ops__query_cpu_metrics")
        self.assertIn("[来源服务: my-local-ops]", by_server["my-local-ops"][0].description)

        # 所有输出的工具名必须全局唯一，杜绝 LLM 语法编译器死锁
        all_names = [t.name for t in flat]
        self.assertEqual(len(all_names), len(set(all_names)))
        self.assertIn("monitor__query_cpu_metrics", all_names)
        self.assertIn("my_local_ops__query_cpu_metrics", all_names)

    def test_collision_with_reserved_local_tool(self):
        tools_by_server = {
            "custom-server": [
                make_tool("get_current_time", "自定义时间工具"),
                make_tool("custom_action", "自定义动作"),
            ]
        }
        flat, by_server = disambiguate_tools(tools_by_server, LOCAL_TOOL_NAMES)

        # get_current_time 与本地保留工具冲突，必须被前缀消歧
        self.assertEqual(by_server["custom-server"][0].name, "custom_server__get_current_time")
        self.assertIn("[来源服务: custom-server]", by_server["custom-server"][0].description)

        # 非冲突工具保留短名
        self.assertEqual(by_server["custom-server"][1].name, "custom_action")

    def test_clone_tool_with_pydantic_or_generic(self):
        class DummyPydantic:
            def __init__(self, name, description):
                self.name = name
                self.description = description

            def model_copy(self, update=None):
                update = update or {}
                return DummyPydantic(
                    name=update.get("name", self.name),
                    description=update.get("description", self.description),
                )

        t1 = DummyPydantic("test_tool", "desc")
        t2 = _clone_tool_with_name(t1, "renamed_tool", "new_desc")
        self.assertEqual(t2.name, "renamed_tool")
        self.assertEqual(t2.description, "new_desc")
        self.assertEqual(t1.name, "test_tool")  # 保证不可变性

    def test_warning_logged_only_once_on_repeated_calls(self):
        from unittest.mock import patch
        from app.agent.mcp_client import _logged_collision_signatures

        # 清除之前的已记录特征以便精确测试
        _logged_collision_signatures.clear()

        tools_by_server = {
            "s1": [make_tool("dup_tool", "desc1")],
            "s2": [make_tool("dup_tool", "desc2")],
        }

        with patch("app.agent.mcp_client.logger.warning") as mock_warn:
            # 第一次调用，应当记录并打印 warning
            disambiguate_tools(tools_by_server, LOCAL_TOOL_NAMES)
            self.assertEqual(mock_warn.call_count, 2)  # s1 与 s2 各一条

            # 第二次调用（模拟前端每 10s 轮询 /api/tools/status 或 snapshot）
            disambiguate_tools(tools_by_server, LOCAL_TOOL_NAMES)
            # call_count 依然保持为 2，不再重复打印刷屏！
            self.assertEqual(mock_warn.call_count, 2)

            # 第三次调用
            disambiguate_tools(tools_by_server, LOCAL_TOOL_NAMES)
            self.assertEqual(mock_warn.call_count, 2)


if __name__ == "__main__":
    unittest.main()
