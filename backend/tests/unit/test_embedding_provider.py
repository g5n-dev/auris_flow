from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from app.core.embeddings import (
    EMBEDDING_MAX_RESPONSE_BYTES,
    DeterministicTestEmbeddingProvider,
    EmbeddingConfigurationError,
    EmbeddingResponseError,
    HTTPEmbeddingProvider,
    build_embedding_provider,
)
from app.services.adapters import RealQdrantIndexClient


class Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return json.dumps(self.payload).encode("utf-8")[:size]


def test_http_embedding_provider_bounds_response_read_before_buffering() -> None:
    read_sizes: list[int] = []

    class OversizedResponse(Response):
        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return b"x" * size

    provider = HTTPEmbeddingProvider(
        endpoint="https://embeddings.example.test/v1/embeddings",
        model="multilingual-semantic-v1",
        dimension=4,
        transport=lambda _request, _timeout: OversizedResponse({}),
    )

    with pytest.raises(EmbeddingResponseError, match="too large"):
        provider.embed("query", purpose="query")

    assert read_sizes == [EMBEDDING_MAX_RESPONSE_BYTES + 1]


def test_http_embedding_provider_uses_semantic_endpoint_without_leaking_key() -> None:
    captured: dict[str, object] = {}

    def transport(request: Request, timeout: float) -> Response:
        captured["url"] = request.full_url
        captured["body"] = json.loads((request.data or b"{}").decode("utf-8"))
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return Response({"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]})

    provider = HTTPEmbeddingProvider(
        endpoint="https://embeddings.example.test/v1/embeddings",
        model="multilingual-semantic-v1",
        dimension=4,
        api_key="unit-http-embedding-fixture-key",
        transport=transport,
    )

    vector = provider.embed("报价金额冲突处理 SOP", purpose="query")

    assert vector == [0.1, 0.2, 0.3, 0.4]
    assert captured["url"] == "https://embeddings.example.test/v1/embeddings"
    assert captured["body"] == {
        "input": ["报价金额冲突处理 SOP"],
        "model": "multilingual-semantic-v1",
        "input_type": "query",
    }
    assert captured["authorization"] == "Bearer unit-http-embedding-fixture-key"
    assert captured["timeout"] == 10.0
    assert "unit-http-embedding-fixture-key" not in repr(provider)


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {"data": [{"embedding": [0.1, 0.2]}]},
        {"data": [{"embedding": [0.1, float("nan"), 0.3, 0.4]}]},
        {"embeddings": [[0.1, "bad", 0.3, 0.4]]},
    ],
)
def test_http_embedding_provider_rejects_invalid_vectors(payload: dict[str, object]) -> None:
    provider = HTTPEmbeddingProvider(
        endpoint="https://embeddings.example.test/v1/embeddings",
        model="multilingual-semantic-v1",
        dimension=4,
        transport=lambda _request, _timeout: Response(payload),
    )

    with pytest.raises(EmbeddingResponseError):
        provider.embed("query", purpose="query")


def test_http_embedding_provider_does_not_include_remote_error_body() -> None:
    def transport(_request: Request, _timeout: float) -> Response:
        raise HTTPError(
            "https://embeddings.example.test/v1/embeddings",
            401,
            "fixture-secret-must-not-leak",
            {},
            None,
        )

    provider = HTTPEmbeddingProvider(
        endpoint="https://embeddings.example.test/v1/embeddings",
        model="multilingual-semantic-v1",
        dimension=4,
        transport=transport,
    )

    with pytest.raises(EmbeddingResponseError, match="HTTP 401") as error:
        provider.embed("query", purpose="query")

    assert "fixture-secret-must-not-leak" not in str(error.value)


