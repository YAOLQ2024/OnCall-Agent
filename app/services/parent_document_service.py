"""正式知识库父子召回及来源文件的索引版本管理。"""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from uuid import uuid4

from langchain_classic.retrievers import ParentDocumentRetriever
from langchain_classic.storage import LocalFileStore, create_kv_docstore
from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore
from langchain_text_splitters import MarkdownTextSplitter
from loguru import logger
from langsmith import traceable

from app.config import Settings, config
from app.services.vector_store_manager import vector_store_manager
from app.services.traced_stores import TracedDocumentStore


class ParentDocumentService:
    """框架负责切分和父子回取，本服务负责文件更新、可见版本和持久化生命周期。

    SQLite 使用默认回滚日志模式：读取事务覆盖召回过程，版本切换等待旧读取结束。
    多进程须共享相同的本地父文档目录和 SQLite 文件。
    """

    def __init__(
        self,
        settings: Settings = config,
        vector_store_provider: Callable[[], VectorStore] = vector_store_manager.get_vector_store,
    ) -> None:
        self.settings = settings
        self._vector_store_provider = vector_store_provider
        root = Path(settings.rag_storage_path)
        if not root.is_absolute():
            root = Path(__file__).resolve().parents[2] / root
        self.storage_path = root / settings.rag_collection_name
        self._database = self.storage_path / "sources.sqlite"
        self.docstore = TracedDocumentStore(
            create_kv_docstore(LocalFileStore(self.storage_path / "parents"))
        )
        self.parent_splitter = MarkdownTextSplitter(
            chunk_size=settings.rag_parent_chunk_size, chunk_overlap=0,
            add_start_index=True, strip_whitespace=False,
        )
        self.child_splitter = MarkdownTextSplitter(
            chunk_size=settings.rag_child_chunk_size,
            chunk_overlap=settings.rag_child_chunk_overlap,
            add_start_index=True, strip_whitespace=False,
        )
        parameters = (
            "parent_document_v1", settings.qwen_api_base, settings.qwen_embedding_model,
            settings.qwen_embedding_dimensions, settings.rag_parent_chunk_size,
            settings.rag_child_chunk_size, settings.rag_child_chunk_overlap,
        )
        self._fingerprint = hashlib.sha256(json.dumps(parameters).encode("utf-8")).hexdigest()
        self._storage_ready = False
        self._init_lock = RLock()

    def initialize(self) -> None:
        self._ensure_storage()
        self._vector_store_provider()
        self._cleanup_retired()

    def _ensure_storage(self) -> None:
        with self._init_lock:
            if self._storage_ready:
                return
            self.storage_path.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._database, timeout=self.settings.rag_storage_timeout) as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS sources (
                        source TEXT PRIMARY KEY,
                        generation TEXT NOT NULL,
                        parent_ids TEXT NOT NULL,
                        fingerprint TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS retired (
                        generation TEXT PRIMARY KEY,
                        parent_ids TEXT NOT NULL
                    );
                """)
            self._storage_ready = True

    @contextmanager
    def _records(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        self._ensure_storage()
        db = sqlite3.connect(
            self._database, timeout=self.settings.rag_storage_timeout, isolation_level=None
        )
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _retriever(self, *, search_kwargs: dict | None = None) -> ParentDocumentRetriever:
        return ParentDocumentRetriever(
            vectorstore=self._vector_store_provider(),
            docstore=self.docstore,
            parent_splitter=self.parent_splitter,
            child_splitter=self.child_splitter,
            id_key="parent_id",
            search_kwargs=search_kwargs or {"k": self.settings.rag_top_k},
        )

    @traceable(name="rag.index_document", run_type="chain")
    def index_document(self, document: Document) -> dict:
        source = document.metadata.get("_source")
        if not isinstance(source, str) or not source:
            raise ValueError("索引文档必须包含非空的 _source")
        if not document.page_content.strip():
            raise ValueError("不能索引空文档")
        generation = uuid4().hex
        staged = document.model_copy(deep=True)
        staged.metadata["_index_generation"] = generation
        staged.metadata["_index_fingerprint"] = self._fingerprint

        # 使用框架公开切分接口确定父块数量，显式 IDs 仅用于来源更新时的清理。
        # Retriever 仍执行原生父子切分、parent_id 关联及两层写入。
        parent_count = len(self.parent_splitter.split_documents([staged]))
        if not parent_count:
            raise ValueError("文档没有可索引的内容")
        parent_ids = [str(uuid4()) for _ in range(parent_count)]
        started = False
        try:
            with self._records(write=True) as db:
                old = db.execute(
                    "SELECT generation, parent_ids FROM sources WHERE source = ?", (source,)
                ).fetchone()
                retriever = self._retriever()
                started = True
                retriever.add_documents(
                    [staged], ids=parent_ids, timeout=self.settings.milvus_timeout / 1000
                )
                # 核实父存储写入完成后才让新版本进入检索范围。
                if any(parent is None for parent in self.docstore.mget(parent_ids)):
                    raise RuntimeError("父文档存储写入不完整")
                if old:
                    db.execute("INSERT OR IGNORE INTO retired VALUES (?, ?)", old)
                db.execute(
                    "INSERT INTO sources VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(source) DO UPDATE SET generation=excluded.generation, "
                    "parent_ids=excluded.parent_ids, fingerprint=excluded.fingerprint",
                    (source, generation, json.dumps(parent_ids), self._fingerprint),
                )
        except Exception:
            if started:
                try:
                    with self._records(write=True) as db:
                        db.execute(
                            "INSERT OR IGNORE INTO retired VALUES (?, ?)",
                            (generation, json.dumps(parent_ids)),
                        )
                    self._cleanup_retired()
                except Exception as cleanup_error:
                    logger.error("未完成索引版本清理失败 {}：{}", generation, cleanup_error)
            raise

        self._cleanup_retired()
        logger.info("父子索引已更新：{}，父文档 {} 个", source, parent_count)
        return {"source": source, "parent_count": parent_count, "generation": generation}

    @traceable(name="rag.retrieve", run_type="chain")
    def retrieve(self, query: str, top_k: int | None = None) -> list[Document]:
        if not query or not query.strip():
            raise ValueError("检索问题不能为空")
        k = self.settings.rag_top_k if top_k is None else top_k
        if k <= 0:
            raise ValueError("top_k 必须大于 0")
        with self._records() as db:
            rows = db.execute(
                "SELECT generation FROM sources WHERE fingerprint = ?", (self._fingerprint,)
            ).fetchall()
            if not rows:
                return []
            generations = [row[0] for row in rows]
            expr = "_index_generation in " + json.dumps(generations)
            # 原生 invoke 完成子向量检索、parent_id 去重和父文档回取。
            return self._retriever(search_kwargs={
                "k": k, "expr": expr, "timeout": self.settings.milvus_timeout / 1000,
            }).invoke(
                query,
                config={"run_name": "rag.parent_retriever", "tags": ["rag", "parent-child"]},
            )

    def delete_source(self, source: str) -> None:
        with self._records(write=True) as db:
            old = db.execute(
                "SELECT generation, parent_ids FROM sources WHERE source = ?", (source,)
            ).fetchone()
            if old:
                db.execute("INSERT OR IGNORE INTO retired VALUES (?, ?)", old)
                db.execute("DELETE FROM sources WHERE source = ?", (source,))
        self._cleanup_retired()

    def _cleanup_retired(self) -> None:
        try:
            with self._records() as db:
                rows = db.execute("SELECT generation, parent_ids FROM retired").fetchall()
        except Exception as exc:
            logger.warning("读取索引清理队列失败，稍后重试：{}", exc)
            return
        if not rows:
            return
        for generation, serialized_ids in rows:
            try:
                store = self._vector_store_provider()
                # 尚未创建集合时，没有该版本的子向量可删。
                if getattr(store, "col", True) is not None:
                    deleted = store.delete(
                        expr="_index_generation == " + json.dumps(generation),
                        timeout=self.settings.milvus_timeout / 1000,
                    )
                    if deleted is False:
                        raise RuntimeError("Milvus 未能删除旧版本子向量")
                self.docstore.mdelete(json.loads(serialized_ids))
                with self._records(write=True) as db:
                    db.execute("DELETE FROM retired WHERE generation = ?", (generation,))
            except Exception as exc:
                # 旧版本已被过滤；保留清理队列，在下一次更新或启动时重试。
                logger.warning("索引旧版本 {} 清理待重试：{}", generation, exc)


parent_document_service = ParentDocumentService()
