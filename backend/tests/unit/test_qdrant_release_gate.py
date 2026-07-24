from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.main import OBJECT_STORAGE_READINESS_TIMEOUT_SECONDS, settings

SECURE_RELEASE_SETTINGS = {
    "database_url": f"mysql+pymysql://auris:{'M' * 48}@mysql:3306/auris_flow",
    "redis_url": f"redis://:{'R' * 48}@redis:6379/0",
    "auth_provider": "oidc",
    "allow_dev_auth": False,
    "oidc_issuer": "https://identity.example.com/realms/auris",
    "oidc_client_id": "auris-flow-bff",
    "oidc_client_secret": "I" * 48,
    "oidc_audience": "auris-flow-api",
    "oidc_redirect_uri": "https://auris.example.com/api/v1/auth/oidc/callback",
    "browser_session_cookie_name": "__Host-auris_session",
    "auris_embedding_provider": "http",
    "embedding_endpoint": "https://embeddings.example.com/v1/embeddings",
    "embedding_model": "multilingual-semantic-v1",
    "embedding_dimension": 1024,
    "embedding_api_key": "G" * 48,
    "audio_playback_grant_secret": "unit-playback-secret-32-characters",
    "completion_receipt_secret": "unit-completion-secret-32-characters",
    "experiment_assignment_secret": "unit-experiment-assignment-secret-32-characters",
    "cors_allowed_origins": "https://auris.example.com",
    "trusted_hosts": "auris.example.com",
    "auris_object_storage_adapter": "real",
    "object_storage_endpoint": "http://minio:9000",
    "object_storage_bucket": "auris-unit",
    "object_storage_access_key": "auris-unit-access",
    "object_storage_secret_key": "O" * 48,
    "auris_dagster_adapter": "real",
    "dagster_graphql_url": "http://dagster:3000/graphql",
    "auris_external_callback_adapter": "real",
    "external_callback_url": "https://callback.example.com/callbacks/platform",
    "external_callback_allowed_hosts": "callback.example.com",
    "external_callback_key_bindings": json.dumps(
        {
            "callback-2026-07": {
                "secret": "callback-production-key-material-2026-07-C!",
                "state": "active",
            }
        }
    ),
    "external_callback_active_key_id": "callback-2026-07",
    "dependency_check_mode": "strict",
    "otel_enabled": True,
    "otel_exporter_otlp_endpoint": "https://telemetry.example.com/v1/traces",
    "metrics_enabled": True,
}


@pytest.mark.parametrize("app_env", ["prod", "release"])
def test_real_qdrant_requires_api_key_in_production_environments(app_env: str):
    with pytest.raises(ValidationError, match="QDRANT_API_KEY"):
        Settings(
            app_env=app_env,
            **SECURE_RELEASE_SETTINGS,
            auris_qdrant_adapter="real",
            qdrant_api_key="   ",
        )


def test_real_qdrant_accepts_api_key_in_release():
    configured = Settings(
        app_env="release",
        **SECURE_RELEASE_SETTINGS,
        auris_qdrant_adapter="real",
        qdrant_api_key="Q" * 48,
    )

    assert configured.qdrant_api_key == "Q" * 48


def test_runtime_qdrant_factory_forwards_file_resolved_key(monkeypatch):
    from app.core import config
    from app.services import adapters

    captured: dict[str, object] = {}

    class StubClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(
            qdrant_url="http://qdrant:6333",
            qdrant_api_key="file-resolved-qdrant-secret",
            embedding_dimension=1024,
        ),
    )
    monkeypatch.setattr(adapters, "RealQdrantIndexClient", StubClient)

    client = adapters.configured_real_qdrant_client()

    assert isinstance(client, StubClient)
    assert captured == {
        "api_key": "file-resolved-qdrant-secret",
        "base_url": "http://qdrant:6333",
        "vector_size": 1024,
    }


def test_default_real_qdrant_client_uses_configured_factory(monkeypatch):
    from app.services import adapters

    sentinel = object()
    monkeypatch.setenv("AURIS_QDRANT_ADAPTER", "real")
    monkeypatch.setattr(adapters, "configured_real_qdrant_client", lambda: sentinel)

    assert adapters._default_qdrant_client() is sentinel


def test_recall_real_qdrant_uses_configured_client(monkeypatch):
    from app.services import knowledge_recall_service

    calls: list[tuple[dict[str, object], str, int]] = []

    class StubClient:
        def search_index_payload(
            self,
            payload: dict[str, object],
            *,
            query: str,
            top_k: int,
        ) -> dict[str, object]:
            calls.append((payload, query, top_k))
            return {"mode": "real", "collection": "knowledge", "points": []}

    monkeypatch.setattr(
        knowledge_recall_service,
        "configured_real_qdrant_client",
        lambda: StubClient(),
    )

    result = knowledge_recall_service.recall_from_real_qdrant(
        {"collection": "knowledge"},
        query="hello",
        top_k=3,
    )

    assert result["mode"] == "real"
    assert calls == [({"collection": "knowledge"}, "hello", 3)]


