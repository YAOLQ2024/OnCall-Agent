"""Local SQLite checkpoint saver. Production PostgreSQL requires a separate adapter.

Each checkpoint and its changed channel values commit in one transaction.
The configured LangGraph serializer is used without implicit pickle fallback.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    SerializerProtocol,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from loguru import logger


class SqliteSaver(BaseCheckpointSaver[int]):
    """LangGraph 状态机 SQLite 持久化 Checkpoint 存储器。"""

    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(serde=serde)
        if db_path is None:
            data_dir = Path(__file__).resolve().parents[2] / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = str(data_dir / "oncall_sessions.db")
        else:
            self.db_path = db_path
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._init_tables()

    @classmethod
    def from_conn_string(cls, conn_string: str) -> "SqliteSaver":
        """工厂方法：根据连接字符串创建实例（支持本地 SQLite 路径）。"""
        clean_path = conn_string.replace("sqlite:///", "").replace("sqlite://", "")
        return cls(db_path=clean_path)

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_tables(self) -> None:
        """初始化 LangGraph 状态机检查点关系表。"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    parent_checkpoint_id TEXT,
                    type TEXT NOT NULL,
                    checkpoint_blob BLOB NOT NULL,
                    metadata_type TEXT NOT NULL,
                    metadata_blob BLOB NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS writes (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    type TEXT NOT NULL,
                    value_blob BLOB NOT NULL,
                    task_path TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS blobs (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    version TEXT NOT NULL,
                    type TEXT NOT NULL,
                    blob BLOB NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_chk_thread ON checkpoints(thread_id, checkpoint_ns, checkpoint_id DESC);")

    # =========================================================
    # 序列化辅助方法（遵循配置的 SerializerProtocol）
    # =========================================================

    def _dump_typed(self, obj: Any) -> tuple[str, bytes]:
        return self.serde.dumps_typed(obj)

    def _load_typed(self, type_str: str, data: bytes) -> Any:
        # Do not silently bypass serializer validation with pickle.loads.
        return self.serde.loads_typed((type_str, data))

    def _load_blobs(
        self, conn: sqlite3.Connection, thread_id: str, checkpoint_ns: str, versions: ChannelVersions
    ) -> dict[str, Any]:
        channel_values: dict[str, Any] = {}
        cursor = conn.cursor()
        for k, v in versions.items():
            cursor.execute("""
                SELECT type, blob FROM blobs
                WHERE thread_id = ? AND checkpoint_ns = ? AND channel = ? AND version = ?;
            """, (thread_id, checkpoint_ns, k, str(v)))
            row = cursor.fetchone()
            if row and row["type"] != "empty":
                channel_values[k] = self._load_typed(row["type"], row["blob"])
        return channel_values

    # =========================================================
    # BaseCheckpointSaver 核心接口实现
    # =========================================================

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """从 SQLite 中持久化读取指定的或最新的 CheckpointTuple。"""
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            if checkpoint_id:
                cursor.execute("""
                    SELECT checkpoint_id, parent_checkpoint_id, type, checkpoint_blob, metadata_type, metadata_blob
                    FROM checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?;
                """, (thread_id, checkpoint_ns, checkpoint_id))
            else:
                cursor.execute("""
                    SELECT checkpoint_id, parent_checkpoint_id, type, checkpoint_blob, metadata_type, metadata_blob
                    FROM checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ?
                    ORDER BY checkpoint_id DESC LIMIT 1;
                """, (thread_id, checkpoint_ns))

            row = cursor.fetchone()
            if not row:
                return None

            c_id = row["checkpoint_id"]
            parent_c_id = row["parent_checkpoint_id"]
            checkpoint_data: Checkpoint = self._load_typed(row["type"], row["checkpoint_blob"])
            metadata_data: CheckpointMetadata = self._load_typed(row["metadata_type"], row["metadata_blob"])

            # 填充 channel_values
            channel_values = self._load_blobs(conn, thread_id, checkpoint_ns, checkpoint_data["channel_versions"])

            # 获取 pending_writes
            cursor.execute("""
                SELECT task_id, channel, type, value_blob FROM writes
                WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                ORDER BY idx ASC;
            """, (thread_id, checkpoint_ns, c_id))
            writes_rows = cursor.fetchall()
            pending_writes = [
                (w["task_id"], w["channel"], self._load_typed(w["type"], w["value_blob"]))
                for w in writes_rows
            ]

            return CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": c_id,
                    }
                },
                checkpoint={**checkpoint_data, "channel_values": channel_values},
                metadata=metadata_data,
                pending_writes=pending_writes,
                parent_config=(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": parent_c_id,
                        }
                    }
                    if parent_c_id
                    else None
                ),
            )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """按条件遍历持久化的历史检查点。"""
        clauses, params = [], []
        if config is not None:
            configurable = config["configurable"]
            clauses.append("thread_id = ?")
            params.append(configurable["thread_id"])
            if "checkpoint_ns" in configurable:
                clauses.append("checkpoint_ns = ?")
                params.append(configurable["checkpoint_ns"])
            if checkpoint_id := get_checkpoint_id(config):
                clauses.append("checkpoint_id = ?")
                params.append(checkpoint_id)
        if before and (before_id := get_checkpoint_id(before)):
            clauses.append("checkpoint_id < ?")
            params.append(before_id)
        query = "SELECT thread_id, checkpoint_ns, checkpoint_id FROM checkpoints"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY checkpoint_id DESC"
        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        count = 0
        for row in rows:
            if limit is not None and count >= limit:
                break
            res = self.get_tuple({"configurable": dict(row)})
            if res is not None and all(res.metadata.get(k) == v for k, v in (filter or {}).items()):
                count += 1
                yield res

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """原子持久化保存 Checkpoint 和关联的通道 Blobs。"""
        c = checkpoint.copy()
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = checkpoint["id"]
        parent_id = config["configurable"].get("checkpoint_id")

        values: dict[str, Any] = c.pop("channel_values", {})

        with self._get_connection() as conn:
            cursor = conn.cursor()
            # 1. 存储变动的通道 Blobs
            for k, v in new_versions.items():
                if k in values:
                    blob_type, blob_bytes = self._dump_typed(values[k])
                else:
                    blob_type, blob_bytes = "empty", b""

                cursor.execute("""
                    INSERT OR REPLACE INTO blobs (thread_id, checkpoint_ns, channel, version, type, blob)
                    VALUES (?, ?, ?, ?, ?, ?);
                """, (thread_id, checkpoint_ns, k, str(v), blob_type, blob_bytes))

            # 2. 存储 Checkpoint 记录
            chk_type, chk_bytes = self._dump_typed(c)
            meta_type, meta_bytes = self._dump_typed(get_checkpoint_metadata(config, metadata))

            cursor.execute("""
                INSERT OR REPLACE INTO checkpoints (
                    thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                    type, checkpoint_blob, metadata_type, metadata_blob
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, (thread_id, checkpoint_ns, checkpoint_id, parent_id, chk_type, chk_bytes, meta_type, meta_bytes))

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """持久化保存任务写入操作。"""
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]

        with self._get_connection() as conn:
            cursor = conn.cursor()
            for idx, (channel, value) in enumerate(writes):
                real_idx = WRITES_IDX_MAP.get(channel, idx)
                val_type, val_bytes = self._dump_typed(value)
                statement = "INSERT OR REPLACE" if channel in WRITES_IDX_MAP else "INSERT OR IGNORE"
                cursor.execute(f"""
                    {statement} INTO writes (
                        thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value_blob, task_path
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (thread_id, checkpoint_ns, checkpoint_id, task_id, real_idx, channel, val_type, val_bytes, task_path))

    def delete_thread(self, thread_id: str) -> None:
        """按 thread_id 级联删除该会话的所有持久化检查点与写入。"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM checkpoints WHERE thread_id = ?;", (thread_id,))
            cursor.execute("DELETE FROM writes WHERE thread_id = ?;", (thread_id,))
            cursor.execute("DELETE FROM blobs WHERE thread_id = ?;", (thread_id,))
            logger.info(f"已持久化清理 Checkpointer thread: {thread_id}")

    # 异步代理实现
    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        items = await asyncio.to_thread(lambda: list(self.list(config, filter=filter, before=before, limit=limit)))
        for item in items:
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)


# 全局单例持久化 Saver
sqlite_checkpointer = SqliteSaver()
