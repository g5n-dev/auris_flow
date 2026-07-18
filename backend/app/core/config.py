from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from pydantic import model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from app.core.callback_signature import CallbackSignatureError, parse_callback_keyring
from app.core.secrets import (
    SecretFileSettingsSource,
    is_production_environment,
)

AdapterMode = Literal["local", "real"]
EmbeddingProviderMode = Literal["deterministic_test", "http"]
_INSECURE_SECRET_MARKERS = (
    "auris-demo",
    "auris-dev",
    "auris-local",
    "changeme",
    "change-me",
    "example",
    "minioadmin",
    "placeholder",
    "replace-with",
)


class Settings(BaseSettings):
    app_env: str = "local"
    app_name: str = "auris-flow-bff"
    log_level: str = "INFO"
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = ""
    otel_exporter_otlp_headers: str = ""
    otel_service_name: str = "auris-flow-bff"
    otel_trace_sample_ratio: float = 0.1
    otel_export_timeout_seconds: float = 5.0
    metrics_enabled: bool = False
    metrics_trusted_cidrs: str = ""
    api_prefix: str = "/api/v1"
    cors_allowed_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    trusted_hosts: str = "127.0.0.1,localhost,testserver"
    security_headers_enabled: bool = True
    database_url: str = "mysql+pymysql://auris:auris@127.0.0.1:3306/auris_flow"
    redis_url: str = "redis://127.0.0.1:6379/0"
    object_storage_endpoint: str = "http://127.0.0.1:9000"
    object_storage_bucket: str = "auris-flow-local"
    object_storage_access_key: str = "minioadmin"
    object_storage_secret_key: str = "minioadmin"
    object_storage_region: str = "us-east-1"
    object_storage_provider: str = "minio"
    object_storage_addressing_style: str = ""
    object_storage_signature_mode: str = ""
    object_storage_session_token: str = ""
    object_storage_allowed_buckets: str = ""
    auris_object_storage_adapter: AdapterMode = "local"
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str = ""
    auris_qdrant_adapter: AdapterMode = "local"
    auris_embedding_provider: EmbeddingProviderMode = "deterministic_test"
    embedding_endpoint: str = ""
    embedding_model: str = ""
    embedding_dimension: int = 8
    embedding_api_key: str = ""
    embedding_http_timeout_seconds: float = 10.0
    dagster_graphql_url: str = "http://127.0.0.1:3000/graphql"
    auris_dagster_adapter: AdapterMode = "local"
    auris_external_callback_adapter: AdapterMode = "local"
    external_callback_url: str = "http://127.0.0.1:8089/callbacks/platform"
    external_callback_secret: str = "auris-dev-callback-secret"
    external_callback_key_bindings: str = ""
    external_callback_active_key_id: str = ""
    external_callback_legacy_hmac_enabled: bool = False
    external_callback_signature_tolerance_seconds: int = 300
    external_callback_allowed_hosts: str = ""
    dependency_check_mode: str = "local"
    required_dependency_checks: str = "auto"
    auth_provider: str = "auto"
    auth_token_secret: str = ""
    auth_token_issuer: str = "auris-flow"
    auth_token_audience: str = "auris-flow-api"
    auth_token_clock_skew_seconds: int = 30
    auth_session_last_seen_interval_seconds: int = 60
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_audience: str = ""
    oidc_redirect_uri: str = ""
    oidc_scopes: str = "openid profile email"
    oidc_discovery_url: str = ""
    oidc_jwks_cache_ttl_seconds: int = 300
    oidc_clock_skew_seconds: int = 30
    oidc_http_timeout_seconds: float = 5.0
    oidc_authorization_state_ttl_seconds: int = 600
    oidc_session_ttl_seconds: int = 28800
    browser_session_cookie_name: str = "auris_session"
    dev_auth_password: str = "auris-demo"
    dev_auth_session_ttl_seconds: int = 28800
    audio_playback_grant_secret: str = ""
    audio_playback_grant_ttl_seconds: int = 300
    dev_auth_token: str = "dev-token"
    allow_dev_auth: bool = False
    completion_receipt_secret: str = ""
    completion_receipt_signature_id: str = "auris-local-completion"
    completion_receipt_key_bindings: str = ""
    completion_receipt_signature_tolerance_seconds: int = 300
    completion_receipt_allowed_sources: str = "dagster,object_storage,external_callback"
    experiment_assignment_secret: str = "auris-local-experiment-assignment-secret"
    rate_limit_per_minute: int = 240
    outbox_lease_seconds: int = 60
    outbox_claim_batch_size: int = 1
    outbox_claim_retries: int = 3
    outbox_claim_retry_base_ms: int = 25
    outbox_max_attempts: int = 5
    outbox_retry_base_seconds: int = 30
    outbox_retry_max_seconds: int = 300
    outbox_retry_jitter_seconds: int = 5
    label_optimization_scheduler_enabled: bool = False
    label_optimization_scheduler_poll_seconds: int = 60
    label_optimization_scheduler_batch_size: int = 20
    label_optimization_scheduler_claim_lease_seconds: int = 120
    label_optimization_scheduler_worker_id: str = "label-opt-scheduler-local"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        dotenv_values: dict[str, Any] = {}
        if isinstance(dotenv_settings, DotEnvSettingsSource):
            dotenv_values = dict(dotenv_settings.env_vars)
        secret_file_settings = SecretFileSettingsSource(
            settings_cls,
            dotenv_values=dotenv_values,
        )
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            secret_file_settings,
            file_secret_settings,
        )

    @model_validator(mode="after")
    def require_release_security_configuration(self) -> Settings:
        if not self.auth_token_issuer.strip():
            raise ValueError("AUTH_TOKEN_ISSUER must not be empty")
        if not self.auth_token_audience.strip():
            raise ValueError("AUTH_TOKEN_AUDIENCE must not be empty")
        if not 0 <= self.auth_token_clock_skew_seconds <= 300:
            raise ValueError("AUTH_TOKEN_CLOCK_SKEW_SECONDS must be between 0 and 300")
        if not 60 <= self.auth_session_last_seen_interval_seconds <= 3600:
            raise ValueError("AUTH_SESSION_LAST_SEEN_INTERVAL_SECONDS must be between 60 and 3600")
        if not 1 <= self.external_callback_signature_tolerance_seconds <= 900:
            raise ValueError(
                "EXTERNAL_CALLBACK_SIGNATURE_TOLERANCE_SECONDS must be between 1 and 900"
            )
        if self.external_callback_key_bindings.strip():
            try:
                parse_callback_keyring(
                    self.external_callback_key_bindings,
                    active_key_id=self.external_callback_active_key_id.strip(),
                )
            except CallbackSignatureError:
                raise ValueError(
                    "EXTERNAL_CALLBACK_KEY_BINDINGS or EXTERNAL_CALLBACK_ACTIVE_KEY_ID is invalid"
                ) from None
        elif self.external_callback_active_key_id.strip():
            raise ValueError(
                "EXTERNAL_CALLBACK_KEY_BINDINGS is required when "
                "EXTERNAL_CALLBACK_ACTIVE_KEY_ID is configured"
            )
        if not 1 <= self.oidc_jwks_cache_ttl_seconds <= 86400:
            raise ValueError("OIDC_JWKS_CACHE_TTL_SECONDS must be between 1 and 86400")
        if not 0 <= self.oidc_clock_skew_seconds <= 300:
            raise ValueError("OIDC_CLOCK_SKEW_SECONDS must be between 0 and 300")
        if not 0 < self.oidc_http_timeout_seconds <= 30:
            raise ValueError("OIDC_HTTP_TIMEOUT_SECONDS must be greater than 0 and at most 30")
        if not 60 <= self.oidc_authorization_state_ttl_seconds <= 900:
            raise ValueError("OIDC_AUTHORIZATION_STATE_TTL_SECONDS must be between 60 and 900")
        if not 300 <= self.oidc_session_ttl_seconds <= 2592000:
            raise ValueError("OIDC_SESSION_TTL_SECONDS must be between 300 and 2592000")
        scopes = set(_space_items(self.oidc_scopes))
        if self.auth_provider.strip().lower() == "oidc" and "openid" not in scopes:
            raise ValueError("OIDC_SCOPES must include openid")
        if "offline_access" in scopes:
            raise ValueError("OIDC_SCOPES must not request offline_access")
        if not 5 <= self.label_optimization_scheduler_poll_seconds <= 3600:
            raise ValueError("LABEL_OPTIMIZATION_SCHEDULER_POLL_SECONDS must be between 5 and 3600")
        if not 1 <= self.label_optimization_scheduler_batch_size <= 100:
            raise ValueError("LABEL_OPTIMIZATION_SCHEDULER_BATCH_SIZE must be between 1 and 100")
        if not 30 <= self.label_optimization_scheduler_claim_lease_seconds <= 3600:
            raise ValueError(
                "LABEL_OPTIMIZATION_SCHEDULER_CLAIM_LEASE_SECONDS must be between 30 and 3600"
            )
        if not 0.0 <= self.otel_trace_sample_ratio <= 1.0:
            raise ValueError("OTEL_TRACE_SAMPLE_RATIO must be between 0 and 1")
        if not 0.1 <= self.otel_export_timeout_seconds <= 30.0:
            raise ValueError("OTEL_EXPORT_TIMEOUT_SECONDS must be between 0.1 and 30")
        if self.otel_enabled:
            endpoint = urlparse(self.otel_exporter_otlp_endpoint.strip())
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError(
                    "OTEL_EXPORTER_OTLP_ENDPOINT must be an HTTP(S) URL without credentials, "
                    "query, or fragment"
                )
            if not self.otel_service_name.strip():
                raise ValueError("OTEL_SERVICE_NAME must not be empty when OTEL is enabled")
        for item in _csv_items(self.metrics_trusted_cidrs):
            try:
                network = ipaddress.ip_network(item, strict=False)
            except ValueError:
                raise ValueError("METRICS_TRUSTED_CIDRS entries must be valid CIDRs") from None
            if network.prefixlen == 0:
                raise ValueError("METRICS_TRUSTED_CIDRS must not trust the entire internet")

        production = is_production_environment(self.app_env)
        _validate_embedding_settings(self, production=production)

        if not production:
            return self

        if not self.otel_enabled:
            raise ValueError("OTEL_ENABLED must be true in prod/release")
        if not self.metrics_enabled:
            raise ValueError("METRICS_ENABLED must be true in prod/release")

        auth_provider = self.auth_provider.strip().lower()
        if self.allow_dev_auth or auth_provider == "dev":
            raise ValueError("ALLOW_DEV_AUTH and AUTH_PROVIDER=dev are forbidden in prod/release")
        if auth_provider != "oidc":
            raise ValueError("AUTH_PROVIDER must be oidc in prod/release")
        _validate_production_oidc_settings(self)
        for setting_name in (
            "auris_object_storage_adapter",
            "auris_qdrant_adapter",
            "auris_dagster_adapter",
            "auris_external_callback_adapter",
        ):
            if getattr(self, setting_name) != "real":
                raise ValueError(f"{setting_name.upper()} must be real in prod/release")
        if self.dependency_check_mode.strip().lower() != "strict":
            raise ValueError("DEPENDENCY_CHECK_MODE must be strict in prod/release")

        _require_strong_url_password("DATABASE_URL", self.database_url)
        _require_strong_url_password("REDIS_URL", self.redis_url)
        _require_strong_secret(
            "AUDIO_PLAYBACK_GRANT_SECRET",
            self.audio_playback_grant_secret,
        )
        if (
            not self.completion_receipt_secret.strip()
            and not self.completion_receipt_key_bindings.strip()
        ):
            raise ValueError(
                "COMPLETION_RECEIPT_SECRET or COMPLETION_RECEIPT_KEY_BINDINGS "
                "is required in prod/release"
            )
        if self.completion_receipt_secret.strip():
            _require_strong_secret(
                "COMPLETION_RECEIPT_SECRET",
                self.completion_receipt_secret,
            )
        _require_strong_secret(
            "EXPERIMENT_ASSIGNMENT_SECRET",
            self.experiment_assignment_secret,
        )
        if not _csv_items(self.cors_allowed_origins) or "*" in _csv_items(
            self.cors_allowed_origins
        ):
            raise ValueError("CORS_ALLOWED_ORIGINS must be explicit in prod/release")
        if not _csv_items(self.trusted_hosts) or "*" in _csv_items(self.trusted_hosts):
            raise ValueError("TRUSTED_HOSTS must be explicit in prod/release")
        _require_strong_secret("QDRANT_API_KEY", self.qdrant_api_key)
        _validate_production_callback_settings(self)
        missing = [
            key
            for key, value in {
                "OBJECT_STORAGE_ENDPOINT": self.object_storage_endpoint,
                "OBJECT_STORAGE_BUCKET": self.object_storage_bucket,
                "OBJECT_STORAGE_ACCESS_KEY": self.object_storage_access_key,
                "OBJECT_STORAGE_SECRET_KEY": self.object_storage_secret_key,
                "OBJECT_STORAGE_REGION": self.object_storage_region,
                "OBJECT_STORAGE_PROVIDER": self.object_storage_provider,
            }.items()
            if not value.strip()
        ]
        if missing:
            raise ValueError(
                "object storage real adapter missing prod/release config: " + ", ".join(missing)
            )
        if _is_insecure_value(self.object_storage_access_key):
            raise ValueError(
                "OBJECT_STORAGE_ACCESS_KEY must not use demo credentials in prod/release"
            )
        _require_strong_secret(
            "OBJECT_STORAGE_SECRET_KEY",
            self.object_storage_secret_key,
        )
        if self.object_storage_session_token.strip():
            _require_strong_secret(
                "OBJECT_STORAGE_SESSION_TOKEN",
                self.object_storage_session_token,
            )
        return self


