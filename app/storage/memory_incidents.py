"""Private incident lifecycle and its transactional lexical index and vector outbox."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from app.models.memory import IncidentConfirm, IncidentCreate, IncidentFields, IncidentInvalidate, IncidentPatch, MemoryScope
from app.storage.memory_db import MemoryDatabase, _now
from app.storage.memory_incident_index import (
    NORMALIZER_VERSION, exact_identifiers, fts_available, index_lexical, match_expression, remove_lexical,
)


@dataclass(frozen=True)
class IncidentIndexRef:
    memory_id: str
    revision: int
    generation: int
    content_hash: str

    @classmethod
    def from_row(cls, row):
        return cls(row["memory_id"], row["revision"], row["generation"], row["content_hash"])


_CURRENT_INCIDENT = """i.user_id=? AND i.status='confirmed'
    AND (i.expires_at IS NULL OR i.expires_at>?)
    AND i.generation=coalesce((SELECT generation FROM ltm_profiles WHERE user_id=i.user_id), 0)"""


def _scope_filters(uid: str, scope: MemoryScope):
    filters = _CURRENT_INCIDENT
    args = [uid, _now()]
    for key, value in scope.model_dump(exclude_none=True).items():
        filters += f" AND i.{key}=?"
        args.append(value)
    return filters, args


class IncidentNotFoundError(Exception):
    pass


class IncidentConflictError(Exception):
    pass


class IncidentValidationError(Exception):
    pass


def _audit(conn, row, operation, before, fields=(), source="manual"):
    conn.execute("""INSERT INTO ltm_audit
        (event_id, user_id, memory_id, operation, field_paths_json, old_revision, new_revision, source_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
        uuid.uuid4().hex, row["user_id"], row["memory_id"], operation, json.dumps(list(fields)),
        before, row["revision"], source, row["updated_at"],
    ))


def _queue(conn, row, kind):
    key = f'{kind}:{row["memory_id"]}:{row["generation"]}:{row["revision"]}'
    conn.execute("""INSERT INTO ltm_jobs
        (job_id, user_id, kind, memory_id, session_id, idempotency_key, expected_generation,
         expected_revision, source_hash, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(idempotency_key) DO NOTHING""", (
        uuid.uuid4().hex, row["user_id"], kind, row["memory_id"], row["source_session_id"], key,
        row["generation"], row["revision"], row["content_hash"], row["updated_at"], row["updated_at"],
    ))


def _drop_index(conn, old, updated, *, withdraw=False):
    uid, mid, timestamp = updated["user_id"], updated["memory_id"], updated["updated_at"]
    remove_lexical(conn, uid, mid)
    conn.execute("""UPDATE ltm_jobs SET status='cancelled', updated_at=?
        WHERE user_id=? AND memory_id=? AND kind='index_incident' AND status IN ('pending','running')""",
        (timestamp, uid, mid))
    if withdraw:
        conn.execute("""UPDATE ltm_jobs SET session_id=NULL, source_hash=NULL,
            payload_json=NULL, result_json=NULL, error_code=NULL WHERE user_id=? AND memory_id=?""", (uid, mid))
    if withdraw or old["status"] == "confirmed":
        _queue(conn, updated, "purge_incident_index")


def _summary(row):
    return {key: row[key] for key in (
        "memory_id", "title", "status", "outcome", "revision", "environment", "updated_at", "expires_at",
    )}


def _snapshot(row, conn):
    snapshot = {**_summary(row), "generation": row["generation"]}
    if row["status"] == "withdrawn":
        return snapshot
    return {**snapshot, "incident": json.loads(row["incident_json"]), "created_at": row["created_at"],
            "confirmed_at": row["confirmed_at"], "confirmed_by": row["confirmed_by"],
            "invalidation_reason": row["invalidation_reason"],
            "lexical_mode": "fts5" if fts_available(conn) else "degraded"}


def _owned(conn, uid, mid):
    row = conn.execute("SELECT * FROM ltm_incidents WHERE user_id=? AND memory_id=?", (uid, mid)).fetchone()
    if row is None:
        raise IncidentNotFoundError("事故不存在或无权访问")
    return row


def _change(conn, old, **changes):
    updated = {**dict(old), **changes, "revision": old["revision"] + 1, "updated_at": _now()}
    columns = [*changes, "revision", "updated_at"]
    setters = ", ".join(f"{col}=?" for col in columns)  # column names come only from repository code
    count = conn.execute(f"UPDATE ltm_incidents SET {setters} WHERE user_id=? AND memory_id=? AND revision=?",
                         (*(updated[col] for col in columns), old["user_id"], old["memory_id"], old["revision"])).rowcount
    if count != 1:
        raise IncidentConflictError("事故版本已变更，请重新加载")
    return updated


