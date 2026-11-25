"""配置管理模块

使用 Pydantic Settings 实现类型安全的配置管理
"""

from typing import Dict, Any, Optional
from pathlib import Path
import json
import sys
from urllib.parse import urlsplit
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 应用配置
    app_name: str = "OnCall Aegnt"
    app_version: str = "1.0.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 9900

    # LangSmith 追踪配置。启动时导出给 LangChain/LangSmith SDK，不记录密钥。
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_project: str = "OncallAgent"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_workspace_id: str = ""
    langsmith_hide_inputs: bool = False
    langsmith_hide_outputs: bool = False

    # Empty uses a randomly generated key persisted in the local session database.
    oncall_jwt_secret: SecretStr = SecretStr("")

    # Manual management remains available independently of runtime recall.
    ltm_enabled: bool = False
    ltm_auto_extract_enabled: bool = False
    ltm_extract_model: str = "qwen3.8-flash"
    ltm_extract_timeout_seconds: float = Field(default=15, gt=0, le=120)
    ltm_extract_input_chars: int = Field(default=6_000, ge=500, le=64_000)
    ltm_extract_output_tokens: int = Field(default=1_000, ge=128, le=8_000)
    ltm_worker_concurrency: int = Field(default=1, ge=1, le=4)
    ltm_job_lease_seconds: int = Field(default=60, ge=10, le=600)
    ltm_job_max_attempts: int = Field(default=2, ge=1, le=2)
    ltm_daily_input_token_budget: int = Field(default=0, ge=0)
    ltm_daily_output_token_budget: int = Field(default=0, ge=0)
    ltm_vector_enabled: bool = False
    ltm_paid_calls_enabled: bool = False
    ltm_collection_name: str = Field(default="oncall_long_term_incidents", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    ltm_daily_embedding_item_budget: int = Field(default=0, ge=0)
    ltm_recall_top_k: int = Field(default=3, ge=1, le=10)
    ltm_candidates_per_route: int = Field(default=10, ge=1, le=30)
    ltm_context_estimated_tokens: int = Field(default=32_768, ge=100, le=1_000_000)
    ltm_recall_timeout_seconds: float = Field(default=2, gt=0, le=10)

    # Budgets for the model view of existing conversation and diagnostic evidence.
    chat_history_estimated_tokens: int = Field(default=64_000, ge=1_000, le=1_000_000)
    aiops_replan_step_max_chars: int = Field(default=24_000, ge=1_000, le=256_000)

    # 千问平台 OpenAI 兼容接口配置
    qwen_api_key: str = ""
    qwen_api_base: str = "https://maas.qianwenaiapi.com/compatible-mode/v1"
    qwen_chat_model: str = "qwen3.8-max"
    supervisor_model: str = "qwen3.8-flash"
    qwen_embedding_model: str = "qwen3.7-text-embedding-flash"
    qwen_embedding_dimensions: int = 1024
    qwen_rerank_model: str = "qwen3.7-text-rerank"
    qwen_rerank_url: str = "https://maas.qianwenaiapi.com/api/v1/services/rerank/text-rerank/text-rerank"
    qwen_rerank_timeout: float = Field(default=20, gt=0, le=120)

    # 联网搜索配置 (Tavily Search API)
    tavily_api_key: str = ""
    tavily_api_base: str = "https://api.tavily.com"
    web_search_max_results: int = Field(default=5, ge=1, le=20)
    web_search_timeout: float = Field(default=15.0, gt=0, le=60)
    web_search_include_answer: bool = True

    # Milvus 配置
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_timeout: int = 10000  # 毫秒

    # RAG 配置
    rag_top_k: int = Field(default=3, gt=0)
    rag_collection_name: str = Field(
        default="oncall_knowledge_children", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"
    )
    rag_storage_path: str = Field(default="data/knowledge", min_length=1)
    rag_parent_chunk_size: int = Field(default=1600, gt=0)
    rag_child_chunk_size: int = Field(default=400, gt=0)
    rag_child_chunk_overlap: int = Field(default=80, ge=0)
    rag_storage_timeout: int = Field(default=120, gt=0)
    rag_query_rewrite_enabled: bool = True
    rag_query_rewrite_timeout: float = Field(default=20, gt=0, le=120)
    rag_history_max_messages: int = Field(default=20, ge=1, le=64)
    rag_history_max_chars: int = Field(default=32_000, ge=100, le=128_000)
    rag_hyde_enabled: bool = False
    rag_hyde_timeout: float = Field(default=20, gt=0, le=120)
    rag_rerank_enabled: bool = False
    rag_rerank_top_n: int = Field(default=3, ge=1, le=20)
    rag_rerank_score_threshold: float | None = Field(default=None, allow_inf_nan=False)

    # 上下文压缩 (Context Compactor) 生产级水位线配置
    compactor_enabled: bool = True
    compactor_hot_turns: int = Field(default=2, ge=1, le=10)
    compactor_tool_char_threshold: int = Field(default=45000, ge=1000)
    compactor_head_lines: int = Field(default=50, ge=5)
    compactor_tail_lines: int = Field(default=100, ge=5)
    compactor_error_context_radius: int = Field(default=3, ge=1, le=10)
    compactor_high_watermark_tokens: int = Field(default=48000, ge=2000)
    compactor_target_watermark_tokens: int = Field(default=18000, ge=1000)
    compactor_model: str = "qwen3.8-flash"
    compactor_timeout: float = Field(default=15.0, gt=0, le=60)

    @model_validator(mode="after")
    def validate_rag_chunks(self) -> "Settings":
        if not self.rag_child_chunk_size < self.rag_parent_chunk_size:
            raise ValueError("RAG_CHILD_CHUNK_SIZE 必须小于 RAG_PARENT_CHUNK_SIZE")
        if not self.rag_child_chunk_overlap < self.rag_child_chunk_size:
            raise ValueError("RAG_CHILD_CHUNK_OVERLAP 必须小于 RAG_CHILD_CHUNK_SIZE")
        return self

    # MCP 服务配置
    mcp_auto_start: bool = True
    mcp_discovery_timeout: float = Field(default=10, gt=0)
    mcp_tool_timeout: float = Field(default=30, gt=0)
    mcp_refresh_interval: float = Field(default=30, gt=0)
    mcp_cls_transport: str = "streamable-http"
    mcp_cls_url: str = "http://localhost:8003/mcp"
    mcp_monitor_transport: str = "streamable-http"
    mcp_monitor_url: str = "http://localhost:8004/mcp"

    @property
    def mcp_servers_file(self) -> Path:
        """外部 mcp_servers.json 配置文件路径（与 Cline / Claude Desktop 完全兼容）"""
        return Path(__file__).resolve().parents[1] / "mcp_servers.json"

    def read_custom_mcp_config(self) -> Dict[str, Any]:
        """读取外部 mcp_servers.json，返回解析后的字典"""
        if not self.mcp_servers_file.exists():
            return {}
        try:
            with open(self.mcp_servers_file, "r", encoding="utf-8") as f:
                content = json.load(f)
                return content if isinstance(content, dict) else {}
        except Exception:
            return {}

    def write_custom_mcp_config(self, data: Dict[str, Any]) -> None:
        """保存外部 mcp_servers.json 文件"""
        with open(self.mcp_servers_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @property
    def mcp_servers(self) -> Dict[str, Dict[str, Any]]:
        """获取完整的 MCP 服务器配置（内置服务 + 外部 mcp_servers.json 动态挂载）"""
        root = Path(__file__).resolve().parents[1]
        servers = {
            "cls": {
                "transport": self.mcp_cls_transport,
                "url": self.mcp_cls_url,
            },
            "monitor": {
                "transport": self.mcp_monitor_transport,
                "url": self.mcp_monitor_url,
            }
        }
        # 1. 规范化内置服务（如果支持 stdio 自动启动）
        for name in list(servers.keys()):
            connection = servers[name]
            try:
                url = urlsplit(connection.get("url", ""))
                local_builtin = (
                    url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
                    and url.port == {"cls": 8003, "monitor": 8004}.get(name)
                    and url.path.rstrip("/") == "/mcp"
                    and not url.username and not url.password and not url.query and not url.fragment
                )
            except ValueError:
                # MCP 地址错误交给后台发现报告，不阻止主应用和知识库启动。
                local_builtin = False
            if connection.get("transport") == "stdio" or (
                self.mcp_auto_start and connection.get("transport") == "streamable-http" and local_builtin
            ):
                servers[name] = {
                    "transport": "stdio", "command": sys.executable,
                    "args": ["-u", str(root / "mcp_servers" / f"{name}_server.py"), "--stdio"],
                    "cwd": str(root), "encoding": "utf-8",
                }

        # 2. 读取并合并外部 mcp_servers.json
        raw_custom = self.read_custom_mcp_config()
        # 兼容 {"mcpServers": {...}} 格式（与 Cline / Claude Desktop 完全通用）
        custom_servers = raw_custom.get("mcpServers", raw_custom) if isinstance(raw_custom, dict) else {}

        if isinstance(custom_servers, dict):
            for name, conn in custom_servers.items():
                if not isinstance(conn, dict):
                    continue
                server_entry = dict(conn)
                transport = server_entry.get("transport")
                if not transport:
                    if "command" in server_entry:
                        transport = "stdio"
                    elif "url" in server_entry:
                        transport = "streamable-http"
                    else:
                        transport = "stdio"
                server_entry["transport"] = transport

                if transport == "stdio":
                    cmd = server_entry.get("command")
                    # 如果未显式提供 command，或写的是通用的 python/python3/py，
                    # 自动注入当前运行环境的 sys.executable，确保自带 fastmcp 等项目虚拟环境依赖
                    if not cmd or cmd.lower() in ("python", "python3", "py", "python.exe"):
                        cmd = sys.executable
                    server_entry["command"] = cmd
                    args = server_entry.get("args", [])
                    resolved_args = []
                    for arg in args:
                        # 如果 arg 为相对路径且在项目内存在，自动转为绝对路径
                        arg_path = root / arg
                        if isinstance(arg, str) and not (arg.startswith("-") or arg.startswith("http")) and arg_path.exists():
                            resolved_args.append(str(arg_path))
                        else:
                            resolved_args.append(arg)
                    server_entry["args"] = resolved_args
                    server_entry.setdefault("cwd", str(root))
                    server_entry.setdefault("encoding", "utf-8")

                servers[name] = server_entry

        return servers


# 全局配置实例
config = Settings()