def _csv_items(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.replace("\n", ",").split(",") if item.strip())


def _space_items(value: str) -> tuple[str, ...]:
    return tuple(item for item in value.split() if item)


def _is_insecure_value(value: str) -> bool:
    normalized = value.strip().casefold()
    return not normalized or any(marker in normalized for marker in _INSECURE_SECRET_MARKERS)


def _require_strong_secret(setting_name: str, value: str, *, minimum_length: int = 32) -> None:
    if len(value.strip()) < minimum_length or _is_insecure_value(value):
        raise ValueError(
            f"{setting_name} must be at least {minimum_length} characters and must not use "
            "demo or placeholder values in prod/release"
        )


def _require_strong_url_password(setting_name: str, value: str) -> None:
    try:
        parsed = urlparse(value.strip())
        password = unquote(parsed.password or "")
    except (TypeError, ValueError):
        password = ""
    try:
        _require_strong_secret(f"{setting_name}_PASSWORD", password)
    except ValueError:
        raise ValueError(
            f"{setting_name} must contain a strong non-demo password in prod/release"
        ) from None


def _normalize_callback_host(value: str) -> str:
    host = value.strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or any(character in host for character in "/@?#"):
        raise ValueError("EXTERNAL_CALLBACK_ALLOWED_HOSTS entries must be hostnames")
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        try:
            return host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError(
                "EXTERNAL_CALLBACK_ALLOWED_HOSTS entries must be valid hostnames"
            ) from exc