def _withdraw(conn, old, source="manual"):
    updated = _change(conn, old, status="withdrawn", outcome=None, title=None, cluster=None,
                      environment=None, namespace=None, incident_json=None, identifiers_json="[]",
                      source_session_id=None, content_hash=None, confirmed_by=None, confirmed_at=None,
                      invalidation_reason=None, created_at=None, expires_at=None)
    _drop_index(conn, old, updated, withdraw=True)
    _audit(conn, updated, "incident_withdraw", old["revision"], source=source)
    return updated


def withdraw_session_incidents(conn: sqlite3.Connection, uid: str, session_id: str):
    """Called under the existing session deletion transaction, including on pre-M2 databases."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='ltm_incidents'").fetchone():
        return
    conn.execute("PRAGMA secure_delete=ON")
    if MemoryDatabase._schema_version(conn) >= 4:
        conn.execute("""UPDATE ltm_jobs SET request_id=NULL,turn_end_message_id=NULL
            WHERE user_id=? AND session_id=?""", (uid, session_id))
    conn.execute("""UPDATE ltm_jobs
        SET status=CASE WHEN status IN ('pending','running') OR result_json IS NOT NULL THEN 'cancelled' ELSE status END,
            lease_until=NULL, session_id=NULL, source_hash=NULL, payload_json=NULL, result_json=NULL,
            error_code=NULL, updated_at=?
        WHERE user_id=? AND session_id=? AND kind NOT IN ('index_incident','purge_incident_index')""",
        (_now(), uid, session_id))
    rows = conn.execute("SELECT * FROM ltm_incidents WHERE user_id=? AND source_session_id=?",
                        (uid, session_id)).fetchall()
    for row in rows:
        _withdraw(conn, row, source="session_delete")


def _validate_source(conn, uid, fields: IncidentFields):
    ids = set(fields.source_message_ids or ())
    if fields.recovery_evidence and not set(fields.recovery_evidence.message_ids) <= ids:
        raise IncidentValidationError("恢复证据的消息须包含在来源消息中")
    session = fields.source_session_id
    if not session:
        if ids:
            raise IncidentValidationError("引用消息前须选择来源会话")
        return
    if not conn.execute("SELECT 1 FROM sessions WHERE session_id=? AND user_id=?", (session, uid)).fetchone():
        raise IncidentNotFoundError("来源会话不存在或无权访问")
    if not ids:
        return
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(f"""SELECT timestamp FROM messages
        WHERE user_id=? AND session_id=? AND id IN ({placeholders})""", (uid, session, *ids)).fetchall()
    if len(rows) != len(ids):
        raise IncidentNotFoundError("来源消息不存在或无权访问")
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            value = row["timestamp"]
            timestamp = datetime.fromisoformat(value)
            # SessionDatabaseManager writes naive local ISO dates; SQLite legacy defaults were UTC.
            if timestamp.tzinfo is None:
                timestamp = timestamp.astimezone() if "T" in value else timestamp.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, OverflowError):
            raise IncidentValidationError("来源消息时间无效") from None
        if timestamp > now:
            raise IncidentValidationError("不能引用未来的消息")


def _content(fields: IncidentFields, outcome=None):
    document = fields.model_dump(mode="json", exclude_none=True)
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True)
    fingerprint = json.dumps([document, outcome], ensure_ascii=False, sort_keys=True)
    identifiers = [*(fields.services or []), *(fields.error_codes or [])]
    identifiers.extend(value for value in (fields.cluster, fields.environment, fields.namespace) if value)
    return {
        "incident_json": payload, "content_hash": hashlib.sha256(fingerprint.encode()).hexdigest(),
        "identifiers_json": json.dumps(sorted(set(exact_identifiers(" ".join(identifiers))))),
        "title": fields.title, "cluster": fields.cluster, "environment": fields.environment,
        "namespace": fields.namespace, "source_session_id": fields.source_session_id,
        "expires_at": fields.expires_at.isoformat() if fields.expires_at else None,
    }


class IncidentRepository:
    def __init__(self, db: MemoryDatabase):
        self.db = db

    @contextmanager
    def _connection(self, uid, *, write=False):
        self.db._ensure_schema(uid, 2)
        with self.db._connection() as conn:
            if write:
                conn.execute("PRAGMA secure_delete=ON")
                conn.execute("BEGIN IMMEDIATE")
            self.db._require_user(conn, uid)
            yield conn

    @contextmanager
    def _edit(self, uid, mid, revision):
        with self._connection(uid, write=True) as conn:
            row = _owned(conn, uid, mid)
            if row["status"] == "withdrawn":
                raise IncidentNotFoundError("事故已撤回")
            if row["revision"] != revision:
                raise IncidentConflictError("事故版本已变更，请重新加载")
            yield conn, row

    def create(self, uid: str, draft: IncidentCreate):
        with self._connection(uid, write=True) as conn:
            return self.create_in_transaction(conn, uid, draft)

    @staticmethod
    def create_in_transaction(conn, uid, draft, *, source="manual"):
        _validate_source(conn, uid, draft)
        mid, timestamp = uuid.uuid4().hex, _now()
        generation = conn.execute("SELECT generation FROM ltm_profiles WHERE user_id=?", (uid,)).fetchone()
        row = {
            "memory_id": mid, "user_id": uid, "status": "draft", "revision": 1,
            "generation": generation[0] if generation else 0, "created_at": timestamp,
            "updated_at": timestamp, **_content(draft),
        }
        columns, params = ", ".join(row), ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO ltm_incidents ({columns}) VALUES ({params})", tuple(row.values()))
        _audit(conn, row, "incident_create", 0, draft.model_fields_set, source=source)
        return _snapshot(_owned(conn, uid, mid), conn)

    def get(self, uid: str, mid: str):
        with self._connection(uid) as conn:
            return _snapshot(_owned(conn, uid, mid), conn)

    def list(self, uid: str, status, limit: int, cursor: str | None):
        with self._connection(uid) as conn:
            rows = conn.execute("""SELECT * FROM ltm_incidents WHERE user_id=?
                AND (? IS NULL OR status=?) AND (? IS NULL OR rowid <
                    (SELECT rowid FROM ltm_incidents WHERE user_id=? AND memory_id=?))
                ORDER BY rowid DESC LIMIT ?""", (uid, status, status, cursor, uid, cursor, limit + 1)).fetchall()
            return {"items": [_summary(row) for row in rows[:limit]],
                    "next_cursor": rows[limit - 1]["memory_id"] if len(rows) > limit else None}

    def patch(self, uid: str, mid: str, patch: IncidentPatch):
        with self._edit(uid, mid, patch.expected_revision) as (conn, old):
            original = json.loads(old["incident_json"])
            fields = IncidentFields.model_validate({**original, **patch.changes.model_dump(mode="json", exclude_unset=True)})
            if not fields.title:
                raise IncidentValidationError("标题不能为空")
            document = fields.model_dump(mode="json", exclude_none=True)
            changed = [key for key in patch.changes.model_fields_set if original.get(key) != document.get(key)]
            if not changed:
                return _snapshot(old, conn)
            _validate_source(conn, uid, fields)
            updated = _change(conn, old, **_content(fields), status="draft", outcome=None, confirmed_by=None,
                              confirmed_at=None, invalidation_reason=None)
            if old["status"] == "confirmed":
                _drop_index(conn, old, updated)
            _audit(conn, updated, "incident_edit", old["revision"], changed)
            return _snapshot(updated, conn)

    def confirm(self, uid: str, mid: str, confirmation: IncidentConfirm):
        with self._edit(uid, mid, confirmation.expected_revision) as (conn, old):
            if old["status"] != "draft":
                raise IncidentConflictError("仅待确认草稿可确认")
            document = json.loads(old["incident_json"])
            document.update(confirmation.model_dump(mode="json", include={"recovery_evidence"}, exclude_unset=True))
            fields = IncidentFields.model_validate(document)
            now = datetime.now(timezone.utc)
            if not (fields.symptoms and fields.services and fields.environment and fields.occurred_at):
                raise IncidentValidationError("确认前须填写症状、目标服务、适用环境与发生时间")
            if fields.occurred_at > now or (fields.expires_at and fields.expires_at <= now):
                raise IncidentValidationError("发生时间不能在未来，有效期须晚于当前时间")
            if confirmation.outcome == "resolved" and not fields.recovery_evidence:
                raise IncidentValidationError("确认已恢复前须提供恢复证据及引用")
            _validate_source(conn, uid, fields)
            updated = _change(conn, old, **_content(fields, confirmation.outcome), status="confirmed",
                              outcome=confirmation.outcome, confirmed_by=uid, confirmed_at=now.isoformat())
            index_lexical(conn, updated)
            _queue(conn, updated, "index_incident")
            _audit(conn, updated, "incident_confirm", old["revision"], ("status", "outcome", "recovery_evidence"))
            return _snapshot(updated, conn)

    def invalidate(self, uid: str, mid: str, request: IncidentInvalidate):
        with self._edit(uid, mid, request.expected_revision) as (conn, old):
            if old["status"] != "confirmed":
                raise IncidentConflictError("仅已确认事故可标记失效")
            updated = _change(conn, old, status="invalidated", invalidation_reason=request.reason)
            _drop_index(conn, old, updated)
            _audit(conn, updated, "incident_invalidate", old["revision"], ("status",))
            return _snapshot(updated, conn)

    def withdraw(self, uid: str, mid: str, revision: int):
        with self._edit(uid, mid, revision) as (conn, old):
            return _snapshot(_withdraw(conn, old), conn)

    @staticmethod
    def _lexical_rows(conn, uid, query, limit, scope):
        filters, args = _scope_filters(uid, scope)
        if fts_available(conn):
            expression = match_expression(query)
            rows = conn.execute(f"""SELECT i.* FROM ltm_incidents_fts
                JOIN ltm_incidents i ON i.memory_id=ltm_incidents_fts.memory_id
                 AND i.user_id=ltm_incidents_fts.user_id AND i.revision=ltm_incidents_fts.revision
                WHERE ltm_incidents_fts MATCH ? AND normalizer_version=? AND {filters}
                ORDER BY bm25(ltm_incidents_fts), i.memory_id LIMIT ?""",
                (expression, NORMALIZER_VERSION, *args, limit)).fetchall() if expression else []
            return rows, "fts5"

        identifiers = json.dumps(exact_identifiers(query))
        rows = conn.execute(f"""SELECT i.* FROM ltm_incidents i WHERE {filters}
            AND EXISTS (SELECT 1 FROM json_each(i.identifiers_json) field
              JOIN json_each(?) term ON field.value=term.value)
            ORDER BY i.rowid DESC LIMIT ?""", (*args, identifiers, limit)).fetchall()
        return rows, "degraded"

    def search(self, uid: str, query: str, limit: int, *, environment=None, cluster=None, namespace=None):
        scope = MemoryScope(environment=environment, cluster=cluster, namespace=namespace)
        with self._connection(uid) as conn:
            rows, mode = self._lexical_rows(conn, uid, query, limit, scope)
            return {"items": [_summary(row) for row in rows], "lexical_mode": mode}

    def runtime_candidates(self, uid: str, query: str, limit: int, scope: MemoryScope):
        with self.db._connection() as conn:
            if self.db._schema_version(conn) < 2:
                return [], "unavailable"
            rows, mode = self._lexical_rows(conn, uid, query, limit, scope)
            return [IncidentIndexRef.from_row(row) for row in rows], mode

    def verified_details(self, uid: str, scope: MemoryScope, generation: int, candidates: list[IncidentIndexRef]):
        if not candidates:
            return []
        by_id = {}
        for candidate in candidates:
            by_id.setdefault(candidate.memory_id, set()).add(candidate)
        placeholders = ", ".join("?" for _ in by_id)
        filters, args = _scope_filters(uid, scope)
        with self.db._connection() as conn:
            rows = conn.execute(f"""SELECT i.* FROM ltm_incidents i WHERE {filters}
                AND i.generation=? AND i.memory_id IN ({placeholders})""",
                (*args, generation, *by_id)).fetchall()
        valid = {IncidentIndexRef.from_row(row): dict(row) for row in rows
                 if IncidentIndexRef.from_row(row) in by_id[row["memory_id"]]}
        return [valid[candidate] for candidate in candidates if candidate in valid]

    def current_index_refs(self, uid: str, cursor: str, limit: int):
        with self.db._connection() as conn:
            if self.db._schema_version(conn) < 2:
                return []
            rows = conn.execute(f"""SELECT i.* FROM ltm_incidents i WHERE {_CURRENT_INCIDENT}
                AND i.memory_id>? ORDER BY i.memory_id LIMIT ?""", (uid, _now(), cursor, limit))
            return [IncidentIndexRef.from_row(row) for row in rows]

    def rebuild_index(self, uid: str):
        with self._connection(uid, write=True) as conn:
            if not fts_available(conn):
                return
            conn.execute("DELETE FROM ltm_incidents_fts WHERE user_id=?", (uid,))
            rows = conn.execute("""SELECT i.* FROM ltm_incidents i
                WHERE i.user_id=? AND i.status='confirmed' AND (i.expires_at IS NULL OR i.expires_at>?)
                  AND i.generation=coalesce((SELECT generation FROM ltm_profiles WHERE user_id=i.user_id), 0)""",
                (uid, _now()))
            for row in rows:
                index_lexical(conn, row)
