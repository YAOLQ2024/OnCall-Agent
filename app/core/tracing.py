"""在模型及 Agent 导入前配置 LangSmith SDK；不发起网络请求。"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings


def configure_langsmith(settings: Settings) -> None:
    key = settings.langsmith_api_key.get_secret_value()
    if settings.langsmith_tracing and not key:
        raise ValueError("启用 LANGSMITH_TRACING 时必须配置 LANGSMITH_API_KEY")
    values = {
        "LANGSMITH_TRACING": str(settings.langsmith_tracing).lower(),
        "LANGSMITH_TRACING_V2": str(settings.langsmith_tracing).lower(),
        "LANGSMITH_PROJECT": settings.langsmith_project,
        "LANGSMITH_ENDPOINT": settings.langsmith_endpoint,
        "LANGSMITH_HIDE_INPUTS": str(settings.langsmith_hide_inputs).lower(),
        "LANGSMITH_HIDE_OUTPUTS": str(settings.langsmith_hide_outputs).lower(),
    }
    if key:
        values["LANGSMITH_API_KEY"] = key
    if settings.langsmith_workspace_id:
        values["LANGSMITH_WORKSPACE_ID"] = settings.langsmith_workspace_id
    os.environ.update(values)
    # SDK 可能已被其他模块导入；清除环境读取缓存以应用本次启动配置。
    from langsmith import utils as langsmith_utils

    langsmith_utils.get_env_var.cache_clear()
    langsmith_utils.get_tracer_project.cache_clear()


def flush_langsmith() -> None:
    """应用退出前提交 SDK 缓冲区；未开启时不创建客户端。"""
    if os.environ.get("LANGSMITH_TRACING") == "true":
        from langchain_core.tracers.langchain import wait_for_all_tracers

        wait_for_all_tracers()