def _is_non_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not address.is_global


def _validate_production_callback_settings(settings: Settings) -> None:
    callback_url = settings.external_callback_url.strip()
    try:
        parsed = urlparse(callback_url)
        host = _normalize_callback_host(parsed.hostname or "")
        _port = parsed.port
    except ValueError as exc:
        raise ValueError("EXTERNAL_CALLBACK_URL must be a valid absolute URL") from exc
    if parsed.scheme.lower() != "https":
        raise ValueError("EXTERNAL_CALLBACK_URL must use HTTPS in prod/release")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("EXTERNAL_CALLBACK_URL must not contain credentials or a fragment")
    allowed_hosts = tuple(
        _normalize_callback_host(item)
        for item in _csv_items(settings.external_callback_allowed_hosts)
    )
    if not allowed_hosts or "*" in allowed_hosts:
        raise ValueError(
            "EXTERNAL_CALLBACK_ALLOWED_HOSTS must contain explicit hosts in prod/release"
        )
    if host not in allowed_hosts:
        raise ValueError(
            "external callback host must be present in EXTERNAL_CALLBACK_ALLOWED_HOSTS"
        )
    if _is_non_public_address(host):
        raise ValueError("EXTERNAL_CALLBACK_URL must not target a non-public IP in prod/release")
    if settings.external_callback_legacy_hmac_enabled:
        raise ValueError("EXTERNAL_CALLBACK_LEGACY_HMAC_ENABLED is forbidden in prod/release")
    if not settings.external_callback_key_bindings.strip():
        raise ValueError("EXTERNAL_CALLBACK_KEY_BINDINGS is required in prod/release")
    if not settings.external_callback_active_key_id.strip():
        raise ValueError("EXTERNAL_CALLBACK_ACTIVE_KEY_ID is required in prod/release")


