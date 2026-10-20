"""给框架存储的真实调用补充 LangSmith 节点，不改写检索或存储算法。"""

from collections.abc import Iterator, Sequence

from langchain_core.documents import Document
from langchain_core.stores import BaseStore
from langchain_milvus import Milvus
from langsmith import traceable


def document_trace_output(documents: Sequence[Document | None] | None) -> dict:
    """只调整追踪序列化，调用方收到的 Document 对象保持原样。"""
    return {
        "documents": [
            doc.model_dump(mode="json") if doc is not None else None for doc in documents or []
        ]
    }


class TracedMilvus(Milvus):
    @traceable(
        name="rag.child_search", run_type="retriever", process_outputs=document_trace_output
    )
    def similarity_search(self, query: str, k: int = 4, **kwargs) -> list[Document]:
        return super().similarity_search(query, k=k, **kwargs)

    @traceable(name="rag.index_children", run_type="chain")
    def add_documents(self, documents: list[Document], **kwargs) -> list[str]:
        return super().add_documents(documents, **kwargs)


class TracedDocumentStore(BaseStore[str, Document]):
    """父文档存储适配器，读取时将存储键暴露为 Document.id。"""
    def __init__(self, store: BaseStore[str, Document]) -> None:
        self._store = store

    @traceable(
        name="rag.parent_fetch", run_type="retriever", process_outputs=document_trace_output
    )
    def mget(self, keys: Sequence[str]) -> list[Document | None]:
        documents = self._store.mget(keys)
        # 父存储键就是子文档 metadata.parent_id。返回副本，兼容已有索引，
        # 不修改持久化正文/metadata，也不把原文级 document_id 当成父块 ID。
        return [
            document.model_copy(update={"id": key}) if document is not None else None
            for key, document in zip(keys, documents, strict=True)
        ]

    @traceable(name="rag.store_parents", run_type="chain")
    def mset(self, key_value_pairs: Sequence[tuple[str, Document]]) -> None:
        self._store.mset(key_value_pairs)

    def mdelete(self, keys: Sequence[str]) -> None:
        self._store.mdelete(keys)

    def yield_keys(self, *, prefix: str | None = None) -> Iterator[str]:
        yield from self._store.yield_keys(prefix=prefix)