def test_readyz_sends_qdrant_api_key(client, monkeypatch):
    observed_headers: list[dict[str, str]] = []

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, _limit: int = -1) -> bytes:
            return b'{"result":{"collections":[]},"status":"ok"}'

    def fake_urlopen(request, timeout):
        del timeout
        if request.full_url.endswith("/collections"):
            observed_headers.append({name.lower(): value for name, value in request.header_items()})
        return Response()

    monkeypatch.setattr("app.main.urlopen", fake_urlopen)
    monkeypatch.setattr(settings, "required_dependency_checks", "qdrant")
    monkeypatch.setattr(settings, "qdrant_api_key", "readyz-secret")

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["data"]["checks"]["qdrant"] == "ok"
    assert observed_headers == [{"api-key": "readyz-secret"}]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
def test_readyz_rejects_qdrant_4xx_responses(client, monkeypatch, status: int):
    class Response:
        def __init__(self) -> None:
            self.status = status

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr("app.main.urlopen", lambda _request, timeout: Response())
    monkeypatch.setattr(settings, "required_dependency_checks", "qdrant")
    monkeypatch.setattr(settings, "qdrant_url", "http://qdrant:6333")

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["data"]["checks"]["qdrant"] == "not_ready"


def test_readyz_minio_requires_authenticated_bucket_access(client, monkeypatch):
    observed: list[tuple[str, float]] = []

    class StorageClient:
        bucket = "auris-production"

        def head_bucket(
            self,
            bucket: str,
            *,
            timeout_seconds: float,
        ) -> dict[str, object]:
            observed.append((bucket, timeout_seconds))
            return {"status": 200}

    monkeypatch.setattr(
        "app.main.object_storage_client_for_provider",
        lambda provider: StorageClient() if provider == "minio" else None,
    )
    monkeypatch.setattr(settings, "required_dependency_checks", "object_storage")
    monkeypatch.setattr(settings, "object_storage_provider", "minio")
    monkeypatch.setattr(settings, "object_storage_endpoint", "http://minio:9000")
    monkeypatch.setattr(settings, "qdrant_url", "")
    monkeypatch.setattr(settings, "dagster_graphql_url", "")

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["data"]["checks"]["object_storage"] == "ok"
    assert observed == [("auris-production", OBJECT_STORAGE_READINESS_TIMEOUT_SECONDS)]


@pytest.mark.parametrize("status", [403, 404])
def test_readyz_rejects_inaccessible_or_missing_object_storage_bucket(
    client,
    monkeypatch,
    status: int,
):
    class StorageClient:
        bucket = "auris-production"

        def head_bucket(
            self,
            bucket: str,
            *,
            timeout_seconds: float,
        ) -> dict[str, object]:
            assert bucket == self.bucket
            assert timeout_seconds == OBJECT_STORAGE_READINESS_TIMEOUT_SECONDS
            raise HTTPError(
                url=f"https://{bucket}.s3.example.test/",
                code=status,
                msg="bucket unavailable",
                hdrs=None,
                fp=None,
            )

    monkeypatch.setattr(
        "app.main.object_storage_client_for_provider",
        lambda _provider: StorageClient(),
    )
    monkeypatch.setattr(settings, "required_dependency_checks", "object_storage")
    monkeypatch.setattr(settings, "object_storage_provider", "s3")
    monkeypatch.setattr(settings, "qdrant_url", "")
    monkeypatch.setattr(settings, "dagster_graphql_url", "")

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["data"]["checks"]["object_storage"] == "not_ready"


def test_readyz_bounds_object_storage_bucket_probe_timeout(client, monkeypatch):
    observed_timeouts: list[float] = []

    class StorageClient:
        bucket = "auris-production"

        def head_bucket(
            self,
            _bucket: str,
            *,
            timeout_seconds: float,
        ) -> dict[str, object]:
            observed_timeouts.append(timeout_seconds)
            raise TimeoutError("object storage readiness deadline")

    monkeypatch.setattr(
        "app.main.object_storage_client_for_provider",
        lambda _provider: StorageClient(),
    )
    monkeypatch.setattr(settings, "required_dependency_checks", "object_storage")
    monkeypatch.setattr(settings, "qdrant_url", "")
    monkeypatch.setattr(settings, "dagster_graphql_url", "")

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["data"]["checks"]["object_storage"] == "not_ready"
    assert observed_timeouts == [OBJECT_STORAGE_READINESS_TIMEOUT_SECONDS]
    assert 0 < OBJECT_STORAGE_READINESS_TIMEOUT_SECONDS <= 0.5