def _validate_production_oidc_settings(settings: Settings) -> None:
    missing = [
        name
        for name, value in {
            "OIDC_ISSUER": settings.oidc_issuer,
            "OIDC_CLIENT_ID": settings.oidc_client_id,
            "OIDC_AUDIENCE": settings.oidc_audience,
            "OIDC_REDIRECT_URI": settings.oidc_redirect_uri,
        }.items()
        if not value.strip()
    ]
    if missing:
        raise ValueError("OIDC production configuration missing: " + ", ".join(missing))
    for name, value in (
        ("OIDC_ISSUER", settings.oidc_issuer),
        ("OIDC_REDIRECT_URI", settings.oidc_redirect_uri),
        ("OIDC_DISCOVERY_URL", settings.oidc_discovery_url),
    ):
        if not value:
            continue
        try:
            parsed = urlparse(value)
            _port = parsed.port
        except ValueError:
            raise ValueError(f"{name} must be a valid absolute HTTPS URL") from None
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError(f"{name} must be a valid absolute HTTPS URL")
        if name != "OIDC_REDIRECT_URI" and (parsed.query or parsed.params):
            raise ValueError(f"{name} must not contain query parameters")
    if settings.oidc_client_secret.strip():
        _require_strong_secret("OIDC_CLIENT_SECRET", settings.oidc_client_secret)
    if settings.browser_session_cookie_name != "__Host-auris_session":
        raise ValueError("BROWSER_SESSION_COOKIE_NAME must be __Host-auris_session in prod/release")


