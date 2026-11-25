"""Select current, private memories once per turn and bound the model view."""

import asyncio
import json
import re
import time
import uuid
from collections import defaultdict
from dataclasses import replace

from loguru import logger
from pydantic import ValidationError

from app.agent.compactor import estimate_tokens
from app.config import Settings
from app.models.memory import MemoryScope, ProfileSnapshot
from app.models.memory_runtime import MEMORY_POLICY, MemoryRunContext, RecalledIncident, reference_json
from app.storage.memory_incidents import IncidentIndexRef, IncidentRepository

# Match labeled identifiers and common environment names. No inference from an arbitrary hostname.
_SCOPE_LABELS = {"环境": "environment", "env": "environment", "environment": "environment",
                 "集群": "cluster", "cluster": "cluster", "命名空间": "namespace", "namespace": "namespace"}
_LABELED_SCOPE = re.compile(
    r"(?<![\w-])(env(?:ironment)?|cluster|namespace)\b(?:\s*[:：=]\s*|\s+)([a-z0-9][a-z0-9._:/-]*)"
    r"|(环境|集群|命名空间)\s*[:：=]\s*([a-z0-9][a-z0-9._:/-]*)"
    r"|(环境|集群|命名空间)\s*([a-z0-9][a-z0-9._:/-]*)", re.IGNORECASE,
)
_ENV_NAME = re.compile(r"(?<![A-Za-z0-9._/-])(production|prod|staging|stage|preprod|uat|test|development|dev)(?![A-Za-z0-9._/-])", re.I)
_CHINESE_ENV = re.compile("生产环境|线上环境|预发环境|测试环境|开发环境")
_ALIASES = {"prod": "production", "stage": "staging", "dev": "development",
            "生产环境": "production", "线上环境": "production", "预发环境": "staging",
            "测试环境": "test", "开发环境": "development"}
_NEGATIVE_SCOPE = re.compile(
    r"(?:不是|不在|而非|不要|不用|别用|排除|非|(?:not|never|except|exclude)\s+)\s*(?:在\s*)?$", re.I,
)
_NEGATIVE_SUFFIX = re.compile(r"^\s*(?:之外|以外|除外|不可用|不适用)")
_LEXICAL_QUERY_CHARS = 16_000
_EMBEDDING_QUERY_CHARS = 1_000


