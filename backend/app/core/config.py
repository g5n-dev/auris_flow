from __future__ import annotations

import ipaddress
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production", "release"})


def is_production_environment(value: str) -> bool:
    return value.strip().lower() in PRODUCTION_ENVIRONMENTS


class Settings(BaseSettings):
    app_env: str = "local"
    app_name: str = "auris-flow-bff"
    log_level: str = "INFO"
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
    auris_object_storage_adapter: str = "local"
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str = ""
    auris_qdrant_adapter: str = "local"
    dagster_graphql_url: str = "http://127.0.0.1:3000/graphql"
    auris_external_callback_adapter: str = "local"
    external_callback_url: str = "http://127.0.0.1:8089/callbacks/platform"
    external_callback_secret: str = "auris-dev-callback-secret"
    external_callback_allowed_hosts: str = ""
    dependency_check_mode: str = "local"
    required_dependency_checks: str = "auto"
    auth_provider: str = "auto"
    auth_token_secret: str = ""
    auth_token_issuer: str = "auris-flow"
    auth_token_audience: str = "auris-flow-api"
    auth_token_clock_skew_seconds: int = 30
    auth_session_last_seen_interval_seconds: int = 60
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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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
        if not 5 <= self.label_optimization_scheduler_poll_seconds <= 3600:
            raise ValueError("LABEL_OPTIMIZATION_SCHEDULER_POLL_SECONDS must be between 5 and 3600")
        if not 1 <= self.label_optimization_scheduler_batch_size <= 100:
            raise ValueError("LABEL_OPTIMIZATION_SCHEDULER_BATCH_SIZE must be between 1 and 100")
        if not 30 <= self.label_optimization_scheduler_claim_lease_seconds <= 3600:
            raise ValueError(
                "LABEL_OPTIMIZATION_SCHEDULER_CLAIM_LEASE_SECONDS must be between 30 and 3600"
            )

        if not is_production_environment(self.app_env):
            return self

        auth_provider = self.auth_provider.strip().lower()
        if self.allow_dev_auth or auth_provider == "dev":
            raise ValueError("ALLOW_DEV_AUTH and AUTH_PROVIDER=dev are forbidden in prod/release")
        if (
            auth_provider in {"auto", "signed", "hmac", "hmac_sha256"}
            and len(self.auth_token_secret.strip()) < 32
        ):
            raise ValueError("AUTH_TOKEN_SECRET must be at least 32 characters in prod/release")
        if len(self.audio_playback_grant_secret.strip()) < 32:
            raise ValueError(
                "AUDIO_PLAYBACK_GRANT_SECRET must be at least 32 characters in prod/release"
            )
        if (
            len(self.completion_receipt_secret.strip()) < 32
            and not self.completion_receipt_key_bindings.strip()
        ):
            raise ValueError(
                "COMPLETION_RECEIPT_SECRET or COMPLETION_RECEIPT_KEY_BINDINGS "
                "is required in prod/release"
            )
        if len(self.experiment_assignment_secret.strip()) < 32:
            raise ValueError(
                "EXPERIMENT_ASSIGNMENT_SECRET must be at least 32 characters in prod/release"
            )
        if not _csv_items(self.cors_allowed_origins) or "*" in _csv_items(
            self.cors_allowed_origins
        ):
            raise ValueError("CORS_ALLOWED_ORIGINS must be explicit in prod/release")
        if not _csv_items(self.trusted_hosts) or "*" in _csv_items(self.trusted_hosts):
            raise ValueError("TRUSTED_HOSTS must be explicit in prod/release")
        if self.auris_qdrant_adapter.strip().lower() == "real" and not self.qdrant_api_key.strip():
            raise ValueError(
                "QDRANT_API_KEY is required when AURIS_QDRANT_ADAPTER=real in prod/release"
            )
        if self.auris_external_callback_adapter.strip().lower() == "real":
            _validate_production_callback_settings(self)
        if self.auris_object_storage_adapter.strip().lower() == "real":
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
        return self


def _csv_items(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.replace("\n", ",").split(",") if item.strip())


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
    if len(settings.external_callback_secret.strip()) < 32:
        raise ValueError("EXTERNAL_CALLBACK_SECRET must be at least 32 characters in prod/release")


@lru_cache
def get_settings() -> Settings:
    return Settings()
