from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings

CALLBACK_ACTIVE_KEY_ID = "callback-2026-07"
CALLBACK_KEY_MATERIAL = "callback-production-key-material-2026-07-A!"
CALLBACK_KEY_BINDINGS = json.dumps(
    {
        CALLBACK_ACTIVE_KEY_ID: {
            "secret": CALLBACK_KEY_MATERIAL,
            "state": "active",
        }
    }
)


def secure_production_values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "app_env": "production",
        "database_url": f"mysql+pymysql://auris:{'M' * 48}@mysql:3306/auris_flow",
        "redis_url": f"redis://:{'R' * 48}@redis:6379/0",
        "auth_provider": "oidc",
        "allow_dev_auth": False,
        "oidc_issuer": "https://identity.example.com/realms/auris",
        "oidc_client_id": "auris-flow-bff",
        "oidc_client_secret": "A" * 48,
        "oidc_audience": "auris-flow-api",
        "oidc_redirect_uri": "https://auris.example.com/api/v1/auth/oidc/callback",
        "browser_session_cookie_name": "__Host-auris_session",
        "audio_playback_grant_secret": "B" * 48,
        "completion_receipt_secret": "C" * 48,
        "experiment_assignment_secret": "D" * 48,
        "cors_allowed_origins": "https://auris.example.com",
        "trusted_hosts": "auris.example.com",
        "auris_object_storage_adapter": "real",
        "object_storage_endpoint": "http://minio:9000",
        "object_storage_bucket": "auris-production",
        "object_storage_access_key": "auris-release-access",
        "object_storage_secret_key": "E" * 48,
        "auris_qdrant_adapter": "real",
        "qdrant_api_key": "F" * 48,
        "auris_embedding_provider": "http",
        "embedding_endpoint": "https://embeddings.example.com/v1/embeddings",
        "embedding_model": "multilingual-semantic-v1",
        "embedding_dimension": 1024,
        "embedding_api_key": "G" * 48,
        "auris_dagster_adapter": "real",
        "dagster_graphql_url": "http://dagster:3000/graphql",
        "auris_external_callback_adapter": "real",
        "external_callback_url": "https://callback.example.com/callbacks/platform",
        "external_callback_allowed_hosts": "callback.example.com",
        "external_callback_key_bindings": CALLBACK_KEY_BINDINGS,
        "external_callback_active_key_id": CALLBACK_ACTIVE_KEY_ID,
        "external_callback_legacy_hmac_enabled": False,
        "dependency_check_mode": "strict",
        "web_concurrency": 1,
        "otel_enabled": True,
        "otel_exporter_otlp_endpoint": "https://telemetry.example.com/v1/traces",
        "metrics_enabled": True,
    }
    values.update(overrides)
    return values


def test_secure_production_configuration_is_accepted() -> None:
    configured = Settings(_env_file=None, **secure_production_values())

    assert configured.auris_dagster_adapter == "real"
    assert configured.auris_embedding_provider == "http"
    assert configured.otel_enabled is True
    assert configured.metrics_enabled is True
    assert configured.dependency_check_mode == "strict"
    assert configured.task_run_monitor_enabled is True
    assert configured.web_concurrency == 1


@pytest.mark.parametrize("app_env", ["production", "release"])
@pytest.mark.parametrize(
    ("field_name", "value", "error"),
    [
        ("web_concurrency", 0, "WEB_CONCURRENCY"),
        ("web_concurrency", 2, "WEB_CONCURRENCY"),
    ],
)
def test_production_and_release_require_a_single_bff_process(
    app_env: str,
    field_name: str,
    value: int,
    error: str,
) -> None:
    with pytest.raises(ValidationError, match=error):
        Settings(
            _env_file=None,
            **secure_production_values(
                app_env=app_env,
                **{field_name: value},
            ),
        )


def test_local_environment_may_explicitly_use_multiple_bff_processes() -> None:
    configured = Settings(
        _env_file=None,
        web_concurrency=2,
    )

    assert configured.web_concurrency == 2


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("task_run_default_deadline_seconds", 59),
        ("task_run_default_deadline_seconds", 7 * 24 * 60 * 60 + 1),
        ("task_run_status_sync_interval_seconds", 4),
        ("task_run_status_sync_interval_seconds", 3601),
        ("task_run_monitor_poll_seconds", 0),
        ("task_run_monitor_poll_seconds", 301),
        ("task_run_monitor_batch_size", 0),
        ("task_run_monitor_batch_size", 501),
    ],
)
def test_task_run_monitor_configuration_is_bounded(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError, match=field_name.upper()):
        Settings(_env_file=None, **secure_production_values(**{field_name: value}))


def test_production_requires_task_run_monitor() -> None:
    with pytest.raises(ValidationError, match="TASK_RUN_MONITOR_ENABLED"):
        Settings(
            _env_file=None,
            **secure_production_values(task_run_monitor_enabled=False),
        )


