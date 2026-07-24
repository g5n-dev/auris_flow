from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from typing import Literal

import pytest

from app.core import embeddings as embedding_module
from app.services import adapters as adapter_module
from app.services.adapters import RealQdrantIndexClient


class RecordingEmbeddingProvider:
    provider_name = "http"
    model_name = "multilingual-semantic-v1"
    dimension = 4
    is_semantic = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def embed(
        self,
        text: str,
        *,
        purpose: Literal["document", "query"],
    ) -> list[float]:
        self.calls.append((text, purpose))
        return [0.1, 0.2, 0.3, 0.4]


class RecordingQdrantClient(RealQdrantIndexClient):
    def __init__(self, provider: RecordingEmbeddingProvider) -> None:
        super().__init__(
            base_url="http://qdrant.example.test",
            vector_size=provider.dimension,
            embedding_provider=provider,
        )
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []

    def _ensure_collection(self, collection: str) -> None:
        self.requests.append(("ENSURE", collection, None))

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.requests.append((method, path, body))
        if path.endswith("/points/search"):
            return {"result": []}
        return {"result": {"operation_id": 7, "status": "completed"}}


def _qdrant_payload() -> dict[str, object]:
    return {
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "trace_id": "trace_embedding",
        "collection": "knowledge_chunks",
        "knowledge_index_id": "ki_001",
        "knowledge_source_id": "ks_001",
        "source_id": "ks_001",
        "source_type": "sop",
        "asset_key": "auris/knowledge/ks_001",
        "version": "v1",
        "business_ref": {"source_name": "报价规则"},
        "embedding_text": "报价金额冲突处理规则",
    }


def _embedding_fingerprint(provider: RecordingEmbeddingProvider) -> str:
    return embedding_module.embedding_space_v1_fingerprint(provider, distance="Cosine")


def _authorized_search_payload(
    provider: RecordingEmbeddingProvider,
    point_ids: object,
) -> dict[str, object]:
    return {
        **_qdrant_payload(),
        "embedding_space_fingerprint": _embedding_fingerprint(provider),
        "_authorized_point_ids": point_ids,
    }


def test_embedding_space_v1_fingerprint_is_stable_and_excludes_transport_configuration() -> None:
    provider_a = embedding_module.HTTPEmbeddingProvider(
        endpoint="https://embedding-a.example.test/v1/embeddings",
        model="multilingual-semantic-v1",
        dimension=4,
        api_key="first-secret",
        timeout_seconds=3,
    )
    provider_b = embedding_module.HTTPEmbeddingProvider(
        endpoint="https://embedding-b.example.test/private/embeddings",
        model="multilingual-semantic-v1",
        dimension=4,
        api_key="second-secret",
        timeout_seconds=57,
    )

    contract = embedding_module.embedding_space_v1_contract(provider_a, distance="Cosine")
    fingerprint_a = embedding_module.embedding_space_v1_fingerprint(
        provider_a,
        distance="Cosine",
    )
    fingerprint_b = embedding_module.embedding_space_v1_fingerprint(
        provider_b,
        distance="Cosine",
    )

    assert contract == {
        "version": "auris.embedding-space.v1",
        "provider": "http",
        "model": "multilingual-semantic-v1",
        "dimension": 4,
        "semantic": True,
        "distance": "Cosine",
        "query_contract": {
            "purpose": "query",
            "normalization": "trim-nonempty-utf8-v1",
            "max_input_bytes": 2 * 1024 * 1024,
        },
        "document_contract": {
            "purpose": "document",
            "normalization": "trim-nonempty-utf8-v1",
            "max_input_bytes": 2 * 1024 * 1024,
            "source_precedence": [
                "embedding_text",
                "document_text",
                "content",
                "text",
            ],
        },
    }
    assert fingerprint_a == fingerprint_b
    assert len(fingerprint_a) == 64
    assert fingerprint_a == fingerprint_a.lower()
    assert "embedding-a" not in json.dumps(contract)
    assert "first-secret" not in json.dumps(contract)
    assert "timeout" not in json.dumps(contract)


@pytest.mark.parametrize(
    ("attribute", "replacement"),
    [
        ("provider_name", "another-provider"),
        ("model_name", "another-model"),
        ("dimension", 8),
        ("is_semantic", False),
    ],
)
def test_embedding_space_v1_fingerprint_changes_for_semantic_contract_fields(
    attribute: str,
    replacement: object,
) -> None:
    provider = RecordingEmbeddingProvider()
    changed_provider = RecordingEmbeddingProvider()
    setattr(changed_provider, attribute, replacement)

    baseline = embedding_module.embedding_space_v1_fingerprint(provider, distance="Cosine")
    changed = embedding_module.embedding_space_v1_fingerprint(
        changed_provider,
        distance="Cosine",
    )

    assert changed != baseline


def test_embedding_space_v1_fingerprint_changes_with_distance_contract() -> None:
    provider = RecordingEmbeddingProvider()

    cosine = embedding_module.embedding_space_v1_fingerprint(provider, distance="Cosine")
    dot = embedding_module.embedding_space_v1_fingerprint(provider, distance="Dot")

    assert cosine != dot


