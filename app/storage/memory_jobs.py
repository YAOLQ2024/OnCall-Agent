"""Durable jobs, user review and generation fencing for long-term memory."""

import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app.models.memory import IncidentCreate, ProfilePatch
from app.storage.memory_db import ProfileConflictError, _now
from app.storage.memory_incidents import IncidentConflictError, IncidentNotFoundError, _withdraw

PROFILE_JOB = "extract_profile_proposal"
INCIDENT_JOB = "extract_incident_draft"
INDEX_JOB = "index_incident"
PURGE_JOB = "purge_incident_index"
EXTRACTION_JOBS = (PROFILE_JOB, INCIDENT_JOB)
RETRYABLE = ("budget_exhausted", "temporary_failure", "lease_expired")
SOURCE_MESSAGES = 12
SOURCE_OMISSION = "\n[中间内容已截取]\n"


class MemoryBudgetError(Exception):
    pass


def _later(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _public(row, *, details=False):
    data = {key: row[key] for key in ("job_id", "kind", "status", "session_id", "memory_id",
                                    "attempt_count", "error_code", "created_at", "updated_at")}
    data["requires_confirmation"] = (row["kind"] in EXTRACTION_JOBS and row["status"] == "succeeded"
                                     and row["result_json"] is not None)
    data["retry_scheduled"] = row["status"] == "pending" and row["next_attempt_at"] is not None
    data["can_retry"] = row["status"] == "failed" and row["error_code"] in RETRYABLE
    if details and data["requires_confirmation"]:
        data["proposal"] = json.loads(row["result_json"])
    return data


def _source_rows(conn, uid, sid, start, end, char_limit):
    half = char_limit // 2
    return list(reversed(conn.execute("""SELECT id,role,
        CASE WHEN length(content)>? THEN substr(content,1,?) || ? || substr(content,-?) ELSE content END AS content,
        length(content) AS size FROM messages WHERE user_id=? AND session_id=? AND id BETWEEN ? AND ?
        AND role IN ('user','assistant') ORDER BY id DESC LIMIT ?""",
        (char_limit, half, SOURCE_OMISSION, half, uid, sid, start, end, SOURCE_MESSAGES)).fetchall()))

def _source_hash(rows):
    return hashlib.sha256(json.dumps([tuple(row) for row in rows], ensure_ascii=False).encode()).hexdigest()


class MemoryJobRepository:
    def __init__(self, db, incidents, settings):
        self.db = db
        self.incidents = incidents
        self.settings = settings

    @contextmanager
    def _transaction(self, uid=None):
        if uid is not None:
            self.db._ensure_schema(uid, 4)
        with self.db._connection() as conn:
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("BEGIN IMMEDIATE")
            if uid is not None:
                self.db._require_user(conn, uid)
            yield conn

    @staticmethod
    def _profile(conn, uid):
        return conn.execute("SELECT * FROM ltm_profiles WHERE user_id=?", (uid,)).fetchone()

    @staticmethod
    def _job(conn, uid, job_id):
        row = conn.execute("SELECT * FROM ltm_jobs WHERE user_id=? AND job_id=?", (uid, job_id)).fetchone()
        if row is None:
            raise IncidentNotFoundError("任务不存在或无权访问")
        return row

    def _enqueue(self, conn, uid, sid, kind, rows, profile, *, automatic=False, request_id=None):
        generation = profile["generation"] if profile else 0
        revision = profile["revision"] if profile and kind == PROFILE_JOB else 0 if kind == PROFILE_JOB else None
        start, end = rows[0]["id"], rows[-1]["id"]
        key = hashlib.sha256(json.dumps([uid, sid, start, end, generation, kind, 1]).encode()).hexdigest()
        if request_id:
            prior = conn.execute("SELECT * FROM ltm_jobs WHERE user_id=? AND kind=? AND request_id=?",
                                 (uid, kind, request_id)).fetchone()
            if prior and prior["idempotency_key"] != key:
                raise IncidentConflictError("该请求编号已经用于其他消息范围")
            existing = conn.execute("SELECT * FROM ltm_jobs WHERE user_id=? AND idempotency_key=?", (uid, key)).fetchone()
            if existing:
                return existing
            self._check_manual_budget(conn)
        payload = {"start": start, "automatic": automatic, "char_limit": self.settings.ltm_extract_input_chars}
        timestamp = _now()
        conn.execute("""INSERT INTO ltm_jobs
            (job_id,user_id,kind,session_id,idempotency_key,expected_generation,expected_revision,
             source_hash,payload_json,created_at,updated_at,turn_end_message_id,request_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (uuid.uuid4().hex, uid, kind, sid, key, generation, revision, _source_hash(rows),
             json.dumps(payload), timestamp, timestamp, end, request_id))
        row = conn.execute("SELECT * FROM ltm_jobs WHERE user_id=? AND idempotency_key=?", (uid, key)).fetchone()
        if row is None:
            raise IncidentConflictError("该请求编号已经用于其他消息范围")
        return row

    def enqueue_manual(self, uid, request):
        with self._transaction(uid) as conn:
            sid = request.session_id
            if conn.execute("SELECT 1 FROM sessions WHERE user_id=? AND session_id=?", (uid, sid)).fetchone() is None:
                raise IncidentNotFoundError("来源会话不存在或无权访问")
            end = request.end_message_id
            if end is None:
                row = conn.execute("""SELECT id FROM messages WHERE user_id=? AND session_id=? AND role='assistant'
                    ORDER BY id DESC LIMIT 1""", (uid, sid)).fetchone()
                end = row["id"] if row else 0
            rows = _source_rows(conn, uid, sid, request.start_message_id or 1, end, self.settings.ltm_extract_input_chars)
            if not rows or rows[-1]["id"] != end or rows[-1]["role"] != "assistant":
                raise IncidentConflictError("请选择当前会话中已完成的回复")
            if request.start_message_id is not None and rows[0]["id"] != request.start_message_id:
                raise IncidentConflictError("起始消息无效或所选范围超过 12 条")
            if not any(row["role"] == "user" for row in rows):
                raise IncidentConflictError("所选范围需包含用户消息")
            return _public(self._enqueue(conn, uid, sid, INCIDENT_JOB, rows, self._profile(conn, uid),
                                         request_id=request.request_id))

    def _check_manual_budget(self, conn):
        used = self._totals(conn)
        if (used["input_tokens"] >= self.settings.ltm_daily_input_token_budget
                or used["output_tokens"] + self.settings.ltm_extract_output_tokens > self.settings.ltm_daily_output_token_budget):
            raise MemoryBudgetError("本日提取额度不足")

    def enqueue_automatic(self, conn, uid, sid, start, end, snapshot, kinds):
        if self.db._schema_version(conn) < 4:
            return []
        profile = self._profile(conn, uid)
        if not profile or not profile["auto_extract_enabled"] or profile["generation"] != snapshot.generation:
            return []
        rows = _source_rows(conn, uid, sid, start, end, self.settings.ltm_extract_input_chars)
        if len(rows) != 2 or [row["id"] for row in rows] != [start, end] or [row["role"] for row in rows] != ["user", "assistant"]:
            return []
        return [self._enqueue(conn, uid, sid, kind, rows, profile, automatic=True)["job_id"]
                for kind in kinds if kind != PROFILE_JOB or snapshot.revision == profile["revision"]]

    def list(self, uid, limit=20, cursor=None):
        self.db._ensure_schema(uid, 4)
        with self.db._connection() as conn:
            rows = conn.execute("""SELECT * FROM ltm_jobs WHERE user_id=? AND
                (? IS NULL OR rowid < (SELECT rowid FROM ltm_jobs WHERE user_id=? AND job_id=?))
                ORDER BY rowid DESC LIMIT ?""", (uid, cursor, uid, cursor, limit + 1)).fetchall()
            return {"jobs": [_public(row) for row in rows[:limit]],
                    "next_cursor": rows[limit - 1]["job_id"] if len(rows) > limit else None,
                    "usage": self._totals(conn, uid),
                    "manual_extraction_available": bool(self.settings.ltm_enabled and self.settings.ltm_paid_calls_enabled
                        and self.settings.ltm_daily_input_token_budget
                        and self.settings.ltm_daily_output_token_budget >= self.settings.ltm_extract_output_tokens),
                    "limits": {"input_tokens": self.settings.ltm_daily_input_token_budget,
                               "output_tokens": self.settings.ltm_daily_output_token_budget,
                               "embedding_items": self.settings.ltm_daily_embedding_item_budget}}

    def get(self, uid, job_id):
        self.db._ensure_schema(uid, 4)
        with self.db._connection() as conn:
            return _public(self._job(conn, uid, job_id), details=True)

    def _valid_source(self, conn, job):
        profile = self._profile(conn, job["user_id"])
        generation, revision = (profile["generation"], profile["revision"]) if profile else (0, 0)
        if generation != job["expected_generation"] or (job["kind"] == PROFILE_JOB and revision != job["expected_revision"]):
            return []
        payload = json.loads(job["payload_json"] or "{}")
        if not payload or (payload["automatic"] and (not profile or not profile["auto_extract_enabled"])):
            return []
        rows = _source_rows(conn, job["user_id"], job["session_id"], payload["start"],
                            job["turn_end_message_id"], payload["char_limit"])
        return rows if rows and _source_hash(rows) == job["source_hash"] else []

    def review(self, uid, job_id, request):
        with self._transaction(uid) as conn:
            job = self._job(conn, uid, job_id)
            if not _public(job)["requires_confirmation"]:
                raise IncidentConflictError("该任务当前没有可审核的提议")
            proposal = json.loads(job["result_json"])
            result = {}
            if request.action == "apply":
                if not self._valid_source(conn, job):
                    raise IncidentConflictError("来源或画像版本已变化，请重新提取")
                if job["kind"] == PROFILE_JOB:
                    changes = proposal["changes"]
                    if request.fields is not None:
                        if not request.fields or not set(request.fields) <= changes.keys():
                            raise IncidentConflictError("请选择有效的画像字段")
                        changes = {key: changes[key] for key in request.fields}
                    try:
                        snapshot = self.db.patch_profile_in_transaction(
                            conn, uid, ProfilePatch(expected_revision=job["expected_revision"], changes=changes),
                            source_type="reviewed_proposal",
                        )
                    except ProfileConflictError:
                        raise IncidentConflictError("画像版本已变化，请重新提取") from None
                    result = {"profile_revision": snapshot.revision}
                else:
                    draft = IncidentCreate.model_validate(proposal["draft"])
                    incident = self.incidents.create_in_transaction(conn, uid, draft, source="reviewed_proposal")
                    result = {"memory_id": incident["memory_id"]}
            conn.execute("""UPDATE ltm_jobs SET result_json=NULL, status=?, updated_at=?, memory_id=?
                WHERE job_id=? AND user_id=?""",
                ("succeeded" if request.action == "apply" else "skipped", _now(), result.get("memory_id"), job_id, uid))
            return {**_public(self._job(conn, uid, job_id)), **result}

    def retry(self, uid, job_id):
        with self._transaction(uid) as conn:
            job = self._job(conn, uid, job_id)
            if not _public(job)["can_retry"]:
                raise IncidentConflictError("该任务当前不可重试")
            if job["kind"] in EXTRACTION_JOBS:
                if not self.settings.ltm_enabled or not self.settings.ltm_paid_calls_enabled:
                    raise MemoryBudgetError("服务端提取开关尚未开启")
                if json.loads(job["payload_json"] or "{}").get("automatic") and not self.settings.ltm_auto_extract_enabled:
                    raise MemoryBudgetError("服务端自动提取开关尚未开启")
                if not self._valid_source(conn, job):
                    raise IncidentConflictError("来源或画像版本已变化，请重新提取")
                self._check_manual_budget(conn)
            elif not self.settings.ltm_vector_enabled:
                raise IncidentConflictError("向量存储尚未开启")
            elif job["kind"] == INDEX_JOB and (
                not self.settings.ltm_enabled or not self.settings.ltm_paid_calls_enabled
                or not self.settings.ltm_daily_embedding_item_budget or not job["source_hash"]
                or self._totals(conn)["embedding_items"] >= self.settings.ltm_daily_embedding_item_budget
            ):
                raise IncidentConflictError("索引来源或 embedding 额度不可用")
            conn.execute("""UPDATE ltm_jobs SET status='pending',attempt_count=0,next_attempt_at=NULL,
                error_code=NULL,lease_until=NULL,lease_token=NULL,updated_at=? WHERE job_id=? AND user_id=?""",
                (_now(), job_id, uid))
            return _public(self._job(conn, uid, job_id))

    def forget(self, uid, expected_generation):
        with self._transaction(uid) as conn:
            conn.execute("INSERT OR IGNORE INTO ltm_profiles(user_id,updated_at) VALUES (?,?)", (uid, _now()))
            profile = self._profile(conn, uid)
            if profile["generation"] != expected_generation:
                raise IncidentConflictError("记忆代号已变化，请重新加载后确认")
            timestamp = _now()
            conn.execute("""UPDATE ltm_profiles SET generation=generation+1, revision=revision+1,
                profile_json='{}',field_sources_json='{}',updated_at=? WHERE user_id=? AND generation=?""",
                (timestamp, uid, expected_generation))
            conn.execute("""UPDATE ltm_jobs SET
                status=CASE WHEN kind='purge_incident_index' THEN status
                    WHEN status IN ('pending','running') OR result_json IS NOT NULL THEN 'cancelled' ELSE status END,
                payload_json=NULL,result_json=NULL,source_hash=NULL,session_id=NULL,request_id=NULL,
                turn_end_message_id=NULL,error_code=NULL,updated_at=?
                WHERE user_id=? AND expected_generation<=?""", (timestamp, uid, expected_generation))
            rows = conn.execute("SELECT * FROM ltm_incidents WHERE user_id=? AND status!='withdrawn'", (uid,)).fetchall()
            for row in rows:
                _withdraw(conn, row, source="forget")
            conn.execute("""INSERT INTO ltm_audit
                (event_id,user_id,operation,field_paths_json,old_revision,new_revision,source_type,created_at)
                VALUES (?,?,'forget','[]',?,?, 'manual',?)""",
                (uuid.uuid4().hex, uid, profile["revision"], profile["revision"] + 1, timestamp))
            return {"generation": expected_generation + 1, "withdrawn_count": len(rows)}

    def _ensure_worker_schema(self):
        with self.db._connection() as conn:
            version = self.db._schema_version(conn)
            if version >= 4:
                return True
            row = conn.execute("SELECT user_id FROM ltm_jobs WHERE status='pending' LIMIT 1").fetchone() if version >= 2 else None
        if row:
            self.db._ensure_schema(row["user_id"], 4)
            return True
        return False

    def claim(self, kinds):
        if not kinds or not self._ensure_worker_schema():
            return None
        now = _now()
        params = ", ".join("?" for _ in kinds)
        with self._transaction() as conn:
            conn.execute("""UPDATE ltm_jobs SET status='failed',error_code='lease_expired',lease_until=NULL,
                lease_token=NULL,updated_at=? WHERE status='running' AND lease_until<=? AND attempt_count>=?""",
                (now, now, self.settings.ltm_job_max_attempts))
            row = conn.execute(f"""SELECT j.* FROM ltm_jobs j WHERE kind IN ({params})
                AND (j.status='pending' OR (j.status='running' AND j.lease_until<=?))
                AND (j.next_attempt_at IS NULL OR j.next_attempt_at<=?)
                AND (j.kind NOT IN ('extract_profile_proposal','extract_incident_draft') OR ?
                     OR coalesce(json_extract(j.payload_json,'$.automatic'),0)=0)
                AND (j.kind!='index_incident' OR EXISTS
                    (SELECT 1 FROM ltm_profiles p WHERE p.user_id=j.user_id AND p.use_enabled=1
                     AND p.generation=j.expected_generation))
                AND (j.kind!='purge_incident_index' OR NOT EXISTS
                    (SELECT 1 FROM ltm_jobs x WHERE x.user_id=j.user_id AND x.memory_id=j.memory_id
                     AND x.kind='index_incident' AND x.expected_revision<j.expected_revision AND x.lease_until>?))
                ORDER BY CASE WHEN j.kind='purge_incident_index' THEN 0 ELSE 1 END, j.rowid LIMIT 1""",
                (*kinds, now, now, self.settings.ltm_auto_extract_enabled, now)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            until = _later(max(self.settings.ltm_job_lease_seconds, self.settings.ltm_extract_timeout_seconds + 15))
            conn.execute("""UPDATE ltm_jobs SET status='running',attempt_count=attempt_count+1,lease_until=?,
                lease_token=?,next_attempt_at=NULL,error_code=NULL,updated_at=? WHERE job_id=?""",
                (until, token, now, row["job_id"]))
            return dict(self._job(conn, row["user_id"], row["job_id"]))

    def input(self, job):
        with self.db._connection() as conn:
            conn.execute("BEGIN")
            row = self._job(conn, job["user_id"], job["job_id"])
            if row["status"] != "running" or row["lease_token"] != job["lease_token"] or row["lease_until"] <= _now():
                return [], {}
            rows = self._valid_source(conn, row)
            profile = self._profile(conn, job["user_id"])
            return [dict(item) for item in rows], json.loads(profile["profile_json"]) if profile else {}

    def finish(self, job, status, *, result=None, error=None, retry=False):
        with self._transaction() as conn:
            row = self._job(conn, job["user_id"], job["job_id"])
            if row["lease_token"] != job["lease_token"]:
                return
            if row["status"] == "cancelled":
                conn.execute("UPDATE ltm_jobs SET lease_until=NULL,lease_token=NULL WHERE job_id=?", (job["job_id"],))
                return
            if row["status"] != "running" or row["lease_until"] <= _now():
                return
            if row["kind"] in EXTRACTION_JOBS and not self._valid_source(conn, row):
                status, result, error, retry = "cancelled", None, None, False
            if retry and row["attempt_count"] < self.settings.ltm_job_max_attempts:
                status, next_at = "pending", _later(2 ** row["attempt_count"])
            else:
                next_at = None
            conn.execute("""UPDATE ltm_jobs SET status=?,result_json=?,error_code=?,next_attempt_at=?,
                lease_until=NULL,lease_token=NULL,updated_at=? WHERE job_id=?""",
                (status, json.dumps(result, ensure_ascii=False) if result else None, error, next_at, _now(), job["job_id"]))

    @staticmethod
    def _totals(conn, uid=None):
        query = """SELECT coalesce(sum(embedding_items),0) AS embedding_items,
            coalesce(sum(CASE WHEN status='succeeded' THEN coalesce(input_actual,input_reserved) ELSE input_reserved END),0) AS input_tokens,
            coalesce(sum(CASE WHEN status='succeeded' THEN coalesce(output_actual,output_reserved) ELSE output_reserved END),0) AS output_tokens
            FROM ltm_usage WHERE created_at>=?"""
        args = (_now()[:10],)
        if uid is not None:
            query += " AND user_id=?"
            args += (uid,)
        return dict(conn.execute(query, args).fetchone())

    def reserve_extraction(self, job, input_tokens):
        output_tokens = self.settings.ltm_extract_output_tokens
        with self._transaction() as conn:
            row = self._job(conn, job["user_id"], job["job_id"])
            if (row["status"] != "running" or row["lease_token"] != job["lease_token"]
                    or row["lease_until"] <= _now() or not self._valid_source(conn, row)):
                return None
            if json.loads(row["payload_json"]).get("automatic") and not self.settings.ltm_auto_extract_enabled:
                return None
            totals = self._totals(conn)
            if (totals["input_tokens"] + input_tokens > self.settings.ltm_daily_input_token_budget
                    or totals["output_tokens"] + output_tokens > self.settings.ltm_daily_output_token_budget):
                raise MemoryBudgetError("本日提取额度不足")
            reservation = uuid.uuid4().hex
            now = _now()
            conn.execute("""INSERT INTO ltm_usage
                (reservation_id,user_id,request_id,kind,model,status,created_at,updated_at,input_reserved,output_reserved)
                VALUES (?,?,?,?,?,'reserved',?,?,?,?)""",
                (reservation, job["user_id"], job["lease_token"], job["kind"],
                 self.settings.ltm_extract_model, now, now, input_tokens, output_tokens))
            return reservation

    def settle_extraction(self, reservation, status, usage=None):
        usage = usage or {}
        with self.db._connection() as conn:
            conn.execute("""UPDATE ltm_usage SET status=?,input_actual=?,output_actual=?,updated_at=?
                WHERE reservation_id=? AND status='reserved'""",
                (status, usage.get("input_tokens"), usage.get("output_tokens"), _now(), reservation))