@pytest.mark.parametrize(
    ("field_name", "value", "error"),
    [
        ("auris_embedding_provider", "deterministic_test", "AURIS_EMBEDDING_PROVIDER"),
        ("embedding_endpoint", "", "EMBEDDING_ENDPOINT"),
        ("embedding_endpoint", "http://embeddings.example.com/v1", "EMBEDDING_ENDPOINT"),
        ("embedding_model", "", "EMBEDDING_MODEL"),
        ("embedding_dimension", 0, "EMBEDDING_DIMENSION"),
        ("embedding_api_key", "weak", "EMBEDDING_API_KEY"),
    ],
)
def test_production_embedding_configuration_fails_closed(
    field_name: str,
    value: object,
    error: str,
) -> None:
    with pytest.raises(ValidationError, match=error):
        Settings(_env_file=None, **secure_production_values(**{field_name: value}))


@pytest.mark.parametrize("field_name", ["otel_enabled", "metrics_enabled"])
def test_production_requires_observability_gates(field_name: str) -> None:
    with pytest.raises(ValidationError, match=field_name.upper()):
        Settings(_env_file=None, **secure_production_values(**{field_name: False}))


@pytest.mark.parametrize("sample_ratio", [0.0, 0.009])
def test_production_requires_nonzero_minimum_trace_sampling(sample_ratio: float) -> None:
    with pytest.raises(ValidationError, match="OTEL_TRACE_SAMPLE_RATIO"):
        Settings(
            _env_file=None,
            **secure_production_values(otel_trace_sample_ratio=sample_ratio),
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "auris_object_storage_adapter",
        "auris_qdrant_adapter",
        "auris_dagster_adapter",
        "auris_external_callback_adapter",
    ],
)
def test_production_requires_every_external_adapter_to_be_real(field_name: str) -> None:
    with pytest.raises(ValidationError, match=field_name.upper()):
        Settings(_env_file=None, **secure_production_values(**{field_name: "local"}))


@pytest.mark.parametrize(
    "field_name",
    [
        "auris_object_storage_adapter",
        "auris_qdrant_adapter",
        "auris_dagster_adapter",
        "auris_external_callback_adapter",
    ],
)
def test_adapter_mode_rejects_unknown_values_in_every_environment(field_name: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field_name: "fake"})


def test_production_dependency_checks_must_be_strict() -> None:
    with pytest.raises(ValidationError, match="DEPENDENCY_CHECK_MODE"):
        Settings(
            _env_file=None,
            **secure_production_values(dependency_check_mode="local"),
        )


def test_local_and_test_environments_keep_local_compatibility() -> None:
    local = Settings(_env_file=None)
    test = Settings(_env_file=None, app_env="test")

    assert local.auris_object_storage_adapter == "local"
    assert local.auris_qdrant_adapter == "local"
    assert local.auris_dagster_adapter == "local"
    assert local.auris_external_callback_adapter == "local"
    assert local.dependency_check_mode == "local"
    assert test.auris_dagster_adapter == "local"


@pytest.mark.parametrize(
    ("field_name", "weak_value"),
    [
        ("oidc_client_secret", "replace-with-production-secret-value"),
        ("audio_playback_grant_secret", "auris-demo"),
        ("completion_receipt_secret", "changeme-changeme-changeme-changeme"),
        ("experiment_assignment_secret", "example-example-example-example-example"),
        ("object_storage_secret_key", "minioadmin"),
        ("qdrant_api_key", "placeholder-placeholder-placeholder-value"),
    ],
)
def test_production_rejects_demo_placeholder_and_weak_secrets(
    field_name: str,
    weak_value: str,
) -> None:
    with pytest.raises(ValidationError, match=field_name.upper()):
        Settings(_env_file=None, **secure_production_values(**{field_name: weak_value}))


def test_production_rejects_short_secrets_even_without_placeholder_words() -> None:
    with pytest.raises(ValidationError, match="QDRANT_API_KEY"):
        Settings(_env_file=None, **secure_production_values(qdrant_api_key="q" * 15))


def test_production_callback_requires_explicit_keyring_not_legacy_single_secret() -> None:
    with pytest.raises(ValidationError, match="EXTERNAL_CALLBACK_LEGACY_HMAC_ENABLED"):
        Settings(
            _env_file=None,
            **secure_production_values(
                external_callback_key_bindings="",
                external_callback_active_key_id="",
                external_callback_legacy_hmac_enabled=True,
                external_callback_secret="L" * 48,
            ),
        )


