"""千问 API 客户端生命周期与 LangChain 原生精排。"""

import math
import time
from threading import Lock

from langchain_classic.retrievers.document_compressors import CrossEncoderReranker
from langchain_classic.retrievers.document_compressors.cross_encoder import BaseCrossEncoder
from langsmith import traceable
from loguru import logger

from app.config import Settings, config
from app.services.qwen_rerank import QwenRerankCrossEncoder


class ObservedCrossEncoder(BaseCrossEncoder):
    """每次调用独立记录原生 compressor 实际使用的分数，不重复推理。"""

    def __init__(self, provider):
        self.provider = provider
        self.scores = []

    def score(self, text_pairs):
        scores = [float(score) for score in self.provider.score(text_pairs)]
        if len(scores) != len(text_pairs) or not all(math.isfinite(score) for score in scores):
            raise ValueError("Cross-Encoder 返回的分数数量或数值无效")
        self.scores = scores
        return scores


class RerankService:
    def __init__(self, settings: Settings = config, model_provider=None):
        self.settings = settings
        self._provider = model_provider
        self._model = None
        self._load_lock = Lock()
        self._retry_after = 0.0
        self._error = ""

    def _load(self):
        if self._provider:
            return self._provider()
        return QwenRerankCrossEncoder(self.settings)

    def availability(self) -> dict:
        if not self.settings.rag_rerank_enabled:
            return {"available": False, "reason": "rerank_disabled"}
        with self._load_lock:
            if self._model is not None:
                return {"available": True, "reason": "client_ready"}
            if time.monotonic() < self._retry_after:
                return {"available": False, "reason": self._error}
            try:
                self._model = self._load()
                return {"available": True, "reason": "client_ready"}
            except Exception as error:
                # 配置修复后允许重试，避免反复初始化失败。
                self._error = f"rerank_unavailable:{type(error).__name__}"
                self._retry_after = time.monotonic() + 60
                logger.warning("精排客户端不可用，保留直接召回：{}", type(error).__name__)
                return {"available": False, "reason": self._error}

    @traceable(name="rag.rerank", run_type="chain")
    def rerank(self, documents, query, direct_documents, availability):
        before = [document.id for document in documents]
        limit = self.settings.rag_rerank_top_n
        if not documents:
            return {"documents": [], "scores": [], "before": before, "after": [],
                    "degraded": False, "reason": "no_candidates"}
        if not availability["available"]:
            result = list(direct_documents[:limit])
            return {"documents": result, "scores": [], "before": before,
                    "after": [doc.id for doc in result], "degraded": True,
                    "reason": availability["reason"]}
        try:
            model = ObservedCrossEncoder(self._model)
            compressor = CrossEncoderReranker(model=model, top_n=limit)
            result = list(compressor.compress_documents(documents, query))
            scores = {id(doc): score for doc, score in zip(documents, model.scores, strict=True)}
            threshold = self.settings.rag_rerank_score_threshold
            if threshold is not None:
                result = [doc for doc in result if scores[id(doc)] >= threshold]
            return {"documents": result, "before": before,
                    "scores": [{"parent_id": doc.id, "score": scores[id(doc)]} for doc in documents],
                    "after": [doc.id for doc in result], "degraded": False, "reason": "ranked"}
        except Exception as error:
            # HyDE 不能在精排失败时独自混入最终上下文。
            logger.warning("精排失败，回退直接召回：{}", type(error).__name__)
            result = list(direct_documents[:limit])
            return {"documents": result, "scores": [], "before": before,
                    "after": [doc.id for doc in result], "degraded": True,
                    "reason": f"rerank_failed:{type(error).__name__}"}

    def close(self):
        with self._load_lock:
            model, self._model = self._model, None
            if model is not None:
                model.close()
