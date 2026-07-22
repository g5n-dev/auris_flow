from __future__ import annotations

import pytest

from app.api.routers import audio_sessions
from app.core.errors import ApiError


def test_real_audio_execution_accepts_only_server_configured_provider_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio_sessions.settings, "auris_dagster_adapter", "real")
    monkeypatch.setattr(
        audio_sessions.settings,
        "auris_audio_inference_provider",
        "audio_intelligence_default",
    )
    monkeypatch.setattr(
        audio_sessions.settings,
        "auris_audio_inference_allowed_models",
        "audio-v2.3.1,audio-v2.4.0",
    )

    audio_sessions._require_server_audio_inference_policy(  # noqa: SLF001
        provider="audio_intelligence_default",
        model="audio-v2.3.1",
    )
    for provider, model in (
        ("caller_chosen_provider", "audio-v2.3.1"),
        ("audio_intelligence_default", "caller-chosen-model"),
        (None, "audio-v2.3.1"),
    ):
        with pytest.raises(ApiError) as raised:
            audio_sessions._require_server_audio_inference_policy(  # noqa: SLF001
                provider=provider,
                model=model,
            )
        assert raised.value.code == "AUDIO_INFERENCE_POLICY_VIOLATION"
        assert raised.value.retryable is False


def test_non_real_audio_execution_does_not_claim_production_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio_sessions.settings, "auris_dagster_adapter", "local")

    audio_sessions._require_server_audio_inference_policy(  # noqa: SLF001
        provider="test-only-provider",
        model="test-only-model",
    )
