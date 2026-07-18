from __future__ import annotations

from typing import Any

from app.core.config import Settings, get_settings

FAILURE_INJECTION_ENVS = {"test", "ci"}
FAILURE_INJECTION_KEYS = {
    "adapter_error_code",
    "adapter_error_message",
    "adapter_retryable",
    "failure_reason",
    "force_adapter_error",
    "force_worker_error",
    "max_attempts",
    "retry_after_seconds",
    "simulate_adapter_failure",
    "simulate_worker_failure",
}


def failure_injection_enabled(settings: Settings | None = None) -> bool:
    active_settings = settings or get_settings()
    return active_settings.app_env in FAILURE_INJECTION_ENVS


def requested_failure_injection_fields(payload: dict[str, Any]) -> list[str]:
    return sorted(key for key in FAILURE_INJECTION_KEYS if key in payload)
