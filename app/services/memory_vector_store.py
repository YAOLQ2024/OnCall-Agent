"""Optional Milvus accelerator. SQLite remains the authority for every vector hit."""

import argparse
import hashlib
import json
import math
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime

from langsmith import tracing_context

from app.models.memory import MemoryScope
from app.storage.memory_incident_index import NORMALIZER_VERSION
from app.storage.memory_incidents import IncidentIndexRef

_TEXT_FIELDS = {
    "vector_id": 64, "memory_id": 32, "user_id": 256, "content_hash": 64, "embedding_schema": 64,
    "environment": 128, "cluster": 128, "namespace": 63,
}
_INT_FIELDS = ("revision", "generation", "expires_epoch")
_REF_FIELDS = ("memory_id", "revision", "generation", "content_hash")


class EmbeddingBudgetError(RuntimeError):
    pass


class MemoryVectorStore:
    def __init__(self, settings, incidents, *, client_factory=None, embedding_factory=None):
        self.settings = settings
        self.incidents = incidents
        self.client_factory = client_factory
        self.embedding_factory = embedding_factory
        self.name = settings.ltm_collection_name
        self.dimension = settings.qwen_embedding_dimensions
        self.model = settings.qwen_embedding_model
        self.schema_key = hashlib.sha256(f"{self.model}:{self.dimension}:v{NORMALIZER_VERSION}".encode()).hexdigest()
        self.timeout = min(2.0, settings.ltm_recall_timeout_seconds * 0.25)

    def _require_enabled(self):
        if not (self.settings.ltm_enabled and self.settings.ltm_vector_enabled and self.settings.ltm_paid_calls_enabled
                and self.settings.ltm_daily_embedding_item_budget > 0):
            raise RuntimeError("Memory vector calls require explicit opt-in and a positive budget")
        if self.name == self.settings.rag_collection_name:
            raise ValueError("Memory collection must be separate from the knowledge collection")

    @contextmanager
    def _client(self):
        from pymilvus import MilvusClient

        create = self.client_factory or MilvusClient
        # PyMilvus shares a connection by URI unless an explicit alias is supplied.
        uri = f"http://{self.settings.milvus_host}:{self.settings.milvus_port}"
        alias = f"oncall_memory_{uuid.uuid4().hex}"
        with closing(create(uri=uri, timeout=self.timeout, alias=alias)) as client:
            yield client

    def _describe(self, client):
        from pymilvus import DataType

        schema = client.describe_collection(self.name, timeout=self.timeout)
        fields = {field["name"]: field for field in schema["fields"]}
        expected = {**dict.fromkeys(_TEXT_FIELDS, DataType.VARCHAR), **dict.fromkeys(_INT_FIELDS, DataType.INT64),
                    "embedding": DataType.FLOAT_VECTOR}
        if (schema.get("auto_id") or schema.get("enable_dynamic_field")
                or set(fields) != set(expected)
                or any(fields[key]["type"] != dtype for key, dtype in expected.items())
                or any(int(fields[key]["params"]["max_length"]) < length for key, length in _TEXT_FIELDS.items())
                or int(fields["embedding"]["params"]["dim"]) != self.dimension
                or not fields["vector_id"].get("is_primary")
                or schema.get("description") != self.schema_key):
            raise ValueError("Incompatible memory vector collection; use a new collection name for a new embedding schema")

    def _create_collection(self, client):
        from pymilvus import DataType

        schema = client.create_schema(auto_id=False, enable_dynamic_field=False, description=self.schema_key)
        for key, length in _TEXT_FIELDS.items():
            schema.add_field(key, DataType.VARCHAR, is_primary=key == "vector_id", max_length=length)
        for key in _INT_FIELDS:
            schema.add_field(key, DataType.INT64)
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self.dimension)
        index = client.prepare_index_params()
        index.add_index("embedding", index_type="AUTOINDEX", metric_type="COSINE")
        client.create_collection(self.name, schema=schema, index_params=index, timeout=self.timeout)

    def _embed(self, uid, request_id, kind, text, *, deadline=None):
        from app.services.vector_embedding_service import QwenEmbeddings

        db = self.incidents.db
        reservation = db.reserve_embedding(uid, request_id, kind, self.model, self.settings.ltm_daily_embedding_item_budget)
        if reservation is None:
            raise EmbeddingBudgetError("Memory embedding budget exhausted")
        succeeded = False
        try:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Memory embedding deadline exceeded before the call")
            create = self.embedding_factory or QwenEmbeddings
            model = create(api_key=self.settings.qwen_api_key, base_url=self.settings.qwen_api_base,
                           model=self.model, dimensions=self.dimension, timeout=self.timeout, max_retries=0)
            with closing(model.client), tracing_context(enabled=False):
                result = model.embed_query(text)
            if len(result) != self.dimension or not all(math.isfinite(value) for value in result):
                raise ValueError("Invalid memory embedding dimension or values")
            succeeded = True
            return result
        finally:
            db.finish_embedding(reservation, succeeded=succeeded)

    def search(self, context, text: str, scope: MemoryScope, limit: int, *, deadline: float):
        self._require_enabled()
        params = {"owner": context.user_id, "generation": context.generation, "schema": self.schema_key,
                  "now": int(time.time()), **scope.model_dump(exclude_none=True)}
        expression = "user_id == {owner} and generation == {generation} and embedding_schema == {schema} and expires_epoch > {now}"
        for key in scope.model_dump(exclude_none=True):
            expression += f" and {key} == {{{key}}}"
        with self._client() as client:
            self._describe(client)
            if time.monotonic() >= deadline:
                return []
            vector = self._embed(context.user_id, context.request_id, "query_embedding", text, deadline=deadline)
            if time.monotonic() >= deadline:
                return []
            results = client.search(self.name, data=[vector], anns_field="embedding", search_params={"metric_type": "COSINE"},
                                    filter=expression, filter_params=params, limit=limit,
                                    output_fields=[*_REF_FIELDS, "user_id"], consistency_level="Strong",
                                    timeout=self.timeout)
        return [IncidentIndexRef(*(hit["entity"][key] for key in _REF_FIELDS))
                for hit in (results[0] if results else []) if hit["entity"]["user_id"] == context.user_id]

    def index_job(self, job):
        self._require_enabled()
        ref = IncidentIndexRef(job["memory_id"], job["expected_revision"], job["expected_generation"], job["source_hash"])
        if not self.incidents.verified_details(job["user_id"], MemoryScope(), ref.generation, [ref]):
            return 0
        with self._client() as client:
            if not client.has_collection(self.name, timeout=self.timeout):
                try:
                    self._create_collection(client)
                except Exception:
                    if not client.has_collection(self.name, timeout=self.timeout):
                        raise
            self._describe(client)
            return self._index(client, job["user_id"], ref.generation, ref)

    def purge_job(self, job):
        if self.name == self.settings.rag_collection_name:
            raise ValueError("Memory collection must be separate from the knowledge collection")
        with self._client() as client:
            if not client.has_collection(self.name, timeout=self.timeout):
                return
            self._describe(client)
            client.delete(
                self.name,
                filter="user_id == {owner} and memory_id == {mid} and generation == {generation} and revision < {revision}",
                filter_params={"owner": job["user_id"], "mid": job["memory_id"],
                               "generation": job["expected_generation"], "revision": job["expected_revision"]},
                timeout=self.timeout,
            )

    def sync_confirmed(self, uid: str) -> int:
        """Explicit local maintenance. Background outbox consumption belongs to M4."""
        self._require_enabled()
        profile = self.incidents.db.read_runtime_profile(uid)
        if not profile or not profile.use_enabled:
            raise ValueError("This user has not enabled memory use")
        count, cursor = 0, ""
        with self._client() as client:
            if not client.has_collection(self.name, timeout=self.timeout):
                self._create_collection(client)
            self._describe(client)
            while refs := self.incidents.current_index_refs(uid, cursor, 50):
                for ref in refs:
                    count += self._index(client, uid, profile.generation, ref)
                cursor = refs[-1].memory_id
        return count

    def _index(self, client, uid, generation, ref):
        profile = self.incidents.db.read_runtime_profile(uid)
        if not profile or not profile.use_enabled or profile.generation != generation:
            return 0
        rows = self.incidents.verified_details(uid, MemoryScope(), generation, [ref])
        if not rows:
            return 0
        vector_id = hashlib.sha256(f"{ref.memory_id}:{ref.revision}:{self.schema_key}".encode()).hexdigest()
        if client.get(self.name, ids=[vector_id], output_fields=["vector_id"],
                      consistency_level="Strong", timeout=self.timeout):
            return 0
        data = json.loads(rows[0]["incident_json"])
        text = "\n".join([data["title"], data.get("symptoms", ""), ", ".join(data.get("services", [])),
                          ", ".join(data.get("error_codes", []))])[:1000]
        embedding = self._embed(uid, uuid.uuid4().hex, "index_embedding", text)
        current = self.incidents.db.read_runtime_profile(uid)
        if (not current or not current.use_enabled
                or not self.incidents.verified_details(uid, MemoryScope(), generation, [ref])):
            return 0
        expiry = rows[0]["expires_at"]
        client.upsert(self.name, data=[{
            "vector_id": vector_id, "user_id": uid, "memory_id": ref.memory_id, "revision": ref.revision,
            "generation": ref.generation, "content_hash": ref.content_hash, "embedding_schema": self.schema_key,
            **{key: data.get(key) or "" for key in MemoryScope.model_fields},
            "expires_epoch": math.ceil(datetime.fromisoformat(expiry).timestamp()) if expiry else 2**63 - 1,
            "embedding": embedding,
        }], timeout=self.timeout)
        if not self.incidents.verified_details(uid, MemoryScope(), generation, [ref]):
            client.delete(self.name, ids=[vector_id], timeout=self.timeout)
            return 0
        return 1


if __name__ == "__main__":
    from app.config import config
    from app.storage.memory_db import MemoryDatabase
    from app.storage.memory_incidents import IncidentRepository
    from app.storage.session_db import session_db_manager

    parser = argparse.ArgumentParser(description="Index one opted-in user's confirmed incidents using the configured embedding budget.")
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()
    incidents = IncidentRepository(MemoryDatabase(session_db_manager.db_path))
    indexed = MemoryVectorStore(config, incidents).sync_confirmed(args.user_id)
    print(f"Indexed {indexed} incident revisions.")