def test_http_embedding_provider_refuses_redirects_without_forwarding_authorization() -> None:
    redirected_headers: list[str | None] = []

    class RedirectTarget(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            redirected_headers.append(self.headers.get("Authorization"))
            payload = json.dumps({"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    target = ThreadingHTTPServer(("127.0.0.1", 0), RedirectTarget)
    target_url = f"http://127.0.0.1:{target.server_port}/redirect-target"

    class RedirectSource(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            self.send_response(302)
            self.send_header("Location", target_url)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    source = ThreadingHTTPServer(("127.0.0.1", 0), RedirectSource)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True) for server in (target, source)
    ]
    for thread in threads:
        thread.start()
    try:
        provider = HTTPEmbeddingProvider(
            endpoint=f"http://127.0.0.1:{source.server_port}/v1/embeddings",
            model="multilingual-semantic-v1",
            dimension=4,
            api_key="redirect-auth-fixture-key",
        )

        with pytest.raises(EmbeddingResponseError, match="HTTP 302"):
            provider.embed("query", purpose="query")

        assert redirected_headers == []
    finally:
        for server in (source, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_production_refuses_deterministic_test_embedding_provider(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("AURIS_EMBEDDING_PROVIDER", "deterministic_test")

    with pytest.raises(EmbeddingConfigurationError, match="forbidden"):
        build_embedding_provider()


def test_http_embedding_provider_requires_explicit_production_configuration(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "release")
    monkeypatch.setenv("AURIS_EMBEDDING_PROVIDER", "http")
    monkeypatch.delenv("EMBEDDING_ENDPOINT", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)

    with pytest.raises(EmbeddingConfigurationError, match="EMBEDDING_ENDPOINT"):
        build_embedding_provider()


def test_deterministic_provider_is_explicitly_marked_test_only() -> None:
    provider = DeterministicTestEmbeddingProvider(dimension=4)

    assert provider.provider_name == "deterministic_test"
    assert provider.model_name == "sha256-test-vector"
    assert provider.is_semantic is False
    assert provider.embed("same", purpose="document") == provider.embed("same", purpose="document")
    assert provider.embed("same", purpose="document") != provider.embed("same", purpose="query")


def test_real_qdrant_uses_injected_semantic_provider_for_document_and_query() -> None:
    class SemanticProvider:
        provider_name = "http"
        model_name = "multilingual-semantic-v1"
        dimension = 4
        is_semantic = True

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def embed(self, text: str, *, purpose: str) -> list[float]:
            self.calls.append((text, purpose))
            return [0.1, 0.2, 0.3, 0.4] if purpose == "document" else [0.4, 0.3, 0.2, 0.1]

    class QdrantClient(RealQdrantIndexClient):
        def __init__(self, provider: SemanticProvider) -> None:
            super().__init__(
                base_url="http://qdrant.example.test",
                vector_size=4,
                embedding_provider=provider,
            )
            self.calls: list[tuple[str, str, dict[str, object] | None]] = []

        def _request(
            self,
            method: str,
            path: str,
            body: dict[str, object] | None = None,
        ) -> dict[str, object]:
            self.calls.append((method, path, body))
            if method == "GET":
                raise HTTPError(path, 404, "missing", {}, None)
            if path.endswith("/points/search"):
                return {"result": []}
            return {"result": {"operation_id": 1, "status": "completed"}}

    provider = SemanticProvider()
    client = QdrantClient(provider)
    qdrant_payload = {
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

    upsert = client.upsert_index_payload({"qdrant_payload": qdrant_payload})
    search = client.search_index_payload(
        {
            **qdrant_payload,
            "embedding_space_fingerprint": upsert.details["embedding_space_fingerprint"],
            "_authorized_point_ids": upsert.details["point_ids"],
        },
        query="报价冲突",
        top_k=3,
    )

    assert upsert.status == "success"
    assert upsert.details["semantic_embedding"] is True
    assert upsert.details["embedding_model"] == "multilingual-semantic-v1"
    assert provider.calls == [
        ("报价金额冲突处理规则", "document"),
        ("报价冲突", "query"),
    ]
    upsert_body = next(body for method, path, body in client.calls if "points?wait" in path)
    assert upsert_body is not None
    assert upsert_body["points"][0]["vector"] == [0.1, 0.2, 0.3, 0.4]  # type: ignore[index]
    search_body = next(body for method, path, body in client.calls if path.endswith("search"))
    assert search_body is not None
    assert search_body["vector"] == [0.4, 0.3, 0.2, 0.1]
    assert search["semantic_embedding"] is True


def test_real_qdrant_fails_closed_when_semantic_document_text_is_missing() -> None:
    class SemanticProvider:
        provider_name = "http"
        model_name = "multilingual-semantic-v1"
        dimension = 4
        is_semantic = True

        def embed(self, text: str, *, purpose: str) -> list[float]:
            raise AssertionError("provider must not be called without semantic input")

    client = RealQdrantIndexClient(
        base_url="http://qdrant.example.test",
        vector_size=4,
        embedding_provider=SemanticProvider(),
    )
    result = client.upsert_index_payload(
        {
            "qdrant_payload": {
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
            }
        }
    )

    assert result.status == "failed"
    assert result.error_code == "EMBEDDING_GENERATION_FAILED"
    assert result.details["embedding_provider"] == "http"