def _validate_embedding_settings(settings: Settings, *, production: bool) -> None:
    if not 1 <= settings.embedding_dimension <= 65536:
        raise ValueError("EMBEDDING_DIMENSION must be between 1 and 65536")
    if not 0 < settings.embedding_http_timeout_seconds <= 60:
        raise ValueError("EMBEDDING_HTTP_TIMEOUT_SECONDS must be greater than 0 and at most 60")
    if settings.auris_embedding_provider == "deterministic_test":
        if production:
            raise ValueError(
                "AURIS_EMBEDDING_PROVIDER must be http in prod/release; "
                "deterministic_test is test-only"
            )
        return
    if not settings.embedding_endpoint.strip():
        raise ValueError("EMBEDDING_ENDPOINT is required when the HTTP provider is enabled")
    try:
        endpoint = urlparse(settings.embedding_endpoint.strip())
        _port = endpoint.port
    except ValueError:
        raise ValueError("EMBEDDING_ENDPOINT must be a valid absolute HTTP(S) URL") from None
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("EMBEDDING_ENDPOINT must be a valid absolute HTTP(S) URL")
    if production and endpoint.scheme != "https":
        raise ValueError("EMBEDDING_ENDPOINT must use HTTPS in prod/release")
    if not settings.embedding_model.strip() or len(settings.embedding_model.strip()) > 256:
        raise ValueError("EMBEDDING_MODEL is required and must not exceed 256 characters")
    if production:
        _require_strong_secret("EMBEDDING_API_KEY", settings.embedding_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