def _query_excerpt(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    return f"{text[:head]} {text[-(max_chars - head - 1):]}"


def _lexical_query(question: str) -> str:
    query = _CHINESE_ENV.sub(" ", _ENV_NAME.sub(" ", _LABELED_SCOPE.sub(" ", question))).strip()
    return _query_excerpt(query, _LEXICAL_QUERY_CHARS)


def _same_value(key, left, right):
    if key == "environment":
        return _ALIASES.get(left.casefold(), left.casefold()) == _ALIASES.get(right.casefold(), right.casefold())
    return left == right


def resolve_scope(question: str, profile: ProfileSnapshot, explicit: MemoryScope | None):
    defaults = {key: getattr(profile.profile, f"default_{key}") for key in MemoryScope.model_fields}
    fields = explicit.model_dump(exclude_none=True) if explicit else {}
    mentioned = defaultdict(list)
    ranges = []

    def add(key, value, start, end):
        if (_NEGATIVE_SCOPE.search(question[max(0, start - 16):start])
                or _NEGATIVE_SUFFIX.match(question[end:end + 10])):
            return False
        mentioned[key].append(value)
        return True

    for match in _LABELED_SCOPE.finditer(question):
        label, value = next((match[i], match[i + 1]) for i in (1, 3, 5) if match[i])
        # A terminal period or slash is punctuation around, rather than part of, the label value.
        if not add(_SCOPE_LABELS[label.lower()], value.rstrip("./:"), *match.span()):
            return None
        ranges.append(match.span())
    for matcher in (_ENV_NAME, _CHINESE_ENV):
        label_end = iter(ranges)
        current = next(label_end, None)
        for match in matcher.finditer(question):
            while current and current[1] <= match.start():
                current = next(label_end, None)
            if current and current[0] <= match.start():
                continue
            value = match.group().lower()
            if not add("environment", value, *match.span()):
                return None
    for key, values in mentioned.items():
        preferred = fields.get(key, values[0])
        if any(not _same_value(key, value, preferred) for value in values):
            return None
        # Keep an explicitly named persisted environment literal. Equivalent shorthand may use the saved spelling.
        previous = defaults[key]
        fields[key] = previous if key not in fields and previous and _same_value(key, preferred, previous) else preferred
    # An explicit change in any saved scope dimension invalidates inherited dimensions.
    changed_target = any(not defaults[key] or not _same_value(key, value, defaults[key])
                         for key, value in fields.items())
    inherited = {} if changed_target else defaults
    try:
        return MemoryScope(**{**inherited, **fields})
    except ValidationError:
        return None


def reciprocal_rank_fusion(*routes: list[IncidentIndexRef]) -> list[IncidentIndexRef]:
    scores = defaultdict(float)
    for route in routes:
        for rank, candidate in enumerate(dict.fromkeys(route), start=1):
            scores[candidate] += 1 / (60 + rank)
    return sorted(scores, key=scores.get, reverse=True)


def _profile_reference(profile, scope, *, general=False):
    fields = profile.profile.model_dump(exclude_none=True)
    selected = {key: fields[key] for key in ("preferred_language", "answer_detail", "response_preferences") if key in fields}
    if general:
        return selected
    for key in ("preferred_output_order", "responsible_services"):
        if fields.get(key):
            selected[key] = fields[key]
    if scope:
        target = scope.model_dump(exclude_none=True)
        if target:
            selected["target_to_verify"] = target
    return selected


def _incident_reference(row):
    data = json.loads(row["incident_json"])
    evidence = data.get("recovery_evidence", {})
    result = {key: data[key] for key in ("title", "services", "environment", "cluster", "namespace",
              "occurred_at", "version_constraints") if key in data}
    result.update({"memory_id": row["memory_id"], "revision": row["revision"], "outcome": row["outcome"]})
    # Historical commands, raw URLs and past exclusions stay in the management view.
    for key, value in (("symptoms", data.get("symptoms")), ("root_cause", data.get("root_cause")),
                       ("recovery_observation", evidence.get("summary")), ("lessons", data.get("lessons"))):
        if value:
            result[key] = value
    return result


def _pack(context, profile, rows, top_k, budget):
    budget -= estimate_tokens(MEMORY_POLICY) + 30
    if budget <= 0:
        return context
    kept = []
    badges = []
    profile = dict(profile)
    preferences = profile.pop("response_preferences", [])
    for optional in ("responsible_services", "target_to_verify", "preferred_output_order", "answer_detail", "preferred_language"):
        if estimate_tokens(reference_json(profile, kept)) <= budget:
            break
        profile.pop(optional, None)
    while preferences:
        candidate = {**profile, "response_preferences": preferences}
        if estimate_tokens(reference_json(candidate, kept)) <= budget:
            profile = candidate
            break
        preferences = preferences[:-1]
    for row in rows:
        try:
            item = _incident_reference(row)
        except (ValueError, KeyError, TypeError, AttributeError):
            logger.warning("Skipping unreadable incident during memory recall")
            continue
        if estimate_tokens(reference_json(profile, [*kept, item])) > budget:
            for optional in ("lessons", "root_cause"):
                item.pop(optional, None)
                if estimate_tokens(reference_json(profile, [*kept, item])) <= budget:
                    break
            else:
                continue
        kept.append(item)
        badges.append(RecalledIncident(row["memory_id"], row["revision"], item["title"]))
        if len(kept) == top_k:
            break
    if not profile and not kept:
        return context
    return replace(context, reference=reference_json(profile, kept), incidents=tuple(badges),
                   status=context.status if context.status == "degraded" else "recalled" if kept else "profile_only")


class MemoryRetrievalService:
    def __init__(self, incidents: IncidentRepository, settings: Settings, vector=None):
        self.incidents = incidents
        self.settings = settings
        self.vector = vector

    async def prepare(self, uid: str, session_id: str, question: str, *,
                      general: bool = False, explicit: MemoryScope | None = None):
        if not self.settings.ltm_enabled:
            return None
        context = MemoryRunContext(uid, session_id, uuid.uuid4().hex)
        profile_ref, rows = {}, []
        try:
            async with asyncio.timeout(self.settings.ltm_recall_timeout_seconds):
                snapshot = await asyncio.to_thread(self.incidents.db.read_runtime_profile, uid)
                if snapshot is None or not snapshot.use_enabled:
                    return None
                context = replace(context, profile_revision=snapshot.revision, generation=snapshot.generation)
                scope = None if general else await asyncio.to_thread(resolve_scope, question, snapshot, explicit)
                profile_ref = _profile_reference(snapshot, scope, general=general)
                if general or not question.strip() or not scope or not scope.environment:
                    if not general and question.strip():
                        context = replace(context, reason="scope_unavailable")
                    return self._pack(context, profile_ref, [])

                query = await asyncio.to_thread(_lexical_query, question)
                if not query:
                    return self._pack(context, profile_ref, [])

                async with asyncio.TaskGroup() as tasks:
                    vector_task = None
                    if self.settings.ltm_vector_enabled:
                        vector_query = _query_excerpt(query, _EMBEDDING_QUERY_CHARS)
                        vector_task = tasks.create_task(self._vector(context, vector_query, scope))
                    lex, rows, reason = await self._lexical(context, query, scope)
                    if vector_task:
                        vectors, vector_reason = await vector_task
                        reason = vector_reason or reason
                        if vectors:
                            fused = reciprocal_rank_fusion(lex, vectors)
                            rows = await self._verify(context, scope, fused)
                if reason:
                    context = replace(context, status="degraded", reason=reason)
                return self._pack(context, profile_ref, rows)
        except TimeoutError:
            context = replace(context, status="degraded", reason="timeout")
        except Exception as error:
            logger.warning("Long-term recall failed: {}", type(error).__name__)
            context = replace(context, status="degraded", reason="memory_unavailable")
            rows = []
        return self._pack(context, profile_ref, rows)

    async def _lexical(self, context, query, scope):
        try:
            lex, mode = await asyncio.to_thread(
                self.incidents.runtime_candidates, context.user_id, query, self.settings.ltm_candidates_per_route, scope,
            )
            rows = await self._verify(context, scope, lex)
            return lex, rows, None if mode == "fts5" else "lexical_limited"
        except Exception as error:
            logger.warning("Lexical recall failed: {}", type(error).__name__)
            return [], [], "lexical_unavailable"

    async def _verify(self, context, scope, refs):
        return await asyncio.to_thread(
            self.incidents.verified_details, context.user_id, scope, context.generation, refs,
        )

    async def _vector(self, context, query, scope):
        if not (self.vector and self.settings.ltm_paid_calls_enabled and self.settings.ltm_daily_embedding_item_budget):
            return [], "vector_budget_disabled"
        try:
            # Leave time to recheck SQLite after an optional vector attempt.
            seconds = self.settings.ltm_recall_timeout_seconds * 0.55
            async with asyncio.timeout(seconds):
                refs = await asyncio.to_thread(self.vector.search, context, query, scope,
                                               self.settings.ltm_candidates_per_route, deadline=time.monotonic() + seconds)
                return refs, None
        except Exception as error:
            logger.warning("Vector recall degraded: {}", type(error).__name__)
            return [], "vector_unavailable"

    def _pack(self, context, profile, rows):
        return _pack(context, profile, rows, self.settings.ltm_recall_top_k, self.settings.ltm_context_estimated_tokens)
