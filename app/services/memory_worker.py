"""Lifespan-managed consumer for persisted memory jobs."""

import asyncio

from loguru import logger
from openai import APIConnectionError, APIStatusError
from pymilvus.exceptions import MilvusException

from app.services.memory_extraction_service import InvalidProposalError, MemoryExtractionService
from app.services.memory_vector_store import EmbeddingBudgetError
from app.storage.memory_jobs import EXTRACTION_JOBS, INDEX_JOB, PURGE_JOB, MemoryBudgetError


class MemoryWorker:
    def __init__(self, jobs, vectors, settings, *, extractor=None):
        self.jobs = jobs
        self.vectors = vectors
        self.settings = settings
        self.extractor = extractor or MemoryExtractionService(settings)
        self.stop = asyncio.Event()
        self.tasks = []

    def _kinds(self):
        settings = self.settings
        kinds = []
        if settings.ltm_vector_enabled:
            kinds.append(PURGE_JOB)
            if settings.ltm_enabled and settings.ltm_paid_calls_enabled and settings.ltm_daily_embedding_item_budget:
                kinds.append(INDEX_JOB)
        if (settings.ltm_enabled and settings.ltm_paid_calls_enabled and settings.ltm_daily_input_token_budget
                and settings.ltm_daily_output_token_budget >= settings.ltm_extract_output_tokens):
            kinds.extend(EXTRACTION_JOBS)
        return kinds

    async def start(self):
        if self._kinds():
            self.tasks = [asyncio.create_task(self._run(), name=f"memory-worker-{index}")
                          for index in range(self.settings.ltm_worker_concurrency)]

    async def close(self):
        self.stop.set()
        if self.tasks:
            _, pending = await asyncio.wait(self.tasks, timeout=self.settings.ltm_extract_timeout_seconds + 5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _run(self):
        while not self.stop.is_set():
            try:
                job = await asyncio.to_thread(self.jobs.claim, self._kinds())
                if job:
                    await self.process(job)
                    continue
            except Exception as error:
                logger.warning("Memory worker loop failed: {}", type(error).__name__)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=0.75)
            except TimeoutError:
                pass

    async def process(self, job):
        try:
            if job["kind"] in EXTRACTION_JOBS:
                status, result = await self._extract(job)
            elif job["kind"] == INDEX_JOB:
                await asyncio.to_thread(self.vectors.index_job, job)
                status, result = "succeeded", None
            else:
                await asyncio.to_thread(self.vectors.purge_job, job)
                status, result = "succeeded", None
        except (MemoryBudgetError, EmbeddingBudgetError):
            await asyncio.to_thread(self.jobs.finish, job, "failed", error="budget_exhausted")
        except InvalidProposalError:
            await asyncio.to_thread(self.jobs.finish, job, "failed", error="invalid_proposal")
        except Exception as error:
            retry = isinstance(error, (TimeoutError, APIConnectionError, MilvusException))
            if isinstance(error, APIStatusError):
                retry = (error.status_code >= 500 or
                         (error.status_code == 429 and getattr(error, "code", None) != "insufficient_quota"))
            code = "temporary_failure" if retry else "provider_rejected"
            await asyncio.to_thread(self.jobs.finish, job, "failed", error=code, retry=retry)
        else:
            await asyncio.to_thread(self.jobs.finish, job, status, result=result)

    async def _extract(self, job):
        rows, profile = await asyncio.to_thread(self.jobs.input, job)
        if not rows:
            return "cancelled", None
        schema, messages, sources, estimate = self.extractor.prepare(job["kind"], rows, profile)
        reservation = await asyncio.to_thread(self.jobs.reserve_extraction, job, estimate)
        if not reservation:
            return "cancelled", None
        try:
            async with asyncio.timeout(self.settings.ltm_extract_timeout_seconds):
                response = await self.extractor.extract(schema, messages)
        except APIStatusError:
            await asyncio.to_thread(self.jobs.settle_extraction, reservation, "failed")
            raise
        except (Exception, asyncio.CancelledError):
            await asyncio.to_thread(self.jobs.settle_extraction, reservation, "unknown")
            raise
        raw = response.get("raw")
        await asyncio.to_thread(self.jobs.settle_extraction, reservation, "succeeded", getattr(raw, "usage_metadata", None))
        if response.get("parsing_error") or response.get("parsed") is None:
            raise InvalidProposalError()
        result = self.extractor.proposal(job["kind"], response["parsed"], sources, profile, job["session_id"])
        return ("succeeded", result) if result else ("skipped", None)
