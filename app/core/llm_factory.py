"""通过千问平台的 OpenAI 兼容接口创建聊天模型。"""

from typing import Any

from langchain_openai import ChatOpenAI
from openai import APIStatusError
from app.config import config


def model_quota_exhausted(error: Exception) -> bool:
    return isinstance(error, APIStatusError) and error.code == "insufficient_quota"


def model_error_message(error: Exception) -> str:
    if model_quota_exhausted(error):
        return "千问 API 可用额度已用尽，请检查当前 API Key 对应账号的额度和计费设置。"
    return str(error)


class LLMFactory:
    """千问聊天模型工厂。"""

    @staticmethod
    def create_chat_model(
        model: str | None = None,
        temperature: float = 0.7,
        streaming: bool = True,
        base_url: str | None = None,
        api_key: str | None = None,
        request_timeout: float | None = None,
        max_retries: int = 2,
        extra_body: dict[str, Any] | None = None,
    ) -> ChatOpenAI:
        model = model or config.qwen_chat_model
        base_url = base_url or config.qwen_api_base
        api_key = api_key or config.qwen_api_key
        if not api_key:
            raise ValueError("请在项目 .env 中配置 QWEN_API_KEY")

        llm = ChatOpenAI(
            model=model,
            temperature=temperature,
            streaming=streaming,
            base_url=base_url,
            api_key=api_key,
            timeout=request_timeout,
            max_retries=max_retries,
            extra_body=extra_body,
        )

        return llm

# 全局 LLM 工厂实例
llm_factory = LLMFactory()
