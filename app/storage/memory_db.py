"""SQLite long-term memory, verified backups and versioned migrations."""

import json
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.models.memory import ProfilePatch, ProfileSnapshot, ProfileValues
from app.storage.memory_incident_index import create_fts


class ProfileConflictError(Exception):
    pass


class MemoryUserNotFoundError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate_m2(conn: sqlite3.Connection):
    conn.execute("""CREATE TABLE ltm_incidents (
        memory_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        status TEXT NOT NULL CHECK (status IN ('draft','confirmed','invalidated','withdrawn')),
        outcome TEXT CHECK (outcome IN ('resolved','unresolved')),
        revision INTEGER NOT NULL CHECK (revision > 0), generation INTEGER NOT NULL,
        title TEXT, cluster TEXT, environment TEXT, namespace TEXT,
        incident_json TEXT, identifiers_json TEXT NOT NULL DEFAULT '[]',
        source_session_id TEXT, content_hash TEXT,
        confirmed_by TEXT, confirmed_at TEXT, invalidation_reason TEXT,
        created_at TEXT, updated_at TEXT NOT NULL, expires_at TEXT
    )""")
    conn.execute("CREATE INDEX idx_ltm_incidents_owner ON ltm_incidents(user_id, status)")
    conn.execute("CREATE INDEX idx_ltm_incidents_source ON ltm_incidents(user_id, source_session_id)")
    conn.execute("""CREATE TABLE ltm_jobs (
        job_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        kind TEXT NOT NULL, memory_id TEXT, session_id TEXT, idempotency_key TEXT NOT NULL UNIQUE,
        expected_generation INTEGER NOT NULL, expected_revision INTEGER, source_hash TEXT,
        status TEXT NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0,
        lease_until TEXT, next_attempt_at TEXT, payload_json TEXT, result_json TEXT, error_code TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX idx_ltm_jobs_incident ON ltm_jobs(user_id, memory_id, kind)")
    conn.execute("CREATE INDEX idx_ltm_jobs_source ON ltm_jobs(user_id, session_id)")
    conn.execute("ALTER TABLE ltm_audit ADD COLUMN memory_id TEXT")
    create_fts(conn)
    conn.execute("INSERT INTO ltm_schema_migrations VALUES (2, ?)", (_now(),))


def _migrate_m3(conn: sqlite3.Connection):
    conn.execute("""CREATE TABLE ltm_usage (
        reservation_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        request_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        model TEXT NOT NULL,
        embedding_items INTEGER NOT NULL CHECK(embedding_items > 0),
        status TEXT NOT NULL CHECK(status IN ('reserved','succeeded','unknown')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(user_id, request_id, kind)
    )""")
    conn.execute("CREATE INDEX idx_ltm_usage_date ON ltm_usage(created_at)")
    conn.execute("INSERT INTO ltm_schema_migrations VALUES (3, ?)", (_now(),))


def _migrate_m4(conn: sqlite3.Connection):
    conn.execute("ALTER TABLE ltm_jobs ADD COLUMN turn_end_message_id INTEGER")
    conn.execute("ALTER TABLE ltm_jobs ADD COLUMN lease_token TEXT")
    conn.execute("ALTER TABLE ltm_jobs ADD COLUMN request_id TEXT")
    conn.execute("CREATE INDEX idx_ltm_jobs_claim ON ltm_jobs(kind, status, next_attempt_at, lease_until)")
    conn.execute("CREATE INDEX idx_ltm_jobs_user ON ltm_jobs(user_id, created_at)")
    conn.execute("CREATE UNIQUE INDEX idx_ltm_jobs_request ON ltm_jobs(user_id, kind, request_id) WHERE request_id IS NOT NULL")
    conn.execute("ALTER TABLE ltm_usage RENAME TO ltm_usage_v3")
    conn.execute("""CREATE TABLE ltm_usage (
        reservation_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        request_id TEXT NOT NULL, kind TEXT NOT NULL, model TEXT NOT NULL,
        embedding_items INTEGER NOT NULL DEFAULT 0 CHECK(embedding_items >= 0),
        status TEXT NOT NULL CHECK(status IN ('reserved','succeeded','unknown','failed')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        input_reserved INTEGER NOT NULL DEFAULT 0, output_reserved INTEGER NOT NULL DEFAULT 0,
        input_actual INTEGER, output_actual INTEGER,
        UNIQUE(user_id, request_id, kind)
    )""")
    conn.execute("""INSERT INTO ltm_usage
        (reservation_id,user_id,request_id,kind,model,embedding_items,status,created_at,updated_at)
        SELECT reservation_id,user_id,request_id,kind,model,embedding_items,status,created_at,updated_at
        FROM ltm_usage_v3""")
    conn.execute("DROP TABLE ltm_usage_v3")
    conn.execute("CREATE INDEX idx_ltm_usage_date ON ltm_usage(created_at)")
    conn.execute("INSERT INTO ltm_schema_migrations VALUES (4, ?)", (_now(),))


class MemoryDatabase:
    def __init__(self, db_path: str):
        # Construction has no I/O and never creates model/vector clients.
        self.db_path = str(Path(db_path).resolve())
        self._migration_lock = threading.Lock()
        self._initialized_version = 0
        self.last_backup_path: Path | None = None

    @contextmanager
    def _connection(self):
        # The session database must already exist; a wrong path must not create a new DB.
        conn = sqlite3.connect(Path(self.db_path).as_uri() + "?mode=rw", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _require_user(conn, user_id: str):
        if conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone() is None:
            raise MemoryUserNotFoundError()

    def _backup(self, source: sqlite3.Connection, version: int = 1) -> None:
        directory = Path(self.db_path).parent / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = directory / f"ltm-m{version}-{stamp}-{uuid.uuid4().hex[:8]}.db"
        temporary = target.with_suffix(".tmp")
        try:
            with closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Memory migration backup verification failed")
                if not destination.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone():
                    raise RuntimeError("Session tables missing from migration backup")
            temporary.chmod(0o600)
            temporary.replace(target)
            self.last_backup_path = target
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _schema_version(conn: sqlite3.Connection) -> int:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='ltm_schema_migrations'"
        ).fetchone()
        if not exists:
            return 0
        return conn.execute("SELECT coalesce(max(version), 0) FROM ltm_schema_migrations").fetchone()[0]

    def _ensure_schema(self, user_id: str, version: int = 1):
        if self._initialized_version >= version:
            return
        with self._migration_lock, self._connection() as conn:
            if self._initialized_version >= version:
                return
            self._require_user(conn, user_id)
            current = self._schema_version(conn)
            if current < version:
                # Backup outside the write transaction to avoid waiting on our own writer.
                self._backup(conn, version)
                conn.execute("BEGIN IMMEDIATE")
                current = self._schema_version(conn)
                if current < 1:
                    self._migrate_m1(conn)
                if current < 2 <= version:
                    _migrate_m2(conn)
                if current < 3 <= version:
                    _migrate_m3(conn)
                if current < 4 <= version:
                    _migrate_m4(conn)
                conn.commit()
            self._initialized_version = max(current, version)

    @staticmethod
    def _migrate_m1(conn: sqlite3.Connection):
        conn.execute("""CREATE TABLE IF NOT EXISTS ltm_schema_migrations (
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS ltm_profiles (
            user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            profile_json TEXT NOT NULL DEFAULT '{}',
            field_sources_json TEXT NOT NULL DEFAULT '{}',
            revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
            generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
            use_enabled INTEGER NOT NULL DEFAULT 0 CHECK (use_enabled IN (0, 1)),
            auto_extract_enabled INTEGER NOT NULL DEFAULT 0 CHECK (auto_extract_enabled IN (0, 1)),
            updated_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS ltm_audit (
            event_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            operation TEXT NOT NULL,
            field_paths_json TEXT NOT NULL,
            old_revision INTEGER NOT NULL,
            new_revision INTEGER NOT NULL,
            source_type TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ltm_audit_user ON ltm_audit(user_id, created_at)")
        conn.execute("INSERT OR IGNORE INTO ltm_schema_migrations VALUES (1, ?)", (_now(),))

    @staticmethod
    def _snapshot(row) -> ProfileSnapshot:
        return ProfileSnapshot(
            profile=ProfileValues.model_validate(json.loads(row["profile_json"])),
            field_sources=json.loads(row["field_sources_json"]),
            revision=row["revision"], generation=row["generation"],
            use_enabled=bool(row["use_enabled"]), auto_extract_enabled=bool(row["auto_extract_enabled"]),
            updated_at=row["updated_at"],
        )

    def read_runtime_profile(self, user_id: str) -> ProfileSnapshot | None:
        # Chat must neither migrate the database nor create default profiles.
        with self._connection() as conn:
            if self._schema_version(conn) < 1:
                return None
            row = conn.execute("SELECT * FROM ltm_profiles WHERE user_id=?", (user_id,)).fetchone()
            return self._snapshot(row) if row else None

    def reserve_embedding(self, user_id: str, request_id: str, kind: str, model: str, budget: int) -> str | None:
        if budget <= 0:
            return None
        self._ensure_schema(user_id, 3)
        now = _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            items = conn.execute("SELECT coalesce(sum(embedding_items), 0) FROM ltm_usage WHERE created_at>=?",
                                 (now[:10],)).fetchone()[0]
            if items >= budget:
                return None
            reservation = uuid.uuid4().hex
            inserted = conn.execute("""INSERT INTO ltm_usage
                (reservation_id,user_id,request_id,kind,model,embedding_items,status,created_at,updated_at)
                VALUES (?, ?, ?, ?, ?, 1, 'reserved', ?, ?)
                ON CONFLICT(user_id, request_id, kind) DO NOTHING""",
                (reservation, user_id, request_id, kind, model, now, now)).rowcount
            return reservation if inserted else None

    def finish_embedding(self, reservation: str, *, succeeded: bool):
        with self._connection() as conn:
            conn.execute("UPDATE ltm_usage SET status=?, updated_at=? WHERE reservation_id=? AND status='reserved'",
                         ("succeeded" if succeeded else "unknown", _now(), reservation))

    def get_profile(self, user_id: str) -> ProfileSnapshot:
        self._ensure_schema(user_id)
        with self._connection() as conn:
            self._require_user(conn, user_id)
            row = conn.execute("SELECT * FROM ltm_profiles WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                conn.execute("INSERT OR IGNORE INTO ltm_profiles(user_id, updated_at) VALUES (?, ?)", (user_id, _now()))
                row = conn.execute("SELECT * FROM ltm_profiles WHERE user_id=?", (user_id,)).fetchone()
            return self._snapshot(row)

    def patch_profile(self, user_id: str, patch: ProfilePatch) -> ProfileSnapshot:
        self._ensure_schema(user_id, 4 if patch.auto_extract_enabled is not None else 1)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_user(conn, user_id)
            return self.patch_profile_in_transaction(conn, user_id, patch)

    def patch_profile_in_transaction(self, conn, user_id, patch, *, source_type="manual") -> ProfileSnapshot:
        conn.execute("INSERT OR IGNORE INTO ltm_profiles(user_id, updated_at) VALUES (?, ?)", (user_id, _now()))
        row = conn.execute("SELECT * FROM ltm_profiles WHERE user_id=?", (user_id,)).fetchone()
        if row["revision"] != patch.expected_revision:
            raise ProfileConflictError()
        profile = self._snapshot(row).profile.model_dump(exclude_none=True)
        sources = json.loads(row["field_sources_json"])
        changed = []
        timestamp = _now()
        for field, value in patch.changes.model_dump(exclude_unset=True).items():
            if value == profile.get(field):
                continue
            if value is None:
                profile.pop(field, None)
                sources.pop(field, None)
            else:
                profile[field] = value
                sources[field] = {"source_type": source_type, "confirmed_at": timestamp}
            changed.append(field)
        enabled = row["use_enabled"] if patch.use_enabled is None else int(patch.use_enabled)
        if enabled != row["use_enabled"]:
            changed.append("use_enabled")
        automatic = row["auto_extract_enabled"] if patch.auto_extract_enabled is None else int(patch.auto_extract_enabled)
        if automatic != row["auto_extract_enabled"]:
            changed.append("auto_extract_enabled")
        if not changed:
            return self._snapshot(row)
        updated = conn.execute("""UPDATE ltm_profiles SET profile_json=?, field_sources_json=?,
            revision=revision+1, use_enabled=?, auto_extract_enabled=?, updated_at=? WHERE user_id=? AND revision=?""",
            (json.dumps(profile, ensure_ascii=False), json.dumps(sources), enabled, automatic,
             timestamp, user_id, patch.expected_revision))
        if updated.rowcount != 1:
            raise ProfileConflictError()
        # Audit stores field names and revisions, never actual profile values or credentials.
        conn.execute("""INSERT INTO ltm_audit
            (event_id, user_id, operation, field_paths_json, old_revision, new_revision, source_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                     (uuid.uuid4().hex, user_id, "profile_update", json.dumps(changed),
                      row["revision"], row["revision"] + 1, source_type, timestamp))
        return self._snapshot(conn.execute("SELECT * FROM ltm_profiles WHERE user_id=?", (user_id,)).fetchone())