def test_real_qdrant_upsert_records_the_same_embedding_fingerprint_remotely_and_locally() -> None:
    provider = RecordingEmbeddingProvider()
    client = RecordingQdrantClient(provider)

    dispatch = client.upsert_index_payload({"qdrant_payload": _qdrant_payload()})

    assert dispatch.status == "success"
    expected_fingerprint = _embedding_fingerprint(provider)
    upsert_body = next(
        body for method, path, body in client.requests if method == "PUT" and "points?wait" in path
    )
    assert upsert_body is not None
    remote_point = upsert_body["points"][0]  # type: ignore[index]
    remote_payload = remote_point["payload"]
    receipt_payload = dispatch.details["qdrant_payload"]
    assert remote_point["id"] == "f629145b-d8b9-5877-952d-7818f04e90b9"
    assert remote_payload["embedding_space_fingerprint"] == expected_fingerprint
    assert receipt_payload["embedding_space_fingerprint"] == expected_fingerprint
    assert dispatch.details["embedding_space_fingerprint"] == expected_fingerprint
    assert remote_payload == receipt_payload


def test_real_qdrant_request_emits_only_a_low_cardinality_operation_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_spans: list[tuple[str, dict[str, object]]] = []

    @contextmanager
    def recording_span(
        name: str,
        *,
        attributes: dict[str, object] | None = None,
        parent_context: object | None = None,
    ):
        del parent_context
        observed_spans.append((name, dict(attributes or {})))
        yield object()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        @staticmethod
        def read(_size: int = -1) -> bytes:
            return b'{"status":"ok","result":{"status":"completed"}}'

    monkeypatch.setattr(adapter_module, "internal_span", recording_span, raising=False)
    monkeypatch.setattr(adapter_module, "urlopen", lambda _request, timeout: Response())
    provider = RecordingEmbeddingProvider()
    client = RealQdrantIndexClient(
        base_url="http://qdrant:6333",
        vector_size=provider.dimension,
        embedding_provider=provider,
    )

    client._request(  # noqa: SLF001
        "PUT",
        "/collections/private-tenant-collection/points?wait=true",
        {"points": []},
    )

    assert observed_spans == [
        (
            "qdrant.request",
            {
                "auris.qdrant.operation": "points.upsert",
                "http.request.method": "PUT",
            },
        )
    ]
    assert "private-tenant-collection" not in json.dumps(observed_spans)


def test_real_qdrant_search_requires_explicit_authorized_point_ids() -> None:
    provider = RecordingEmbeddingProvider()
    client = RecordingQdrantClient(provider)
    payload = {
        **_qdrant_payload(),
        "embedding_space_fingerprint": _embedding_fingerprint(provider),
    }

    with pytest.raises(ValueError, match="authorized point ids are required"):
        client.search_index_payload(payload, query="报价冲突", top_k=3)

    assert provider.calls == []
    assert client.requests == []


@pytest.mark.parametrize(
    "point_ids",
    [
        "11111111-1111-4111-8111-111111111111",
        ["11111111-1111-4111-8111-111111111111", "not-a-uuid"],
        [" 11111111-1111-4111-8111-111111111111"],
        ["11111111-1111-4111-8111-11111111111A"],
    ],
)
def test_real_qdrant_search_rejects_the_entire_malformed_authority_set(
    point_ids: object,
) -> None:
    provider = RecordingEmbeddingProvider()
    client = RecordingQdrantClient(provider)

    with pytest.raises(ValueError, match="authorized point ids are invalid"):
        client.search_index_payload(
            _authorized_search_payload(provider, point_ids),
            query="报价冲突",
            top_k=3,
        )

    assert provider.calls == []
    assert client.requests == []


def test_real_qdrant_search_rejects_authority_sets_above_the_hard_limit() -> None:
    provider = RecordingEmbeddingProvider()
    client = RecordingQdrantClient(provider)
    oversized = [str(uuid.uuid5(uuid.NAMESPACE_URL, f"authority:{index}")) for index in range(1025)]

    with pytest.raises(ValueError, match="authorized point ids exceed limit"):
        client.search_index_payload(
            _authorized_search_payload(provider, oversized),
            query="报价冲突",
            top_k=3,
        )

    assert provider.calls == []
    assert client.requests == []


def test_real_qdrant_search_short_circuits_an_empty_authority_set() -> None:
    provider = RecordingEmbeddingProvider()
    client = RecordingQdrantClient(provider)

    result = client.search_index_payload(
        _authorized_search_payload(provider, []),
        query="报价冲突",
        top_k=3,
    )

    assert result["points"] == []
    assert result["filter"] == {
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "knowledge_index_id": "ki_001",
        "embedding_space_fingerprint": _embedding_fingerprint(provider),
        "authorized_point_count": 0,
    }
    assert provider.calls == []
    assert client.requests == []


def test_real_qdrant_search_deduplicates_ids_without_echoing_them_in_filter_metadata() -> None:
    provider = RecordingEmbeddingProvider()
    client = RecordingQdrantClient(provider)
    point_id = "11111111-1111-4111-8111-111111111111"

    result = client.search_index_payload(
        _authorized_search_payload(provider, [point_id, point_id]),
        query="报价冲突",
        top_k=3,
    )

    search_body = next(body for method, path, body in client.requests if path.endswith("search"))
    assert search_body is not None
    assert search_body["filter"]["must"][-1] == {"has_id": [point_id]}  # type: ignore[index]
    assert result["filter"]["authorized_point_count"] == 1
    assert "has_id" not in result["filter"]
    assert point_id not in json.dumps(result["filter"])


def test_real_qdrant_search_rejects_a_stale_embedding_space_before_query_embedding() -> None:
    provider = RecordingEmbeddingProvider()
    client = RecordingQdrantClient(provider)
    point_id = "11111111-1111-4111-8111-111111111111"
    payload = _authorized_search_payload(provider, [point_id])
    payload["embedding_space_fingerprint"] = "0" * 64

    with pytest.raises(ValueError, match="embedding space fingerprint does not match"):
        client.search_index_payload(payload, query="报价冲突", top_k=3)

    assert provider.calls == []
    assert client.requests == []
