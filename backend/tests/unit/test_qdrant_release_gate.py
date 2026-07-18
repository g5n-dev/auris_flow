from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.main import settings

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


def test_readyz_sends_qdrant_api_key(client, monkeypatch):
    observed_headers: list[dict[str, str]] = []

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

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
