"""Private memory management, per-turn recall and task scheduling."""

from loguru import logger

from app.config import config
from app.models.memory import ProfilePatch, ProfileSnapshot
from app.services.memory_extraction_service import candidate_kinds
from app.services.memory_retrieval_service import MemoryRetrievalService
from app.services.memory_vector_store import MemoryVectorStore
from app.storage.memory_db import MemoryDatabase
from app.storage.memory_incidents import IncidentRepository
from app.storage.memory_jobs import MemoryBudgetError, MemoryJobRepository
from app.storage.session_db import session_db_manager


class MemoryService:
    def __init__(self, repository: MemoryDatabase):
        self.repository = repository
        self.incidents = IncidentRepository(repository)
        self.vectors = MemoryVectorStore(config, self.incidents)
        self.retrieval = MemoryRetrievalService(self.incidents, config, self.vectors)
        self.jobs = MemoryJobRepository(repository, self.incidents, config)

    def get_profile(self, user_id: str) -> ProfileSnapshot:
        return self.repository.get_profile(user_id)

    def patch_profile(self, user_id: str, patch: ProfilePatch) -> ProfileSnapshot:
        return self.repository.patch_profile(user_id, patch)

    @staticmethod
    def paid_extraction_available():
        return bool(config.ltm_enabled and config.ltm_paid_calls_enabled
                    and config.ltm_daily_input_token_budget
                    and config.ltm_daily_output_token_budget >= config.ltm_extract_output_tokens)

    def enqueue_manual(self, uid, request):
        if not self.paid_extraction_available():
            raise MemoryBudgetError("服务端提取开关或日额度尚未配置")
        return self.jobs.enqueue_manual(uid, request)

    def capture_automatic(self, uid):
        if not config.ltm_auto_extract_enabled or not self.paid_extraction_available():
            return None
        try:
            profile = self.repository.read_runtime_profile(uid)
            return profile if profile and profile.auto_extract_enabled else None
        except Exception as error:
            logger.warning("Memory candidate snapshot failed: {}", type(error).__name__)
            return None

    def save_turn(self, sessions, uid, session_id, user_message_id, question, answer, snapshot, *,
                  diagnostic=False, execution_trace=None):
        kinds = candidate_kinds(question, answer, diagnostic=diagnostic) if snapshot is not None else ()
        jobs = []

        def enqueue(conn, final_message_id):
            # If memory fails, preserve the answer and roll back every partial task.
            conn.execute("SAVEPOINT memory_candidates")
            try:
                jobs.extend(self.jobs.enqueue_automatic(
                    conn, uid, session_id, user_message_id, final_message_id, snapshot, kinds,
                ))
            except Exception as error:
                conn.execute("ROLLBACK TO memory_candidates")
                logger.warning("Memory candidate scheduling failed: {}", type(error).__name__)
            finally:
                conn.execute("RELEASE memory_candidates")

        sessions.record_message(session_id, "assistant", answer, uid, after_insert=enqueue if kinds else None,
                                execution_trace=execution_trace)
        return {"memory_job_id": jobs[0], "memory_job_ids": jobs} if jobs else {}

    @staticmethod
    def response(snapshot: ProfileSnapshot) -> dict:
        return {"code": 200, "message": "success", "data": {
            **snapshot.model_dump(exclude_none=True), "stage": "M4",
            "ltm_enabled": config.ltm_enabled, "runtime_connected": config.ltm_enabled,
            "automatic_available": config.ltm_auto_extract_enabled and MemoryService.paid_extraction_available(),
            "manual_extraction_available": MemoryService.paid_extraction_available(),
        }}


memory_service = MemoryService(MemoryDatabase(session_db_manager.db_path))
