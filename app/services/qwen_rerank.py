"""将千问 DashScope 重排 API 适配到 LangChain Cross-Encoder 接口。"""

import math
from typing import Any

from langchain_classic.retrievers.document_compressors.cross_encoder import BaseCrossEncoder
from langsmith import traceable, get_current_run_tree
from openai import OpenAI

from app.config import Settings


class QwenRerankCrossEncoder(BaseCrossEncoder):
    def __init__(self, settings: Settings, client=None):
        if not settings.qwen_api_key:
            raise ValueError("请配置 QWEN_API_KEY")
        self.model = settings.qwen_rerank_model
        self.url = settings.qwen_rerank_url
        # 使用现有 SDK 的自定义端点能力，复用认证、连接池和异常处理。
        self.client = client if client is not None else OpenAI(
            api_key=settings.qwen_api_key, base_url=settings.qwen_api_base,
            timeout=settings.qwen_rerank_timeout, max_retries=0,
        )

    @traceable(name="rag.rerank_api", run_type="chain", process_inputs=lambda inputs: {
        "query": inputs["text_pairs"][0][0] if inputs.get("text_pairs") else "",
        "document_count": len(inputs.get("text_pairs", [])),
    })
    def score(self, text_pairs):
        run = get_current_run_tree()
        if run is not None:
            run.add_metadata({"model": self.model, "provider": "qwen"})
        if not text_pairs:
            return []
        query = text_pairs[0][0]
        if any(item[0] != query for item in text_pairs):
            raise ValueError("一次重排请求只能包含一个查询")
        # 绝对地址避免拼接聊天接口 /compatible-mode/v1 前缀。
        response = self.client.post(self.url, cast_to=dict[str, Any], body={
            "model": self.model,
            "input": {"query": query, "documents": [text for _, text in text_pairs]},
            "parameters": {"top_n": len(text_pairs), "return_documents": False},
        })
        output = response.get("output") if isinstance(response, dict) else None
        results = output.get("results") if isinstance(output, dict) else None
        if not isinstance(results, list) or len(results) != len(text_pairs):
            raise ValueError("重排 API 未返回全部候选分数")
        scores, seen = [0.0] * len(text_pairs), set()
        for item in results:
            if not isinstance(item, dict):
                raise ValueError("重排 API 结果结构无效")
            index, score = item.get("index"), item.get("relevance_score")
            if (type(index) is not int or not 0 <= index < len(scores) or index in seen
                    or type(score) not in (int, float) or not math.isfinite(score)):
                raise ValueError("重排 API 返回重复/越界索引或无效分数")
            seen.add(index)
            scores[index] = float(score)
        return scores

    def close(self):
        self.client.close()
