"""联网搜索工具 (Tavily) 单元测试

验证：
1. 工具元数据 (name, description, args)
2. 未配置 API Key 时的优雅降级输出
3. 模拟 Tavily API 成功响应的结构化格式提取 (answer, title, url, content)
4. 模拟公网空结果处理
5. 模拟超时、401/403 鉴权失败与网络异常的容错处理
6. search_web 本地工具在 LOCAL_TOOL_NAMES 中的防 MCP 覆盖保护
"""

import sys
import types
import unittest
from types import ModuleType
from unittest.mock import MagicMock, patch

# 构造通用 Mock 确保测试在任意轻量 Python 环境均可快速执行
for mod_name in [
    "langsmith", "loguru", "pydantic", "pydantic_settings",
    "langchain_core", "langchain_core.tools", "langchain_core.documents",
    "langchain_core.messages", "langchain.tools", "langchain", "langchain.agents",
    "langchain_mcp_adapters", "langchain_mcp_adapters.client", "langchain_mcp_adapters.tools",
    "mcp", "mcp.types",
    "app.core.tracing", "app.utils", "app.tools.knowledge_tool", "app.tools.time_tool"
]:
    if mod_name not in sys.modules:
        m = MagicMock()
        m.__path__ = []  # 标记为 package
        if mod_name == "langsmith":
            def fake_traceable(*args, **kwargs):
                def decorator(fn):
                    return fn
                return decorator
            m.traceable = fake_traceable
        # 让 tool 装饰器透明包装函数并自动附加 name、description、args 属性
        elif mod_name == "langchain_core.tools":
            def fake_tool(func=None, **kwargs):
                def decorator(fn):
                    fn.name = kwargs.get("name", fn.__name__)
                    fn.description = kwargs.get("description", fn.__doc__ or "")
                    import inspect
                    fn.args = {p: {} for p in inspect.signature(fn).parameters}
                    return fn
                return decorator(func) if callable(func) else decorator
            m.tool = fake_tool
        sys.modules[mod_name] = m

# Mock app.config 避免执行 pydantic 依赖加载
mock_config = MagicMock()
mock_config.tavily_api_key = "tvly-test-key-mock"
mock_config.tavily_api_base = "https://api.tavily.com"
mock_config.web_search_max_results = 5
mock_config.web_search_timeout = 15.0
mock_config.web_search_include_answer = True

mock_config_mod = ModuleType("app.config")
mock_config_mod.config = mock_config
mock_config_mod.Settings = MagicMock
sys.modules["app.config"] = mock_config_mod

# Mock httpx 异常体系
if "httpx" not in sys.modules:
    mock_httpx = MagicMock()
    class MockTimeoutException(Exception): pass
    class MockHTTPStatusError(Exception):
        def __init__(self, message="", request=None, response=None):
            super().__init__(message)
            self.response = response or MagicMock()
    mock_httpx.TimeoutException = MockTimeoutException
    mock_httpx.HTTPStatusError = MockHTTPStatusError
    sys.modules["httpx"] = mock_httpx

from app.tools.web_search_tool import search_web, perform_web_search
from app.agent.mcp_client import LOCAL_TOOL_NAMES


