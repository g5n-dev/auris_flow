from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError

import pytest

from app.api.routers import audio_sessions as audio_sessions_router
from app.core.embeddings import DeterministicTestEmbeddingProvider
from app.main import probe_dagster_workspace, settings
from app.services.adapters import (
    RealDagsterClient,
    RealObjectStorageClient,
    RealQdrantIndexClient,
)


@contextmanager
def _cross_origin_redirect(
    status_code: int,
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    target_requests: list[dict[str, Any]] = []

    class RedirectTarget(BaseHTTPRequestHandler):
        def _capture(self) -> None:
            target_requests.append(
                {
                    "method": self.command,
                    "headers": {name.lower(): value for name, value in self.headers.items()},
                }
            )
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", '"redirect-target-etag"')
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = _capture  # noqa: N815 - stdlib handler API
        do_HEAD = _capture  # noqa: N815 - stdlib handler API
        do_POST = _capture  # noqa: N815 - stdlib handler API

        def log_message(self, _format: str, *_args: object) -> None:
            return

    target = ThreadingHTTPServer(("127.0.0.1", 0), RedirectTarget)
    target_url = f"http://127.0.0.1:{target.server_port}/captured"

    class RedirectSource(BaseHTTPRequestHandler):
        def _redirect(self) -> None:
            self.send_response(status_code)
            self.send_header("Location", target_url)
            self.end_headers()

        do_GET = _redirect  # noqa: N815 - stdlib handler API
        do_HEAD = _redirect  # noqa: N815 - stdlib handler API
        do_POST = _redirect  # noqa: N815 - stdlib handler API

        def log_message(self, _format: str, *_args: object) -> None:
            return

    source = ThreadingHTTPServer(("127.0.0.1", 0), RedirectSource)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True) for server in (source, target)
    ]
    for thread in threads:
        thread.start()
    try:
        yield f"http://127.0.0.1:{source.server_port}", target_requests
    finally:
        for server in (source, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


@pytest.mark.parametrize("status_code", [302, 307])
def test_dagster_bearer_is_not_forwarded_across_redirect(status_code: int) -> None:
    with _cross_origin_redirect(status_code) as (source_url, target_requests):
        client = RealDagsterClient(
            graphql_url=f"{source_url}/graphql",
            bearer_token="unit-test-fixture-dagster-token",
        )

        with pytest.raises(HTTPError):
            client._request({"query": "query { version }"})

        assert target_requests == []


@pytest.mark.parametrize("status_code", [302, 307])
def test_qdrant_api_key_is_not_forwarded_across_redirect(status_code: int) -> None:
    with _cross_origin_redirect(status_code) as (source_url, target_requests):
        client = RealQdrantIndexClient(
            base_url=source_url,
            vector_size=8,
            api_key="unit-test-fixture-qdrant-api-key",
            embedding_provider=DeterministicTestEmbeddingProvider(dimension=8),
        )

        with pytest.raises(HTTPError):
            client._request("GET", "/collections")

        assert target_requests == []


@pytest.mark.parametrize("status_code", [302, 307])
def test_object_storage_signature_is_not_forwarded_across_redirect(status_code: int) -> None:
    with _cross_origin_redirect(status_code) as (source_url, target_requests):
        client = _object_storage_client(source_url)

        with pytest.raises(HTTPError):
            client.head_bucket("auris-audio")

        assert target_requests == []


@pytest.mark.parametrize("status_code", [302, 307])
def test_audio_signed_get_is_not_forwarded_across_redirect(status_code: int) -> None:
    with _cross_origin_redirect(status_code) as (source_url, target_requests):
        client = _object_storage_client(source_url)

        with pytest.raises(HTTPError):
            audio_sessions_router._open_object_with_if_match(
                client,
                "auris-audio",
                "tenants/tenant/projects/project/audio.wav",
                byte_range=None,
                registered_etag="registered-etag",
                registered_version_id="immutable-version-v1",
            )

        assert target_requests == []


@pytest.mark.parametrize("status_code", [302, 307])
def test_dagster_readiness_bearer_is_not_forwarded_across_redirect(
    status_code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DAGSTER_GRAPHQL_BEARER_TOKEN",
        "unit-test-fixture-readiness-dagster-token",
    )
    with _cross_origin_redirect(status_code) as (source_url, target_requests):
        assert probe_dagster_workspace(f"{source_url}/graphql") == "not_ready"
        assert target_requests == []


@pytest.mark.parametrize("status_code", [302, 307])
def test_qdrant_readiness_api_key_is_not_forwarded_across_redirect(
    status_code: int,
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _cross_origin_redirect(status_code) as (source_url, target_requests):
        monkeypatch.setattr(settings, "required_dependency_checks", "qdrant")
        monkeypatch.setattr(settings, "qdrant_url", source_url)
        monkeypatch.setattr(
            settings,
            "qdrant_api_key",
            "unit-test-fixture-readiness-qdrant-api-key",
        )

        response = client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["data"]["checks"]["qdrant"] == "not_ready"
        assert target_requests == []


def _object_storage_client(endpoint: str) -> RealObjectStorageClient:
    return RealObjectStorageClient(
        endpoint=endpoint,
        bucket="auris-audio",
        access_key="unit-test-fixture-storage-access-key",
        secret_key="unit-test-fixture-storage-secret-key",
        region="us-east-1",
        provider="minio",
        addressing_style="path",
        signature_mode="s3v4",
        allowed_buckets="auris-audio",
    )
