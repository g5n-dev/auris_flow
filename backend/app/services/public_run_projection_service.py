from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable, Collection
from ipaddress import ip_address
from typing import Any

from app.core.json_keys import json_key_fingerprint, normalize_json_key
from app.core.redaction import redact_structured_value

# RunRecord.payload is an internal evidence ledger. Every HTTP projection that
# derives from it must pass this module rather than serializing the ledger.
PUBLIC_RUN_FORBIDDEN_FIELD_FINGERPRINTS = frozenset(
    json_key_fingerprint(field)
    for field in (
        "access_key",
        "access_key_id",
        "access_token",
        "adapter",
        "adapter_dispatch",
        "adapter_mode",
        "authenticated_source",
        "auth",
        "authorization",
        "artifact_uri",
        "api_key",
        "bearer_token",
        "claim_token",
        "cookie",
        "cookies",
        "credential",
        "details",
        "dagster_run_draft",
        "dagster_run_id",
        "dispatch",
        "dispatch_idempotency_key",
        "dispatch_request",
        "dispatch_request_sha256",
        "dispatch_state",
        "engine_status",
        "engine_status_observed_at",
        "error",
        "execution_contract",
        "execution_deadline_at",
        "execution_envelope",
        "endpoint",
        "external_id",
        "external_run_id",
        "failed_event_id",
        "dead_letter_event_id",
        "graphql",
        "graphql_url",
        "headers",
        "hmac",
        "id_token",
        "input_object",
        "job_name",
        "monitor_generation",
        "next_status_sync_at",
        "nonce",
        "observed_engine_status",
        "password",
        "pipeline_name",
        "private_key",
        "partial_artifact_uri",
        "processed_event_id",
        "provider_evidence",
        "provider_artifact_ref",
        "protocol_receipt",
        "refresh_token",
        "remote_id",
        "remote_run_id",
        "repository_location_name",
        "repository_name",
        "request_headers",
        "response_typename",
        "run_config",
        "secret_ref",
        "signature",
        "signature_body_hash",
        "signature_key_id",
        "signature_mode",
        "signature_nonce",
        "signature_request_hash",
        "signed_at",
        "signed_url",
        "signing_key",
        "bucket",
        "download_url",
        "object_key",
        "object_uri",
        "storage_object_id",
        "result_storage_object_ids",
        "result_storage_object_sha256",
        "token",
        "uri",
        "url",
    )
)
PUBLIC_RUN_FORBIDDEN_FIELD_TOKENS = frozenset(
    {
        "adapter",
        "credential",
        "dagster",
        "details",
        "dispatch",
        "endpoint",
        "engine",
        "graphql",
        "headers",
        "hmac",
        "internal",
        "password",
        "protocol",
        "remote",
        "secret",
        "signature",
        "token",
    }
)
_STATUS_REASON_ALIASES = {
    "cancelled_before_engine_dispatch": "cancelled_before_execution",
    "completion_receipt": "result_confirmed",
    "dagster_cancellation_confirmed": "cancellation_confirmed",
    "dagster_cancellation_observed": "cancellation_confirmed",
    "dagster_completed_before_cancel": "result_pending",
    "dagster_completion_won_cancellation_race": "result_confirmed",
    "dagster_failed_during_cancellation": "execution_failed",
    "dagster_failure_observed": "execution_failed",
    "dagster_staged_completion_bound": "result_confirmed",
    "dagster_status_reconciled": "status_reconciled",
    "dagster_success_observed": "execution_succeeded",
    "outbox_dispatch_completed": "execution_completed",
    "outbox_dispatch_dead_letter": "execution_failed",
    "outbox_dispatch_started": "execution_started",
    "outbox_dispatch_submitted": "execution_submitted",
}
_INTERNAL_ADAPTER_VALUES = {
    "control_plane": "internal control",
    "dagster": "execution engine",
    "external_callback": "external integration",
    "label_policy": "policy service",
    "object_storage": "object service",
    "projection": "internal projection",
    "qdrant": "retrieval service",
}
_JOB_VALUE_PATTERN = re.compile(r"(?i)\b(?:job(?:_name)?\s+)?[a-z0-9._-]+_(?:job|pipeline)\b")
_ENGINE_VALUE_PATTERNS = (
    (re.compile("dagster", re.IGNORECASE), "execution engine", "EXECUTION"),
    (re.compile("graphql", re.IGNORECASE), "protocol", "PROTOCOL"),
    (re.compile("qdrant", re.IGNORECASE), "retrieval service", "RETRIEVAL"),
    (re.compile("minio", re.IGNORECASE), "object service", "OBJECT_SERVICE"),
    (re.compile("redis", re.IGNORECASE), "cache", "CACHE"),
    (re.compile("adapter", re.IGNORECASE), "integration", "INTEGRATION"),
    (re.compile("dispatch", re.IGNORECASE), "execution", "EXECUTION"),
)
_CANONICAL_FIELD_PATTERN = re.compile(r"^[A-Za-z0-9_.:/ -]+$")
_OPAQUE_VALUE_PATTERN = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._:/|+-]{0,511}|/[A-Za-z0-9._~:/?#&=%+|-]{0,2047})$"
)
_PUBLIC_NAVIGATION_PATH_PATTERN = re.compile(
    r"^/?[A-Za-z0-9][A-Za-z0-9._~/-]*"
    r"(?:\?[A-Za-z0-9._~!$&'()*+,;=:@/?-]*)?$"
)
_STABLE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_PUBLIC_STORAGE_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_URI_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_HOSTNAME_PATTERN = re.compile(
    r"^(?:[a-z0-9-]+\.)+[a-z]{2,}(?::[0-9]{1,5})?$",
    re.IGNORECASE,
)
_URI_IN_TEXT_PATTERN = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s]+")
_INTERNAL_HOST_IN_TEXT_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9.-])(?:localhost|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:internal|local|lan))(?::[0-9]{1,5})?(?![A-Za-z0-9.-])"
)
_IPV4_IN_TEXT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?::[0-9]{1,5})?(?![A-Za-z0-9])"
)
_AWS_ACCESS_KEY_VALUE_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_PUBLIC_ERROR_CODE_TOKEN_ALIASES = {
    "ADAPTER": "INTEGRATION",
    "DAGSTER": "EXECUTION",
    "DISPATCH": "EXECUTION",
    "GRAPHQL": "PROTOCOL",
    "MINIO": "OBJECT_SERVICE",
    "QDRANT": "RETRIEVAL",
    "REDIS": "CACHE",
}
_PUBLIC_RECEIPT_ID_PREFIX_ALIASES = {
    "dagster": "execution",
    "minio": "object-service",
    "qdrant": "retrieval",
    "redis": "cache",
}
_PUBLIC_ASSET_KEY_PATTERN = re.compile(r"^auris/[A-Za-z0-9._/-]{1,240}$")
_PUBLIC_CONTENT_TYPE_PATTERN = re.compile(
    r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+/[!#$%&'*+.^_`|~0-9A-Za-z-]+$"
)
_PUBLIC_COMPLETION_RECEIPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$")
_PUBLIC_COMPLETION_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_PUBLIC_TERMINAL_REASON_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
_FORBIDDEN_FIELD_SUFFIX_TOKENS = (
    ("url",),
    ("uri",),
    ("storage", "object", "id"),
    ("storage", "object", "ids"),
    ("storage", "object", "sha256"),
    ("object", "key"),
    ("object", "uri"),
    ("bucket",),
)
_COMPLETION_RECEIPT_CONTEXTS = frozenset({"completionreceipt", "completionreceipts"})
_COMPLETION_SUMMARY_FIELDS = frozenset(
    json_key_fingerprint(field)
    for field in (
        "completion_receipt_id",
        "status",
        "result_ref",
        "metrics",
        "error_code",
        "retryable",
        "received_at",
    )
)
_COMPLETION_RESULT_PUBLIC_FIELDS = {
    "resultref": frozenset(
        json_key_fingerprint(field)
        for field in (
            "action",
            "action_id",
            "applied",
            "artifact_id",
            "artifact_sha256",
            "artifact_state",
            "asr_segments",
            "asset_key",
            "audio_session_id",
            "badcase_candidates",
            "baseline_metrics",
            "baseline_mode",
            "baseline_ref",
            "baseline_version_id",
            "bundle_sha256",
            "candidate_artifact_id",
            "candidate_metrics",
            "capability_statuses",
            "change_set_id",
            "checks",
            "command_sha256",
            "content_length",
            "content_sha256",
            "content_type",
            "crosstalk_risk",
            "deployment_id",
            "downstream_asset_keys",
            "draft_ref",
            "environment",
            "error_count",
            "eval_dataset_id",
            "eval_run_id",
            "evaluated_term_ids",
            "evaluated_terms",
            "executed_task_version_binding_sha256",
            "experiment_id",
            "gate",
            "hotword_pack_version_id",
            "id",
            "labeling_eval_result",
            "locked",
            "materialization_id",
            "metric_results",
            "metric_snapshots",
            "object_id",
            "object_type",
            "observations",
            "outcome_metric",
            "pack_id",
            "partition_key",
            "per_term_trusted_occurrences",
            "prompt_candidate_id",
            "prompt_candidate_ids",
            "prompt_candidates",
            "record_count",
            "recording_id",
            "release_command_id",
            "rendered",
            "result_manifest_sha256",
            "review_decision_id",
            "scene_profile_manifest",
            "snr_db",
            "speaker_ref",
            "speaker_turns",
            "status",
            "storage_objects",
            "type",
            "upstream_asset_keys",
            "vad_segments",
            "version_status",
            "voiceprint",
            "voiceprint_id",
            "voiceprint_quality_score",
        )
    ),
    "metrics": frozenset(
        json_key_fingerprint(field)
        for field in (
            "abstain_count",
            "abstention_rate",
            "accuracy",
            "blocking_badcase_count",
            "blocking_regression_count",
            "brier",
            "candidate_count",
            "confidence_interval",
            "conflict_rate",
            "confusion_matrix",
            "cost_micros",
            "cost_ratio",
            "count",
            "critical_recall_delta_pp",
            "dev_macro_f1",
            "duplicate_count",
            "duration_ms",
            "ece",
            "eligible_count",
            "error_count",
            "evidence_count",
            "f1",
            "failure_count",
            "failed_samples",
            "human_override_delta_pp",
            "human_review_tasks",
            "invalid_count",
            "json_valid_rate",
            "json_validity",
            "labeling_f1",
            "latency_ms",
            "latency_ratio",
            "materialized_count",
            "materialized_partitions",
            "metric_schema_version",
            "p95_latency_ms",
            "pass_rate",
            "precision",
            "processed",
            "processed_count",
            "processed_samples",
            "quote_consistency",
            "rate",
            "recall",
            "record_count",
            "render_attempt",
            "retry_count",
            "sample_count",
            "sample_size",
            "score",
            "section_count",
            "skipped_count",
            "statistically_significant",
            "success_count",
            "throughput",
            "total_count",
            "wer",
        )
    ),
}
_COMPLETION_RESULT_FORBIDDEN_TOKENS = frozenset(
    {"evidence", "execution", "locator", "provider", "storage"}
)
_COMPLETION_RESULT_TOKEN_EXCEPTIONS = frozenset({"storageobjects"})
_COMPLETION_RESULT_SLASH_VALUE_FIELDS = frozenset(
    {"assetkey", "contenttype", "metricschemaversion"}
)
_COMPLETION_METRIC_STRUCTURED_FIELDS = frozenset({"confidenceinterval", "confusionmatrix"})
_PUBLIC_METRIC_SCHEMA_VERSION_PATTERN = re.compile(
    r"^[a-z][a-z0-9._-]{0,63}/[0-9][A-Za-z0-9._-]{0,31}$"
)
_COMPLETION_RESULT_FORBIDDEN_FIELD_FINGERPRINTS = frozenset(
    json_key_fingerprint(field)
    for field in (
        # Provider protocol evidence belongs to the internal receipt ledger.
        "provider",
        "provider_evidence",
        "provider_run_id",
        "provider_request_sha256",
        "provider_response_sha256",
        "provider_result_sha256",
        # Execution and immutable object-locator bindings are offline evidence.
        "execution_envelope_sha256",
        "result_manifest_object_key_sha256",
        "result_manifest_version_id_sha256",
        "storage_provider",
        "version_id",
        "object_version_id",
        "etag",
    )
)
_CONFUSABLE_TRANSLATION = str.maketrans(
    {
        "Α": "A",
        "Β": "B",
        "Ε": "E",
        "Ζ": "Z",
        "Η": "H",
        "Ι": "I",
        "Κ": "K",
        "Μ": "M",
        "Ν": "N",
        "Ο": "O",
        "Ρ": "P",
        "Τ": "T",
        "Χ": "X",
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "у": "y",
        "х": "x",
        "і": "i",
        "ј": "j",
        "ѕ": "s",
        "А": "A",
        "В": "B",
        "Е": "E",
        "К": "K",
        "М": "M",
        "Н": "H",
        "О": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "Х": "X",
    }
)
_MAX_DEPTH = 32
_OMITTED = object()


def _is_ip_locator(value: str) -> bool:
    """Reject IP literals, including the common IPv4/IPv6 host:port forms."""

    candidate = value
    if value.startswith("[") and "]" in value:
        candidate = value[1 : value.index("]")]
    elif value.count(":") == 1:
        host, separator, port = value.rpartition(":")
        if separator and port.isdigit():
            candidate = host
    try:
        ip_address(candidate)
    except ValueError:
        return False
    return True


def _contains_network_locator(value: str) -> bool:
    if _URI_IN_TEXT_PATTERN.search(value) or _INTERNAL_HOST_IN_TEXT_PATTERN.search(value):
        return True
    for match in _IPV4_IN_TEXT_PATTERN.finditer(value):
        if _is_ip_locator(match.group(0)):
            return True
    return False


def _public_error_code(value: str) -> str | None:
    if not _PUBLIC_COMPLETION_ERROR_CODE_PATTERN.fullmatch(value):
        return None
    return "_".join(
        _PUBLIC_ERROR_CODE_TOKEN_ALIASES.get(token, token) for token in value.split("_")
    )


def _public_completion_receipt_id(value: str) -> str | None:
    if not _PUBLIC_COMPLETION_RECEIPT_ID_PATTERN.fullmatch(value):
        return None
    prefix, separator, remainder = value.partition(":")
    replacement = _PUBLIC_RECEIPT_ID_PREFIX_ALIASES.get(prefix.casefold())
    if replacement is None or not separator:
        return value
    return f"{replacement}:{remainder}"


def _field_is_forbidden(
    field_name: str,
    *,
    extra_forbidden_fingerprints: Collection[str] = (),
) -> bool:
    canonical = unicodedata.normalize("NFKC", field_name)
    if canonical != field_name or not _CANONICAL_FIELD_PATTERN.fullmatch(canonical):
        return True
    fingerprint = json_key_fingerprint(field_name)
    if (
        fingerprint in PUBLIC_RUN_FORBIDDEN_FIELD_FINGERPRINTS
        or fingerprint in extra_forbidden_fingerprints
    ):
        return True
    normalized = re.sub(r"[^a-z0-9]+", "_", normalize_json_key(field_name))
    ordered_tokens = tuple(token for token in normalized.strip("_").split("_") if token)
    if any(
        len(ordered_tokens) >= len(suffix) and ordered_tokens[-len(suffix) :] == suffix
        for suffix in _FORBIDDEN_FIELD_SUFFIX_TOKENS
    ):
        return True
    return bool(frozenset(ordered_tokens) & PUBLIC_RUN_FORBIDDEN_FIELD_TOKENS)


def _replacement(plain: str, upper: str) -> Callable[[re.Match[str]], str]:
    def replace_engine_term(match: re.Match[str]) -> str:
        return upper if match.group(0).isupper() else plain

    return replace_engine_term


def _visible_string(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(char for char in normalized if unicodedata.category(char) != "Cf")


def _is_opaque_field(field_name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", normalize_json_key(field_name)).strip("_")
    return normalized in {"href", "id", "route"} or normalized.endswith(
        ("_checksum", "_etag", "_hash", "_id", "_key", "_ref", "_sha256")
    )


def sanitize_public_run_string(value: str, *, field_name: str) -> str:
    visible_value = _visible_string(value)
    security_value = visible_value.translate(_CONFUSABLE_TRANSLATION)
    normalized_value = security_value.casefold()
    if normalize_json_key(field_name) == "code" and _STABLE_CODE_PATTERN.fullmatch(visible_value):
        if not any(
            pattern.search(security_value) for pattern, _plain, _upper in _ENGINE_VALUE_PATTERNS
        ):
            return visible_value
    replacement = _INTERNAL_ADAPTER_VALUES.get(normalized_value)
    if replacement is not None:
        return replacement
    if normalize_json_key(field_name) == "reason":
        aliased = _STATUS_REASON_ALIASES.get(normalized_value)
        if aliased is not None:
            return aliased
        if normalized_value.endswith("_completion_received"):
            return "result_confirmed"

    sanitized = _JOB_VALUE_PATTERN.sub(
        lambda match: "WORKFLOW" if match.group(0).isupper() else "workflow",
        security_value,
    )
    for pattern, plain, upper in _ENGINE_VALUE_PATTERNS:
        sanitized = pattern.sub(_replacement(plain, upper), sanitized)
    redacted = redact_structured_value(sanitized, field_name=field_name)
    if (
        _is_opaque_field(field_name)
        and _OPAQUE_VALUE_PATTERN.fullmatch(sanitized)
        and isinstance(redacted, str)
        and "[REDACTED_" in redacted
        and "[REDACTED_SECRET]" not in redacted
    ):
        return sanitized
    return redacted if isinstance(redacted, str) else "[REDACTED]"


def project_public_navigation_path(value: str, *, field_name: str) -> str | None:
    """Return a sanitized same-origin route, or omit an ambiguous navigation target."""

    if unicodedata.normalize("NFKC", value) != value:
        return None
    sanitized = sanitize_public_run_string(value, field_name=field_name)
    if sanitized != value or not sanitized:
        return None
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in sanitized):
        return None
    if sanitized.startswith("//") or "\\" in sanitized or "#" in sanitized or "%" in sanitized:
        return None
    if not _PUBLIC_NAVIGATION_PATH_PATTERN.fullmatch(sanitized):
        return None
    path = sanitized.partition("?")[0]
    if "//" in path or any(segment in {".", ".."} for segment in path.split("/")):
        return None
    return sanitized


def _project_value(
    value: Any,
    *,
    field_name: str,
    depth: int = 0,
    extra_forbidden_fingerprints: Collection[str] = (),
    completion_result_context: bool = False,
    parent_context_fingerprint: str | None = None,
) -> Any:
    if depth > _MAX_DEPTH:
        return _OMITTED
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        context_fingerprint = json_key_fingerprint(field_name)
        completion_summary = context_fingerprint in _COMPLETION_RECEIPT_CONTEXTS
        result_public_fields = (
            _COMPLETION_RESULT_PUBLIC_FIELDS.get(context_fingerprint)
            if context_fingerprint == "resultref"
            or (
                context_fingerprint == "metrics"
                and parent_context_fingerprint in _COMPLETION_RECEIPT_CONTEXTS
            )
            else None
        )
        active_completion_result_context = (
            completion_result_context or result_public_fields is not None
        )
        effective_forbidden_fingerprints = frozenset(extra_forbidden_fingerprints)
        if result_public_fields is not None:
            effective_forbidden_fingerprints |= _COMPLETION_RESULT_FORBIDDEN_FIELD_FINGERPRINTS
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                continue
            raw_fingerprint = json_key_fingerprint(raw_key)
            if result_public_fields is not None and raw_fingerprint not in result_public_fields:
                continue
            if (
                result_public_fields is not None
                and isinstance(child, (dict, list, tuple))
                and raw_fingerprint
                not in ({"storageobjects"} | _COMPLETION_METRIC_STRUCTURED_FIELDS)
            ):
                # Signed callback documents remain internal evidence. Public
                # domain endpoints expose their materialized structures.
                continue
            normalized_tokens = frozenset(
                token
                for token in re.sub(r"[^a-z0-9]+", "_", normalize_json_key(raw_key))
                .strip("_")
                .split("_")
                if token
            )
            if (
                active_completion_result_context
                and not (
                    result_public_fields is not None and raw_fingerprint in result_public_fields
                )
                and raw_fingerprint not in _COMPLETION_RESULT_TOKEN_EXCEPTIONS
                and normalized_tokens & _COMPLETION_RESULT_FORBIDDEN_TOKENS
            ):
                continue
            if _field_is_forbidden(
                raw_key,
                extra_forbidden_fingerprints=effective_forbidden_fingerprints,
            ):
                continue
            if (
                completion_summary
                and json_key_fingerprint(raw_key) not in _COMPLETION_SUMMARY_FIELDS
            ):
                continue
            projected = _project_value(
                child,
                field_name=raw_key,
                depth=depth + 1,
                extra_forbidden_fingerprints=effective_forbidden_fingerprints,
                completion_result_context=active_completion_result_context,
                parent_context_fingerprint=context_fingerprint,
            )
            if projected is not _OMITTED:
                sanitized[raw_key] = projected
        return sanitized
    if isinstance(value, (list, tuple)):
        if completion_result_context and json_key_fingerprint(field_name) == "storageobjects":
            summaries: list[dict[str, str]] = []
            for child in value:
                if not isinstance(child, dict):
                    continue
                role = child.get("role")
                content_sha256 = child.get("content_sha256")
                if (
                    isinstance(role, str)
                    and _PUBLIC_STORAGE_ROLE_PATTERN.fullmatch(role)
                    and sanitize_public_run_string(role, field_name="role") == role
                    and isinstance(content_sha256, str)
                    and _SHA256_PATTERN.fullmatch(content_sha256)
                ):
                    summaries.append({"role": role, "content_sha256": content_sha256})
            return summaries
        projected_items = []
        for child in value:
            projected = _project_value(
                child,
                field_name=field_name,
                depth=depth + 1,
                extra_forbidden_fingerprints=extra_forbidden_fingerprints,
                completion_result_context=completion_result_context,
                parent_context_fingerprint=parent_context_fingerprint,
            )
            if projected is not _OMITTED:
                projected_items.append(projected)
        return projected_items
    if isinstance(value, str):
        field_fingerprint = json_key_fingerprint(field_name)
        visible_value = _visible_string(value).strip()
        if _AWS_ACCESS_KEY_VALUE_PATTERN.search(visible_value):
            return _OMITTED
        if normalize_json_key(field_name) not in {"href", "route"} and (
            _contains_network_locator(visible_value)
        ):
            return _OMITTED
        if field_fingerprint == "errorcode":
            public_error_code = _public_error_code(value)
            return public_error_code if public_error_code is not None else _OMITTED
        if field_fingerprint == "terminalreason" and not (
            _PUBLIC_TERMINAL_REASON_PATTERN.fullmatch(value)
        ):
            return _OMITTED
        if parent_context_fingerprint in _COMPLETION_RECEIPT_CONTEXTS:
            if field_fingerprint == "completionreceiptid":
                public_receipt_id = _public_completion_receipt_id(value)
                return public_receipt_id if public_receipt_id is not None else _OMITTED
        if completion_result_context:
            if (
                not visible_value
                or _URI_SCHEME_PATTERN.match(visible_value)
                or _HOSTNAME_PATTERN.fullmatch(visible_value)
                or _is_ip_locator(visible_value)
                or "%2f" in visible_value.casefold()
                or "%3a" in visible_value.casefold()
                or "?" in visible_value
                or "#" in visible_value
                or (
                    ("/" in visible_value or "\\" in visible_value)
                    and field_fingerprint not in _COMPLETION_RESULT_SLASH_VALUE_FIELDS
                )
            ):
                return _OMITTED
            if field_fingerprint == "assetkey":
                if not _PUBLIC_ASSET_KEY_PATTERN.fullmatch(visible_value) or any(
                    segment in {"", ".", ".."} for segment in visible_value.split("/")
                ):
                    return _OMITTED
            elif field_fingerprint == "contenttype" and not (
                _PUBLIC_CONTENT_TYPE_PATTERN.fullmatch(visible_value)
            ):
                return _OMITTED
            elif field_fingerprint == "metricschemaversion" and not (
                _PUBLIC_METRIC_SCHEMA_VERSION_PATTERN.fullmatch(visible_value)
            ):
                return _OMITTED
        if normalize_json_key(field_name) in {"href", "route"}:
            navigation_path = project_public_navigation_path(value, field_name=field_name)
            return navigation_path if navigation_path is not None else _OMITTED
        return sanitize_public_run_string(value, field_name=field_name)
    if isinstance(value, (bytes, bytearray)):
        return _OMITTED
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return _OMITTED


def public_run_payload(payload: dict[str, Any]) -> dict[str, Any]:
    projected = _project_value(payload, field_name="payload")
    if not isinstance(projected, dict):
        return {}
    # Completion metrics are duplicated at payload.metrics for legacy domain
    # consumers. Re-project that alias with the same strict callback allowlist;
    # otherwise the safer completion_receipt.metrics view can be bypassed.
    completion_receipt = payload.get("completion_receipt")
    completion_metrics = payload.get("metrics")
    if isinstance(completion_receipt, dict) and isinstance(completion_metrics, dict):
        strict_metrics = _project_value(
            completion_metrics,
            field_name="metrics",
            parent_context_fingerprint="completionreceipt",
        )
        projected["metrics"] = strict_metrics if isinstance(strict_metrics, dict) else {}
    return projected


def project_public_run_value(value: Any, *, field_name: str) -> Any:
    """Project a nested value for legacy envelope and idempotency replay paths."""

    projected = _project_value(value, field_name=field_name)
    return None if projected is _OMITTED else projected


def public_run_projection(
    value: dict[str, Any],
    *,
    allowed_fields: Collection[str] | None = None,
    forbidden_fields: Collection[str] = (),
    field_name: str = "data",
) -> dict[str, Any]:
    """Apply a top-level allowlist and recursive internal-evidence policy."""

    source = (
        {key: value[key] for key in allowed_fields if key in value}
        if allowed_fields is not None
        else value
    )
    projected = _project_value(
        source,
        field_name=field_name,
        extra_forbidden_fingerprints=frozenset(
            json_key_fingerprint(field) for field in forbidden_fields
        ),
    )
    return projected if isinstance(projected, dict) else {}
