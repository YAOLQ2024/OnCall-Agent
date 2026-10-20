"""配置管理模块

使用 Pydantic Settings 实现类型安全的配置管理
"""

from typing import Dict, Any
from pathlib import Path
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

    # 千问平台 OpenAI 兼容接口配置
    qwen_api_key: str = ""
    qwen_api_base: str = "https://maas.qianwenaiapi.com/compatible-mode/v1"
    qwen_chat_model: str = "qwen3.8-max"
    qwen_embedding_model: str = "qwen3.7-text-embedding-flash"
    qwen_embedding_dimensions: int = 1024
    qwen_rerank_model: str = "qwen3.7-text-rerank"
    qwen_rerank_url: str = "https://maas.qianwenaiapi.com/api/v1/services/rerank/text-rerank/text-rerank"
    qwen_rerank_timeout: float = Field(default=20, gt=0, le=120)

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
    rag_history_max_messages: int = Field(default=6, ge=1, le=20)
    rag_history_max_chars: int = Field(default=6000, ge=100, le=20000)
    rag_hyde_enabled: bool = False
    rag_hyde_timeout: float = Field(default=20, gt=0, le=120)
    rag_rerank_enabled: bool = False
    rag_rerank_top_n: int = Field(default=3, ge=1, le=20)
    rag_rerank_score_threshold: float | None = Field(default=None, allow_inf_nan=False)

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
    def mcp_servers(self) -> Dict[str, Dict[str, Any]]:
        """获取完整的 MCP 服务器配置"""
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
        root = Path(__file__).resolve().parents[1]
        for name, connection in servers.items():
            try:
                url = urlsplit(connection["url"])
                local_builtin = (
                    url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
                    and url.port == {"cls": 8003, "monitor": 8004}[name]
                    and url.path.rstrip("/") == "/mcp"
                    and not url.username and not url.password and not url.query and not url.fragment
                )
            except ValueError:
                # MCP 地址错误交给后台发现报告，不阻止主应用和知识库启动。
                local_builtin = False
            if connection["transport"] == "stdio" or (
                self.mcp_auto_start and connection["transport"] == "streamable-http" and local_builtin
            ):
                servers[name] = {
                    "transport": "stdio", "command": sys.executable,
                    "args": ["-u", str(root / "mcp_servers" / f"{name}_server.py"), "--stdio"],
                    "cwd": str(root), "encoding": "utf-8",
                }
        return servers


# 全局配置实例
config = Settings()