class TestWebSearchTool(unittest.TestCase):
    def test_tool_metadata(self):
        """验证工具的基本元数据与参数契约"""
        self.assertEqual(search_web.name, "search_web")
        self.assertIn("Tavily", search_web.description)
        self.assertIn("query", search_web.args)

    def test_missing_api_key_graceful_degradation(self):
        """未配置 API Key 时应返回友好引导提示，绝不抛出异常"""
        with patch.object(mock_config, "tavily_api_key", ""):
            result = perform_web_search(query="Kubernetes 1.30", api_key="")
            self.assertIn("[联网搜索未启用]", result)
            self.assertIn("TAVILY_API_KEY", result)

    @patch("app.tools.web_search_tool.httpx.Client")
    def test_successful_search_formatting(self, mock_client_cls):
        """测试 Tavily 正常返回时的 Markdown 结构化排版与链接提取"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "query": "Kubernetes 1.30 release notes",
            "answer": "Kubernetes 1.30 包含了 Structured Authorization 等重要特性。",
            "results": [
                {
                    "title": "Kubernetes v1.30: Uwubernetes",
                    "url": "https://kubernetes.io/blog/2024/04/17/kubernetes-v1-30-release/",
                    "content": "We are pleased to announce the release of Kubernetes v1.30...",
                    "score": 0.98,
                },
                {
                    "title": "K8s 1.30 核心特性解析",
                    "url": "https://example.com/k8s-130",
                    "content": "本次版本移除了废弃的存储插件并增强了安全特性。",
                    "score": 0.89,
                },
            ],
        }

        mock_instance = MagicMock()
        mock_instance.post.return_value = mock_response
        mock_client_cls.return_value.__enter__.return_value = mock_instance

        result = perform_web_search(query="Kubernetes 1.30", api_key="tvly-mock-key")

        self.assertIn("【Tavily 智能概括】", result)
        self.assertIn("Kubernetes v1.30: Uwubernetes", result)
        self.assertIn("https://kubernetes.io/blog/2024/04/17/kubernetes-v1-30-release/", result)
        self.assertIn("核心摘要", result)
        self.assertIn("找到 2 条公网参考资料", result)

    @patch("app.tools.web_search_tool.httpx.Client")
    def test_empty_results_handling(self, mock_client_cls):
        """测试公网未检索到结果时的友好提示"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "query": "some_super_obscure_query",
            "answer": "",
            "results": [],
        }

        mock_instance = MagicMock()
        mock_instance.post.return_value = mock_response
        mock_client_cls.return_value.__enter__.return_value = mock_instance

        result = perform_web_search(query="some_super_obscure_query", api_key="tvly-mock-key")
        self.assertIn("未检索到相关的公网网页内容", result)

    @patch("app.tools.web_search_tool.httpx.Client")
    def test_timeout_handling(self, mock_client_cls):
        """测试网络超时时的降级处理"""
        from app.tools.web_search_tool import httpx

        mock_instance = MagicMock()
        mock_instance.post.side_effect = httpx.TimeoutException("Connection timed out")
        mock_client_cls.return_value.__enter__.return_value = mock_instance

        result = perform_web_search(query="timeout query", api_key="tvly-mock-key")
        self.assertIn("[联网搜索超时]", result)

    @patch("app.tools.web_search_tool.httpx.Client")
    def test_auth_failure_handling(self, mock_client_cls):
        """测试 401/403 鉴权失败时的提示"""
        from app.tools.web_search_tool import httpx

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = '{"error": "Unauthorized"}'
        mock_error = httpx.HTTPStatusError("Unauthorized", request=MagicMock(), response=mock_response)

        mock_instance = MagicMock()
        mock_instance.post.return_value = mock_response
        mock_response.raise_for_status.side_effect = mock_error
        mock_client_cls.return_value.__enter__.return_value = mock_instance

        result = perform_web_search(query="auth fail query", api_key="tvly-bad-key")
        self.assertIn("[联网搜索鉴权失败]", result)

    def test_local_tool_protection(self):
        """确保 search_web 作为本地核心工具被 LOCAL_TOOL_NAMES 保护，防外部 MCP 覆盖"""
        self.assertIn("search_web", LOCAL_TOOL_NAMES)

    def test_citations_extraction_and_appending(self):
        """测试公网网页溯源提取 (extract_web_references)"""
        from app.tools.web_search_tool import extract_web_references

        sample_web_output = (
            "找到 2 条公网参考资料：\n\n"
            "[1] Kubernetes v1.30 Release (相关度: 0.98)\n"
            "    来源链接: https://kubernetes.io/blog/v1.30\n"
            "    核心摘要: K8s 1.30 发布了...\n\n"
            "[2] K8s 1.30 特性盘点\n"
            "    来源链接: https://example.com/k8s\n"
            "    核心摘要: 详情如下...\n"
        )
        web_refs = extract_web_references(sample_web_output)
        self.assertEqual(len(web_refs), 2)
        self.assertEqual(web_refs[0]["title"], "Kubernetes v1.30 Release")
        self.assertEqual(web_refs[0]["url"], "https://kubernetes.io/blog/v1.30")
        self.assertEqual(web_refs[1]["title"], "K8s 1.30 特性盘点")
        self.assertEqual(web_refs[1]["url"], "https://example.com/k8s")


if __name__ == "__main__":
    unittest.main()
