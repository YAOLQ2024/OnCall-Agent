"""Optional vector adapter contract without contacting Milvus or the embedding API."""

import sqlite3
import time
from contextlib import closing
from unittest.mock import Mock

import pytest
from pymilvus import DataType

from app.config import config
from app.models.memory import MemoryScope, ProfilePatch
from app.models.memory_runtime import MemoryRunContext
from app.services.memory_vector_store import MemoryVectorStore
from app.storage.memory_incidents import IncidentIndexRef
from test_memory_incidents import ALICE, confirm, create, stores  # noqa: F401 -- shared fixture


def vector_fixture(incidents):
    settings = config.model_copy(update={
        "ltm_enabled": True, "ltm_vector_enabled": True, "ltm_paid_calls_enabled": True,
        "ltm_daily_embedding_item_budget": 1, "qwen_embedding_dimensions": 2,
    })
    client = Mock()
    embedding = Mock()
    embedding.embed_query.return_value = [0.1, 0.2]
    client_factory, embedding_factory = Mock(return_value=client), Mock(return_value=embedding)
    store = MemoryVectorStore(settings, incidents, client_factory=client_factory, embedding_factory=embedding_factory)
    fields = [
        {"name": key, "type": DataType.VARCHAR, "params": {"max_length": 256}, "is_primary": key == "vector_id"}
        for key in ("vector_id", "memory_id", "user_id", "content_hash", "embedding_schema", "environment", "cluster", "namespace")
    ]
    fields += [{"name": key, "type": DataType.INT64} for key in ("revision", "generation", "expires_epoch")]
    fields.append({"name": "embedding", "type": DataType.FLOAT_VECTOR, "params": {"dim": 2}})
    client.describe_collection.return_value = {"fields": fields, "description": store.schema_key}
    client.search.return_value = [[]]
    return store, client, embedding, client_factory, embedding_factory


def test_vector_reads_filtered_refs_and_reserves_one_item_before_provider(stores):
    _, db, incidents = stores
    store, client, model, client_factory, embedding_factory = vector_fixture(incidents)
    client.search.return_value = [[
        {"entity": {"user_id": ALICE, "memory_id": "case", "revision": 2, "generation": 3, "content_hash": "hash"}},
        {"entity": {"user_id": "someone-else"}},
    ]]
    context = MemoryRunContext(ALICE, "chat", "request", generation=3)
    scope = MemoryScope(environment="production", namespace="pay")
    result = store.search(context, "ERR-503", scope, 5, deadline=time.monotonic() + 10)
    assert result == [IncidentIndexRef("case", 2, 3, "hash")]
    embedding_factory.assert_called_once()
    assert embedding_factory.call_args.kwargs["max_retries"] == 0
    model.client.close.assert_called_once()
    client.close.assert_called_once()
    args = client.search.call_args.kwargs
    assert args["filter_params"]["owner"] == ALICE and ALICE not in args["filter"]
    assert args["filter_params"]["environment"] == "production" and args["filter_params"]["namespace"] == "pay"
    assert args["filter_params"]["generation"] == 3 and args["limit"] == 5
    assert args["consistency_level"] == "Strong"
    with pytest.raises(RuntimeError, match="budget exhausted"):
        store.search(MemoryRunContext(ALICE, "chat", "later"), "ERR-503", scope, 5, deadline=time.monotonic() + 10)
    aliases = [call.kwargs["alias"] for call in client_factory.call_args_list]
    assert len(set(aliases)) == len(aliases) == 2
    with closing(sqlite3.connect(db.db_path)) as conn:
        assert conn.execute("SELECT embedding_items, status FROM ltm_usage").fetchall() == [(1, "succeeded")]
    model.embed_query.assert_called_once()


def test_incompatible_schema_and_disabled_paid_calls_fail_before_embedding(stores):
    _, _, incidents = stores
    store, client, model, client_factory, _ = vector_fixture(incidents)
    store.settings.ltm_paid_calls_enabled = False
    context = MemoryRunContext(ALICE, "chat", "request")
    with pytest.raises(RuntimeError, match="explicit opt-in"):
        store.search(context, "ERR-503", MemoryScope(), 5, deadline=time.monotonic() + 10)
    client_factory.assert_not_called()
    store.settings.ltm_paid_calls_enabled = True
    client.describe_collection.return_value["fields"][-1]["params"]["dim"] = 3
    with pytest.raises(ValueError, match="Incompatible"):
        store.search(context, "ERR-503", MemoryScope(), 5, deadline=time.monotonic() + 10)
    model.embed_query.assert_not_called()
    client.close.assert_called_once()


def test_explicit_index_sync_uses_stable_ids_and_only_current_confirmed_revisions(stores):
    _, db, incidents = stores
    db.patch_profile(ALICE, ProfilePatch(expected_revision=0, use_enabled=True))
    create(incidents, title="draft")
    incident = confirm(incidents, create(incidents, title="eligible"))
    store, client, model, _, _ = vector_fixture(incidents)
    client.get.side_effect = [[], [{"vector_id": "existing"}]]
    assert store.sync_confirmed(ALICE) == 1
    payload = client.upsert.call_args.kwargs["data"][0]
    assert payload["memory_id"] == incident["memory_id"] and payload["revision"] == incident["revision"]
    assert "incident_json" not in payload
    assert store.sync_confirmed(ALICE) == 0
    assert client.get.call_args.kwargs["ids"] == [payload["vector_id"]]
    assert client.get.call_args.kwargs["consistency_level"] == "Strong"
    model.embed_query.assert_called_once()
