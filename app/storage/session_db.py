"""本地持久化会话与用户存储管理模块 (SQLite Engine)。

生产环境对齐：
生产部署需要单独实现 PostgreSQL 存储与身份认证，当前仅实现本地 SQLite。
在本地开发/测试中，使用零外部依赖的高性能嵌入式 SQLite (data/oncall_sessions.db)，
开启 WAL 模式 (Write-Ahead Logging) 保障并发读写与断电持久化安全性。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from loguru import logger
from pydantic import BaseModel

from app.storage.memory_incidents import withdraw_session_incidents


# 默认预设的运维与研发角色用户（默认演示密码均为 123456）
DEFAULT_USERS = [
    {
        "user_id": "sre_alice",
        "username": "alice",
        "display_name": "Alice (SRE核心排障专家)",
        "role": "sre",
        "avatar": "👩‍💻",
        "department": "基础设施与稳定性运维部",
        "password": "123456",
    },
    {
        "user_id": "dev_bob",
        "username": "bob",
        "display_name": "Bob (交易业务研发工程师)",
        "role": "dev",
        "avatar": "👨‍💻",
        "department": "核心业务研发中心",
        "password": "123456",
    },
    {
        "user_id": "admin",
        "username": "admin",
        "display_name": "Admin (运维系统管理员)",
        "role": "admin",
        "avatar": "🛡️",
        "department": "技术保障委员会",
        "password": "123456",
    },
]


class UserProfile(BaseModel):
    user_id: str
    username: str
    display_name: str
    role: str
    avatar: str
    department: str


class SessionMeta(BaseModel):
    session_id: str
    user_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0
    last_message_preview: str = ""


class ChatMessageRecord(BaseModel):
    role: str
    content: str
    timestamp: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    execution_trace: list[dict[str, Any]] | None = None


def checkpoint_thread_id(user_id: str, session_id: str, worker: str) -> str:
    """Use unambiguous, tenant and worker scoped graph identifiers."""
    return json.dumps([user_id, session_id, worker], ensure_ascii=False, separators=(",", ":"))


class SessionOwnershipError(ValueError):
    """An existing session belongs to a different user."""


class SessionDatabaseManager:
    """管理用户、会话元数据与消息历史的持久化数据库管理器。"""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            data_dir = Path(__file__).resolve().parents[2] / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = str(data_dir / "oncall_sessions.db")
        else:
            self.db_path = db_path
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._current_user_id = "sre_alice"  # 默认激活 Alice (SRE 核心排障专家)
        self._init_db()

    @contextmanager
    def _get_connection(self):
        """获取启用 WAL 模式的高性能 SQLite 连接上下文。"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    PASSWORD_ITERATIONS = 600_000

    @classmethod
    def _hash_password(cls, password: str, salt: str | None = None) -> str:
        salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), cls.PASSWORD_ITERATIONS).hex()
        return f"pbkdf2_sha256${cls.PASSWORD_ITERATIONS}${salt}${digest}"

    @classmethod
    def _verify_password(cls, plain_password: str, password_hash: str) -> bool:
        try:
            if "$" not in password_hash:
                # Gemini's existing hashes are upgraded after successful login.
                digest = hashlib.pbkdf2_hmac("sha256", plain_password.encode(), b"oncall_sre_salt_v1", 10_000).hex()
                return hmac.compare_digest(digest, password_hash)
            algorithm, iterations, salt, expected = password_hash.split("$")
            if algorithm != "pbkdf2_sha256" or not 1 <= int(iterations) <= 2_000_000:
                return False
            digest = hashlib.pbkdf2_hmac("sha256", plain_password.encode(), salt.encode(), int(iterations)).hex()
            return hmac.compare_digest(digest, expected)
        except (ValueError, TypeError):
            return False

    def get_or_create_secret(self, name: str) -> str:
        with self._get_connection() as conn:
            conn.execute("INSERT OR IGNORE INTO app_secrets (name, value) VALUES (?, ?)",
                         (name, secrets.token_urlsafe(64)))
            return conn.execute("SELECT value FROM app_secrets WHERE name = ?", (name,)).fetchone()[0]

    def revoke_access_token(self, token_id: str, expires_at: int) -> None:
        with self._get_connection() as conn:
            conn.execute("DELETE FROM revoked_tokens WHERE expires_at <= ?", (int(time.time()),))
            conn.execute("INSERT OR IGNORE INTO revoked_tokens (token_id, expires_at) VALUES (?, ?)",
                         (token_id, expires_at))

    def is_token_revoked(self, token_id: str) -> bool:
        with self._get_connection() as conn:
            return conn.execute("SELECT 1 FROM revoked_tokens WHERE token_id = ? AND expires_at > ?",
                                (token_id, int(time.time()))).fetchone() is not None

    def _init_db(self):
        """初始化数据表结构并装载预设用户。"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE IF NOT EXISTS app_secrets (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            cursor.execute("CREATE TABLE IF NOT EXISTS revoked_tokens (token_id TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)")
            # 1. 用户表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    avatar TEXT NOT NULL,
                    department TEXT NOT NULL,
                    password_hash TEXT NOT NULL DEFAULT ''
                );
            """)
            # 兼容已有旧库：若无 password_hash 列则平滑升级
            existing_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(users);").fetchall()}
            if "password_hash" not in existing_cols:
                cursor.execute("ALTER TABLE users ADD COLUMN password_hash TEXT NOT NULL DEFAULT '';")

            # 2. 会话元数据表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    message_count INTEGER DEFAULT 0,
                    last_message_preview TEXT DEFAULT '',
                    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
            """)

            # 3. 消息明细表 (用于秒级加载前端对话历史)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    tool_calls_json TEXT DEFAULT '',
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at DESC);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id ASC);")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS message_execution_traces (
                    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                    trace_json TEXT NOT NULL
                );
            """)

            # 装载默认用户及密码指纹
            for u in DEFAULT_USERS:
                existing = cursor.execute("SELECT password_hash FROM users WHERE user_id = ?", (u["user_id"],)).fetchone()
                if existing and existing["password_hash"]:
                    continue
                pwd_hash = self._hash_password(u.get("password", "123456"))
                cursor.execute("""
                    INSERT OR IGNORE INTO users (user_id, username, display_name, role, avatar, department, password_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                """, (u["user_id"], u["username"], u["display_name"], u["role"], u["avatar"], u["department"], pwd_hash))
                cursor.execute("""
                    UPDATE users SET password_hash = ?
                    WHERE user_id = ? AND (password_hash IS NULL OR password_hash = '');
                """, (pwd_hash, u["user_id"]))

        logger.info(f"会话与用户持久化数据库初始化就绪: {self.db_path}")

    # =========================================================
    # 用户管理与多租户身份鉴权
    # =========================================================

    def get_current_user_id(self) -> str:
        return self._current_user_id

    def set_current_user_id(self, user_id: str) -> bool:
        user = self.get_user(user_id)
        if user:
            self._current_user_id = user_id
            logger.info(f"切换当前用户至: {user.display_name} ({user_id})")
            return True
        return False

    def get_user(self, user_id: str) -> Optional[UserProfile]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id, username, display_name, role, avatar, department FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = cursor.fetchone()
            if row:
                return UserProfile(**dict(row))
        return None

    def authenticate_user(self, identifier: str, password: str) -> Optional[UserProfile]:
        """按用户名 (alice/bob/admin) 或 user_id (sre_alice/dev_bob/admin) 校验登录凭证。"""
        ident = (identifier or "").strip()
        if not ident or not password:
            return None
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT user_id, username, display_name, role, avatar, department, password_hash
                FROM users
                WHERE LOWER(username) = LOWER(?) OR LOWER(user_id) = LOWER(?)
                LIMIT 1;
                """,
                (ident, ident),
            )
            row = cursor.fetchone()
            if not row:
                return None
            if not self._verify_password(password, row["password_hash"]):
                return None
            if not row["password_hash"].startswith(f"pbkdf2_sha256${self.PASSWORD_ITERATIONS}$"):
                conn.execute("UPDATE users SET password_hash = ? WHERE user_id = ? AND password_hash = ?",
                             (self._hash_password(password), row["user_id"], row["password_hash"]))
            data = dict(row)
            data.pop("password_hash", None)
            return UserProfile(**data)

    def list_users(self) -> List[UserProfile]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, username, display_name, role, avatar, department FROM users ORDER BY role DESC;")
            return [UserProfile(**dict(r)) for r in cursor.fetchall()]

    # =========================================================
    # 会话元数据管理 (CRUD)
    # =========================================================

    def list_sessions(self, user_id: Optional[str] = None) -> List[SessionMeta]:
        """获取指定用户（默认当前用户）的所有历史会话，按最近更新时间倒序。"""
        uid = user_id or self._current_user_id
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT session_id, user_id, title, created_at, updated_at, message_count, last_message_preview
                FROM sessions
                WHERE user_id = ?
                ORDER BY updated_at DESC;
            """, (uid,))
            return [SessionMeta(**dict(r)) for r in cursor.fetchall()]

    def get_session(self, session_id: str, user_id: Optional[str] = None) -> Optional[SessionMeta]:
        """获取指定会话元数据（包含租户权限校验）。"""
        uid = user_id or self._current_user_id
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT session_id, user_id, title, created_at, updated_at, message_count, last_message_preview
                FROM sessions
                WHERE session_id = ? AND user_id = ?;
            """, (session_id, uid))
            row = cursor.fetchone()
            if row:
                return SessionMeta(**dict(row))
        return None

    def create_or_touch_session(
        self,
        session_id: str,
        title: str = "新排障会话",
        user_id: Optional[str] = None,
    ) -> SessionMeta:
        """创建或更新会话元数据（如果已存在则更新 updated_at）。"""
        uid = user_id or self._current_user_id
        now_str = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            cursor.execute("SELECT session_id, user_id FROM sessions WHERE session_id = ?", (session_id,))
            existing = cursor.fetchone()
            if existing and existing["user_id"] != uid:
                raise SessionOwnershipError("会话不存在或无权访问")
            if not existing:
                cursor.execute("""
                    INSERT INTO sessions (session_id, user_id, title, created_at, updated_at, message_count)
                    VALUES (?, ?, ?, ?, ?, 0);
                """, (session_id, uid, title, now_str, now_str))
            else:
                cursor.execute("""
                    UPDATE sessions SET updated_at = ? WHERE session_id = ?;
                """, (now_str, session_id))

            row = cursor.execute("SELECT * FROM sessions WHERE session_id = ? AND user_id = ?", (session_id, uid)).fetchone()
            return SessionMeta(**dict(row))

    def update_session_title(self, session_id: str, title: str, user_id: Optional[str] = None) -> bool:
        uid = user_id or self._current_user_id
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE sessions SET title = ?, updated_at = ?
                WHERE session_id = ? AND user_id = ?;
            """, (title, datetime.now().isoformat(), session_id, uid))
            return cursor.rowcount > 0

    def delete_session(self, session_id: str, user_id: Optional[str] = None) -> bool:
        """删除指定会话及关联的所有历史消息（级联删除）。"""
        uid = user_id or self._current_user_id
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            if not cursor.execute(
                "SELECT 1 FROM sessions WHERE session_id = ? AND user_id = ?", (session_id, uid)
            ).fetchone():
                return False
            # All stores use this same database: cleanup is one transaction.
            tables = {r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            threads = [checkpoint_thread_id(uid, session_id, worker) for worker in ("rag", "aiops")]
            for table in ("checkpoints", "writes", "blobs"):
                if table in tables:
                    for thread in threads:
                        cursor.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread,))
            withdraw_session_incidents(conn, uid, session_id)
            cursor.execute("DELETE FROM sessions WHERE session_id = ? AND user_id = ?", (session_id, uid))
            return cursor.rowcount > 0

    # =========================================================
    # 消息记录持久化 (存储与秒级恢复)
    # =========================================================

    def record_message(
        self,
        session_id: str,
        role: str,
        content: str,
        user_id: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        *,
        after_insert: Callable[[sqlite3.Connection, int], None] | None = None,
        execution_trace: list[dict[str, Any]] | None = None,
    ) -> int:
        """持久化保存单条问答或工具执行结果，并同步更新会话统计。"""
        uid = user_id or self._current_user_id
        now_str = datetime.now().isoformat()
        tool_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else ""
        preview = content[:60].replace("\n", " ") if content else ""

        with self._get_connection() as conn:
            cursor = conn.cursor()
            conn.execute("BEGIN IMMEDIATE")
            # Validate ownership before either metadata or message writes.
            cursor.execute("SELECT title, message_count, user_id FROM sessions WHERE session_id = ?", (session_id,))
            row = cursor.fetchone()
            if row and row["user_id"] != uid:
                raise SessionOwnershipError("会话不存在或无权访问")
            if not row:
                title = preview[:25] if role == "user" else "故障排障分析"
                cursor.execute("""
                    INSERT INTO sessions (session_id, user_id, title, created_at, updated_at, message_count, last_message_preview)
                    VALUES (?, ?, ?, ?, ?, 1, ?);
                """, (session_id, uid, title, now_str, now_str, preview))
            else:
                new_count = row["message_count"] + 1
                # 若还是默认标题且当前是用户首个提问，自动生成语义标题
                current_title = row["title"]
                if role == "user" and (current_title == "新排障会话" or not current_title):
                    current_title = content[:28].strip()
                cursor.execute("""
                    UPDATE sessions
                    SET updated_at = ?, message_count = ?, last_message_preview = ?, title = ?
                    WHERE session_id = ?;
                """, (now_str, new_count, preview, current_title, session_id))

            # 2. 插入消息实体
            cursor.execute("""
                INSERT INTO messages (session_id, user_id, role, content, timestamp, tool_calls_json)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (session_id, uid, role, content, now_str, tool_json))
            message_id = cursor.lastrowid
            if execution_trace:
                cursor.execute("INSERT INTO message_execution_traces (message_id, trace_json) VALUES (?, ?)",
                               (message_id, json.dumps(execution_trace, ensure_ascii=False)))
            if after_insert is not None:
                after_insert(conn, message_id)
            return message_id

    def get_messages(self, session_id: str, user_id: Optional[str] = None) -> List[ChatMessageRecord]:
        """获取指定会话的所有持久化消息列表。"""
        uid = user_id or self._current_user_id
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.role, m.content, m.timestamp, m.tool_calls_json, t.trace_json
                FROM messages m LEFT JOIN message_execution_traces t ON t.message_id = m.id
                WHERE m.session_id = ? AND m.user_id = ?
                ORDER BY m.id ASC;
            """, (session_id, uid))
            rows = cursor.fetchall()

            records = []
            for r in rows:
                t_calls = None
                if r["tool_calls_json"]:
                    try:
                        t_calls = json.loads(r["tool_calls_json"])
                    except Exception:
                        pass
                execution_trace = None
                if r["trace_json"]:
                    try:
                        trace = json.loads(r["trace_json"])
                        if isinstance(trace, list) and all(isinstance(event, dict) for event in trace):
                            execution_trace = trace
                    except ValueError:
                        pass
                records.append(
                    ChatMessageRecord(
                        role=r["role"],
                        content=r["content"],
                        timestamp=r["timestamp"],
                        tool_calls=t_calls,
                        execution_trace=execution_trace,
                    )
                )
            return records

    def get_message_references(self, session_id: str, user_id: str):
        """Recent bounded previews for selecting evidence from an owned session."""
        with self._get_connection() as conn:
            # One snapshot keeps the ownership check and preview list consistent across a delete.
            conn.execute("BEGIN")
            if not conn.execute("SELECT 1 FROM sessions WHERE session_id=? AND user_id=?",
                                (session_id, user_id)).fetchone():
                raise SessionOwnershipError("会话不存在或无权访问")
            rows = conn.execute("""SELECT id, role, substr(content, 1, 100) AS preview FROM messages
                WHERE session_id=? AND user_id=? ORDER BY id DESC LIMIT 50""", (session_id, user_id)).fetchall()
            return [dict(row) for row in reversed(rows)]


# 全局单例会话数据库管理器
session_db_manager = SessionDatabaseManager()