@pytest.mark.parametrize(
    ("bindings", "active_key_id"),
    [
        (CALLBACK_KEY_BINDINGS, "unknown-key"),
        (
            json.dumps(
                {
                    CALLBACK_ACTIVE_KEY_ID: {
                        "secret": "short",
                        "state": "active",
                    }
                }
            ),
            CALLBACK_ACTIVE_KEY_ID,
        ),
        (
            json.dumps(
                {
                    CALLBACK_ACTIVE_KEY_ID: {
                        "secret": "replace-with-production-callback-key-material",
                        "state": "active",
                    }
                }
            ),
            CALLBACK_ACTIVE_KEY_ID,
        ),
        (
            json.dumps(
                {
                    CALLBACK_ACTIVE_KEY_ID: {
                        "secret": "callback-retired-key-material-2026-06-X!",
                        "state": "retired",
                    }
                }
            ),
            CALLBACK_ACTIVE_KEY_ID,
        ),
    ],
)
def test_callback_keyring_rejects_unknown_weak_or_retired_active_key(
    bindings: str,
    active_key_id: str,
) -> None:
    with pytest.raises(ValidationError, match="EXTERNAL_CALLBACK_KEY_BINDINGS"):
        Settings(
            _env_file=None,
            **secure_production_values(
                external_callback_key_bindings=bindings,
                external_callback_active_key_id=active_key_id,
            ),
        )


def test_production_rejects_default_object_storage_access_key() -> None:
    with pytest.raises(ValidationError, match="OBJECT_STORAGE_ACCESS_KEY"):
        Settings(
            _env_file=None,
            **secure_production_values(object_storage_access_key="minioadmin"),
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("database_url", "mysql+pymysql://auris:auris@mysql:3306/auris_flow"),
        ("redis_url", "redis://redis:6379/0"),
        ("redis_url", "redis://:changeme@redis:6379/0"),
    ],
)
def test_production_rejects_missing_demo_or_weak_service_passwords(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match=field_name.upper()) as raised:
        Settings(_env_file=None, **secure_production_values(**{field_name: value}))

    assert value not in str(raised.value)


def test_production_validation_errors_hide_all_input_values() -> None:
    canary = "must-never-appear-in-validation-output"
    values = secure_production_values(
        auris_object_storage_adapter="local",
        oidc_client_secret=canary,
    )

    with pytest.raises(ValidationError) as raised:
        Settings(_env_file=None, **values)

    assert canary not in str(raised.value)


def test_production_rejects_transitional_signed_auth_provider() -> None:
    with pytest.raises(ValidationError, match="AUTH_PROVIDER"):
        Settings(
            _env_file=None,
            **secure_production_values(auth_provider="signed", auth_token_secret="A" * 48),
        )


@pytest.mark.parametrize(
    ("field_name", "value", "error"),
    [
        ("oidc_issuer", "http://identity.example.com/realms/auris", "OIDC_ISSUER"),
        ("oidc_redirect_uri", "http://auris.example.com/callback", "OIDC_REDIRECT_URI"),
        ("oidc_scopes", "profile email", "OIDC_SCOPES"),
        ("oidc_scopes", "openid profile offline_access", "OIDC_SCOPES"),
        ("browser_session_cookie_name", "auris_session", "BROWSER_SESSION_COOKIE_NAME"),
    ],
)
def test_production_oidc_boundary_fails_closed(
    field_name: str,
    value: str,
    error: str,
) -> None:
    with pytest.raises(ValidationError, match=error):
        Settings(_env_file=None, **secure_production_values(**{field_name: value}))


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("cors_allowed_origins", "null"),
        ("cors_allowed_origins", "http://auris.example.com"),
        ("cors_allowed_origins", "https://*.example.com"),
        ("cors_allowed_origins", "https://auris.example.com/path"),
        ("cors_allowed_origins", "https://user@auris.example.com"),
        ("trusted_hosts", "*.example.com"),
        ("trusted_hosts", "auris.example.com,*"),
    ],
)
def test_production_browser_origins_and_hosts_must_be_exact(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match=field_name.upper()):
        Settings(_env_file=None, **secure_production_values(**{field_name: value}))


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("cors_allowed_origins", "https://AURIS.example.com"),
        ("cors_allowed_origins", "https://auris.example.com:443"),
        ("cors_allowed_origins", "https://auris.example.com:"),
        ("cors_allowed_origins", "https://auris.example.com."),
        ("trusted_hosts", "AURIS.example.com"),
        ("trusted_hosts", "auris.example.com:443"),
        ("trusted_hosts", "auris.example.com."),
        ("trusted_hosts", "auris.example.com\\suffix"),
    ],
)
def test_production_browser_boundary_rejects_noncanonical_runtime_mismatches(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match=field_name.upper()):
        Settings(_env_file=None, **secure_production_values(**{field_name: value}))


def test_production_cors_allows_a_canonical_non_default_https_port() -> None:
    configured = Settings(
        _env_file=None,
        **secure_production_values(cors_allowed_origins="https://auris.example.com:8443"),
    )

    assert configured.cors_allowed_origins == "https://auris.example.com:8443"
