"""Milvus 连接生命周期和健康检查；不创建、删除或重建业务集合。"""

from pymilvus import Collection, MilvusClient, connections, utility

from app.config import config


class MilvusClientManager:
    def __init__(self) -> None:
        self._client: MilvusClient | None = None

    def connect(self) -> MilvusClient:
        if self._client is None:
            connections.connect(
                alias="default", host=config.milvus_host, port=str(config.milvus_port),
                timeout=config.milvus_timeout / 1000,
            )
            self._client = MilvusClient(uri=f"http://{config.milvus_host}:{config.milvus_port}")
        return self._client

    def get_collection(self) -> Collection:
        if not utility.has_collection(config.rag_collection_name):
            raise RuntimeError("父子召回集合尚未创建，请先上传文档或重建目录索引")
        return Collection(config.rag_collection_name)

    def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            self._client.list_collections()
            return True
        except Exception:
            return False

    def close(self) -> None:
        try:
            if self._client is not None:
                self._client.close()
        finally:
            self._client = None
            if connections.has_connection("default"):
                connections.disconnect("default")


milvus_manager = MilvusClientManager()
