from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.main import settings

SECURE_RELEASE_SETTINGS = {
    "auth_provider": "signed",
    "allow_dev_auth": False,
    "auth_token_secret": "unit-auth-token-secret-32-characters",
    "audio_playback_grant_secret": "unit-playback-secret-32-characters",
    "completion_receipt_secret": "unit-completion-secret-32-characters",
    "cors_allowed_origins": "https://auris.example.com",
    "trusted_hosts": "auris.example.com",
}


@pytest.mark.parametrize("app_env", ["prod", "release"])
def test_real_qdrant_requires_api_key_in_production_environments(app_env: str):
    with pytest.raises(ValidationError, match="QDRANT_API_KEY is required"):
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
        qdrant_api_key="release-secret",
    )

    assert configured.qdrant_api_key == "release-secret"


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
