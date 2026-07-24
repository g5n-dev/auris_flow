#!/usr/bin/env python3
"""Validate the backend spec pack before backend implementation starts."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - developer environment guard
    raise SystemExit("PyYAML is required: python3 -m pip install pyyaml") from exc


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[1]
API_PREFIX = "/api/v1"
OPENAPI_PATH = ROOT / "openapi-v0.1.yaml"
API_CONTRACT_PATH = ROOT / "api-contract.md"
SEED_PATH = ROOT / "seed-fixture-v0.1.json"
DOC_GLOB = "*.md"

HTTP_METHODS = {"get", "head", "post", "patch", "put", "delete"}
WRITING_METHODS = {"post", "patch", "put", "delete"}
IDEMPOTENCY_EXEMPT_OPERATIONS = {
    ("post", "/auth/dev-login"),
    ("post", "/auth/oidc/back-channel-logout"),
    ("post", "/auth/logout"),
}
PAGING_EXEMPT_PATHS = {"/hotword-statistics", "/insights/metric-comparisons"}
TYPED_RESOURCE_PREFIXES = (
    "/work-items",
    "/event-links",
    "/data-assets",
    "/traces",
    "/label-optimization-runs",
)
PLACEHOLDER_PATTERN = re.compile(r"T[O]DO|F[I]XME|待[补]|待[定]|data-assets/\{i[d]\}")
QDRANT_PAYLOAD_REQUIRED_FIELDS = {
    "collection",
    "tenant_id",
    "project_id",
    "asset_key",
    "source_type",
    "source_id",
    "version",
    "trace_id",
    "evidence_id",
    "label_version",
}
HOTWORD_REQUIRED_OPERATIONS = {
    ("get", "/hotword-statistics"),
    ("get", "/badcases"),
    ("post", "/badcases"),
    ("patch", "/badcases/{badcase_id}"),
    ("post", "/badcases/{badcase_id}/decisions"),
    ("get", "/hotword-packs"),
    ("post", "/hotword-packs"),
    ("get", "/hotword-packs/{pack_id}/versions"),
    ("post", "/hotword-packs/{pack_id}/versions"),
    ("get", "/hotword-pack-versions/{version_id}"),
    ("patch", "/hotword-pack-versions/{version_id}"),
    ("post", "/hotword-pack-versions/{version_id}/items"),
    ("patch", "/hotword-pack-versions/{version_id}/items/{item_id}"),
    ("delete", "/hotword-pack-versions/{version_id}/items/{item_id}"),
    ("post", "/hotword-analysis-runs"),
    ("post", "/hotword-pack-versions/{version_id}/eval-runs"),
    ("post", "/hotword-pack-versions/{version_id}/publish"),
}
HOTWORD_VERSION_STATES = {
    "draft",
    "validating",
    "ready_for_eval",
    "evaluating",
    "gate_blocked",
    "review_required",
    "approved",
    "published",
    "deprecated",
    "rolled_back",
    "archived",
}
HOTWORD_ERROR_TYPES = {
    "missing_term",
    "misrecognition",
    "alias_gap",
    "weight_issue",
    "false_boost",
}
HOTWORD_EVENT_TYPES = {
    "hotword_analysis.requested",
    "hotword_metrics.materialized",
    "hotword_pack_version.build-requested",
    "hotword_pack_version.built",
    "hotword_pack_version.eval-requested",
    "hotword_pack_version.eval-completed",
    "hotword_pack_version.publish-requested",
    "hotword_pack_version.published",
    "hotword_pack_version.rolled-back",
}
HOTWORD_ITEM_SOURCE_TYPES = {"manual", "badcase", "knowledge_candidate"}
HOTWORD_DIAGNOSTIC_TERM_FIELDS = {
    "matched_terms",
    "missed_terms",
    "false_boosted_terms",
}
HOTWORD_TABLES = {
    "hotword_packs",
    "hotword_pack_versions",
    "hotword_version_items",
    "hotword_metric_snapshots",
}
LABEL_LIFECYCLE_ADR_PATH = ROOT / "label-lifecycle-statistics.md"
LABEL_VERSION_STATES = {
    "draft",
    "candidate",
    "validated",
    "locked",
    "evaluating",
    "gate_blocked",
    "review_required",
    "approved",
    "published",
    "deprecated",
    "archived",
}
LABEL_TAXONOMY_MODES = {"native", "normalized", "recomputed"}
LABEL_MAPPING_RELATIONS = {
    "identity",
    "rename",
    "replace",
    "merge",
    "retire",
    "split-recompute",
}
LABEL_MAPPING_COMPATIBILITIES = {
    "exact",
    "metric-dependent",
    "structural-break",
    "not-applicable",
}
LABEL_COMPARABILITY_STATUSES = {
    "comparable",
    "partial",
    "structural-break",
    "not-applicable",
}
LABEL_VERSION_APPLICABILITY = {"required", "none"}
LABEL_METRIC_SCOPE_REQUIRED_FIELDS = {
    "taxonomy_mode",
    "source_label_version_ids",
    "target_label_version_id",
    "mapping_bundle_id",
    "fact_set_generation",
    "fact_as_of",
    "metric_definition_versions",
    "timezone",
    "period_boundary",
    "denominator_definition",
}
LABEL_LIFECYCLE_HTTP_CONTRACT = {
    "/label-versions/{id}/deprecation-preflights": (
        "LabelVersionDeprecationPreflightRequest",
        "LabelVersionDeprecationPreflightResponse",
    ),
    "/label-versions/{id}/transitions": (
        "LabelVersionTransitionRequest",
        "LabelVersionTransitionResponse",
    ),
}
LABEL_MAPPING_HTTP_CONTRACT = {
    "/label-mapping-versions/dry-run": (
        "LabelMappingCreateRequest",
        "LabelMappingDryRunResponse",
        "200",
        "postLabelMappingVersionsDryRun",
    ),
    "/label-mapping-versions": (
        "LabelMappingCreateRequest",
        "LabelMappingVersionCreateResponse",
        "201",
        "postLabelMappingVersions",
    ),
    "/label-mapping-versions/{id}/validate": (
        "LabelMappingValidationRequest",
        "LabelMappingValidationResponse",
        "200",
        "postLabelMappingVersionsByIdValidate",
    ),
    "/label-mapping-versions/{id}/approve": (
        "LabelMappingApprovalRequest",
        "LabelMappingApprovalResponse",
        "200",
        "postLabelMappingVersionsByIdApprove",
    ),
    "/label-mapping-bundles/publish": (
        "LabelMappingBundlePublishRequest",
        "LabelMappingBundlePublishResponse",
        "201",
        "postLabelMappingBundlesPublish",
    ),
}
LABEL_MAPPING_REQUEST_FIELDS = {
    "LabelMappingCreateRequest": {
        "mapping_version",
        "source_label_version_id",
        "target_label_version_id",
        "expected_source_resource_version",
        "expected_target_resource_version",
        "items",
    },
    "LabelMappingValidationRequest": {"expected_resource_version"},
    "LabelMappingApprovalRequest": {"expected_resource_version", "reason"},
    "LabelMappingBundlePublishRequest": {
        "mapping_version_ids",
        "expected_mapping_resource_versions",
        "source_label_version_ids",
        "expected_source_resource_versions",
        "target_label_version_id",
        "expected_target_resource_version",
    },
}
LABEL_MAPPING_RESPONSE_RESULTS = {
    "LabelMappingDryRunResponse": "LabelMappingDryRunResult",
    "LabelMappingVersionCreateResponse": "LabelMappingVersionCreateResult",
    "LabelMappingValidationResponse": "LabelMappingValidationResult",
    "LabelMappingApprovalResponse": "LabelMappingApprovalResult",
    "LabelMappingBundlePublishResponse": "LabelMappingBundlePublishResult",
}
LABEL_FACT_SET_HTTP_CONTRACT = {
    "/label-fact-sets": (
        "LabelFactSetCreateRequest",
        "LabelFactSetMutationEnvelope",
        "201",
        "postLabelFactSets",
    ),
    "/label-fact-sets/{id}/validations": (
        "LabelFactSetValidateRequest",
        "LabelFactSetMutationEnvelope",
        "200",
        "postLabelFactSetsByIdValidations",
    ),
    "/label-fact-sets/{id}/approvals": (
        "LabelFactSetApproveRequest",
        "LabelFactSetMutationEnvelope",
        "200",
        "postLabelFactSetsByIdApprovals",
    ),
    "/label-fact-sets/{id}/promotions": (
        "LabelFactSetPublishPromoteRequest",
        "LabelFactSetPromotionEnvelope",
        "200",
        "postLabelFactSetsByIdPromotions",
    ),
    "/label-fact-sets/{id}/rollbacks": (
        "LabelFactSetRollbackRequest",
        "LabelFactSetPromotionEnvelope",
        "200",
        "postLabelFactSetsByIdRollbacks",
    ),
}
LABEL_FACT_SET_REQUEST_FIELDS = {
    "LabelFactSetCreateRequest": {
        "required": {
            "fact_namespace",
            "target_label_version_id",
            "fact_as_of",
            "partition_manifest",
            "partition_manifest_sha256",
            "source_manifest_sha256",
            "result_manifest_sha256",
            "row_count",
        },
        "properties": {
            "fact_namespace",
            "target_label_version_id",
            "fact_as_of",
            "partition_manifest",
            "partition_manifest_sha256",
            "source_manifest_sha256",
            "result_manifest_sha256",
            "row_count",
        },
    },
    "LabelFactSetValidateRequest": {
        "required": {"expected_manifest_sha256"},
        "properties": {"expected_manifest_sha256"},
    },
    "LabelFactSetApproveRequest": {
        "required": {"expected_manifest_sha256", "approval_id", "reason"},
        "properties": {"expected_manifest_sha256", "approval_id", "reason"},
    },
    "LabelFactSetPublishPromoteRequest": {
        "required": {"environment", "action", "expected_generation"},
        "properties": {
            "environment",
            "action",
            "expected_generation",
            "expected_current_fact_set_id",
            "expected_current_manifest_sha256",
        },
    },
    "LabelFactSetRollbackRequest": {
        "required": {
            "environment",
            "expected_generation",
            "expected_current_fact_set_id",
            "expected_current_manifest_sha256",
        },
        "properties": {
            "environment",
            "action",
            "expected_generation",
            "expected_current_fact_set_id",
            "expected_current_manifest_sha256",
        },
    },
}
LABEL_FACT_SET_RESPONSE_RESULTS = {
    "LabelFactSetMutationEnvelope": "LabelFactSetMutationResponse",
    "LabelFactSetPromotionEnvelope": "LabelFactSetPromotionResponse",
}
MANUAL_LABEL_HTTP_CONTRACT = {
    "/audio-sessions/{id}/annotations/{annotation_id}/submissions": (
        "ManualLabelDraftSubmitRequest",
        {"201": "ManualLabelDraftSubmissionEnvelope"},
        "postAudioSessionsByIdAnnotationsByAnnotationIdSubmissions",
    ),
    "/audio-sessions/{id}/annotations/{annotation_id}/rebases": (
        "ManualLabelDraftRebaseRequest",
        {
            "200": "ManualLabelDraftRebasePreviewEnvelope",
            "201": "ManualLabelDraftRebaseConfirmEnvelope",
        },
        "postAudioSessionsByIdAnnotationsByAnnotationIdRebases",
    ),
}
MANUAL_LABEL_REQUEST_FIELDS = {
    "ManualLabelDraftCreateRequest": {
        "required": {
            "annotation_kind",
            "annotation_id",
            "label_version_id",
            "label_id",
            "subject_scope",
            "subject_key",
            "event_or_segment_id",
            "occurred_at",
            "evidence_ref",
            "value_type",
            "value",
            "expected_release_head_generation",
        },
        "properties": {
            "annotation_kind",
            "annotation_id",
            "label_version_id",
            "label_id",
            "subject_scope",
            "subject_key",
            "event_or_segment_id",
            "assertion_slot",
            "occurred_at",
            "evidence_ref",
            "value_type",
            "value",
            "environment",
            "expected_release_head_generation",
        },
    },
    "ManualLabelDraftSubmitRequest": {
        "required": {
            "expected_draft_sha256",
            "expected_release_head_generation",
            "confirmation",
        },
        "properties": {
            "expected_draft_sha256",
            "expected_release_head_generation",
            "confirmation",
        },
    },
    "ManualLabelDraftRebasePreviewRequest": {
        "required": {
            "action",
            "mapping_bundle_id",
            "expected_release_head_generation",
        },
        "properties": {
            "action",
            "mapping_bundle_id",
            "target_label_id",
            "expected_release_head_generation",
        },
    },
    "ManualLabelDraftRebaseConfirmRequest": {
        "required": {
            "action",
            "mapping_bundle_id",
            "expected_release_head_generation",
            "new_annotation_id",
            "preview_sha256",
            "confirmation",
        },
        "properties": {
            "action",
            "mapping_bundle_id",
            "target_label_id",
            "expected_release_head_generation",
            "new_annotation_id",
            "preview_sha256",
            "confirmation",
        },
    },
}
MANUAL_LABEL_RESPONSE_RESULTS = {
    "ManualLabelDraftMutationEnvelope": "ManualLabelDraftMutationResponse",
    "ManualLabelDraftSubmissionEnvelope": "ManualLabelDraftSubmissionResponse",
    "ManualLabelDraftRebasePreviewEnvelope": "ManualLabelDraftRebasePreviewResponse",
    "ManualLabelDraftRebaseConfirmEnvelope": "ManualLabelDraftRebaseConfirmResponse",
}
LABEL_LIFECYCLE_PREFLIGHT_RESPONSE_FIELDS = {
    "preflight_id",
    "label_version_id",
    "expected_resource_version",
    "replacement_label_version_id",
    "mapping_bundle_id",
    "active_environment_references",
    "draining_environment_references",
    "in_flight_run_references",
    "downstream_impacts",
    "downstream_impact_total",
    "blocking_impact_total",
    "migration_required_impact_total",
    "historical_reference_total",
    "impact_next_cursor",
    "impact_scan_complete",
    "migration_evidence_required",
    "migration_evidence_satisfied",
    "blockers",
    "ready_for_transition",
    "safe_stop_required",
    "audit_id",
    "outbox_event_id",
    "trace_id",
}
LABEL_LIFECYCLE_TRANSITION_RESPONSE_FIELDS = {
    "label_version_id",
    "action",
    "status",
    "artifact_status",
    "resource_version",
    "replacement_label_version_id",
    "mapping_bundle_id",
    "normalized_disposition",
    "safe_stop_required",
    "audit_id",
    "outbox_event_id",
    "trace_id",
}
LABEL_LIFECYCLE_DOC_REQUIREMENTS = {
    "domain-model.md": {
        "LabelMappingBundle",
        "occurred_at",
        "recorded_at",
        "fact_as_of",
        "normalized",
        "recomputed",
    },
    "state-machines.md": {
        "artifact lifecycle",
        "activation lifecycle",
        "draining",
        "structural-break",
        "STALE_LABEL_VERSION",
    },
    "api-contract.md": {
        "taxonomy_mode",
        "mapping_bundle_id",
        "fact_set_generation",
        "fact_as_of",
        "comparability_status",
    },
    "event-contracts.md": {
        "label_version.deprecated",
        "label_mapping_bundle.published",
        "label_fact_set.promoted",
        "insight_metric.materialized",
    },
    "db-schema.md": {
        "label_mapping_versions",
        "label_mapping_bundles",
        "label_fact_heads",
        "label_fact_sets",
        "metric_result_label_scopes",
    },
    "test-plan.md": {
        "native",
        "normalized",
        "recomputed",
        "coverage-gap",
        "same-subject-different-event",
    },
}


def fail(message: str, details: Any = None) -> None:
    print(f"[FAIL] {message}", file=sys.stderr)
    if details:
        print(json.dumps(details, ensure_ascii=False, indent=2), file=sys.stderr)
    raise SystemExit(1)


def ok(message: str) -> None:
    print(f"[OK] {message}")


def load_openapi() -> dict[str, Any]:
    with OPENAPI_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def validate_openapi_local_references(openapi: dict[str, Any]) -> None:
    dangling: list[str] = []

    def resolve(pointer: str) -> bool:
        current: Any = openapi
        for raw_token in pointer.removeprefix("#/").split("/"):
            token = raw_token.replace("~1", "/").replace("~0", "~")
            if not isinstance(current, dict) or token not in current:
                return False
            current = current[token]
        return True

    def walk(value: Any, location: str) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/") and not resolve(ref):
                dangling.append(f"{location}: {ref}")
            for key, child in value.items():
                walk(child, f"{location}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{location}/{index}")

    walk(openapi, "#")
    if dangling:
        fail("OpenAPI contains dangling local references", dangling)
    ok("OpenAPI local references resolve")


def validate_openapi_contract(openapi: dict[str, Any]) -> None:
    paths = openapi.get("paths", {})
    operations = {
        ("/api/v1" + path, method.upper())
        for path, spec in paths.items()
        for method in spec
        if method.lower() in HTTP_METHODS
    }

    api_contract = API_CONTRACT_PATH.read_text(encoding="utf-8")
    declared: list[tuple[str, str]] = []
    for match in re.finditer(
        r"`((?:GET|POST|PATCH|DELETE|PUT)\s+/api/v1/[^`]+)`", api_contract
    ):
        method, path = match.group(1).split(" ", 1)
        item = (path.split("?")[0], method)
        if item not in declared:
            declared.append(item)

    missing = [
        (method, path) for path, method in declared if (path, method) not in operations
    ]
    if missing:
        fail("api-contract.md has operations missing from OpenAPI", missing)
    ok(f"OpenAPI covers api-contract operations: {len(declared)}/{len(declared)}")


def validate_openapi_quality(openapi: dict[str, Any]) -> None:
    missing_operation_ids: list[tuple[str, str]] = []
    operation_ids: list[str] = []
    missing_idempotency: list[tuple[str, str]] = []
    missing_paging: list[tuple[str, str, str]] = []
    critical_generic: list[tuple[str, str, str, str]] = []

    for path, spec in openapi.get("paths", {}).items():
        for method, op in spec.items():
            if method not in HTTP_METHODS:
                continue

            operation_id = op.get("operationId")
            if not operation_id:
                missing_operation_ids.append((method.upper(), path))
            else:
                operation_ids.append(operation_id)

            for code, response in (op.get("responses") or {}).items():
                response_ref = (
                    response.get("$ref", "") if isinstance(response, dict) else ""
                )
                if response_ref.endswith(
                    ("GenericObject", "GenericList")
                ) and path.startswith(TYPED_RESOURCE_PREFIXES):
                    critical_generic.append((method.upper(), path, code, response_ref))

            refs = [
                p.get("$ref", "")
                for p in op.get("parameters", [])
                if isinstance(p, dict)
            ]
            if (
                method in WRITING_METHODS
                and (method, path) not in IDEMPOTENCY_EXEMPT_OPERATIONS
                and not any("IdempotencyKey" in ref for ref in refs)
            ):
                missing_idempotency.append((method.upper(), path))

            is_list_like = "列表" in op.get("summary", "") or path.endswith("s")
            if method == "get" and is_list_like and path not in PAGING_EXEMPT_PATHS:
                has_cursor = any("Cursor" in ref for ref in refs)
                has_limit = any("Limit" in ref for ref in refs)
                if not (has_cursor and has_limit):
                    missing_paging.append((method.upper(), path, op.get("summary", "")))

    duplicate_operation_ids = sorted(
        [op_id for op_id, count in Counter(operation_ids).items() if count > 1]
    )

    if missing_operation_ids:
        fail("OpenAPI operations missing operationId", missing_operation_ids)
    if duplicate_operation_ids:
        fail("OpenAPI operationId values are not unique", duplicate_operation_ids)
    if missing_idempotency:
        fail("write operations missing Idempotency-Key", missing_idempotency)
    if missing_paging:
        fail("list-like GET operations missing cursor/limit", missing_paging)
    if critical_generic:
        fail(
            "critical resource operations still use Generic responses", critical_generic
        )

    ok(f"OpenAPI operationId complete and unique: {len(operation_ids)}")
    ok("OpenAPI idempotency, paging, and critical typed response gates pass")


def validate_hotword_contract(openapi: dict[str, Any]) -> None:
    paths = openapi.get("paths", {})
    missing_operations = sorted(
        (method.upper(), path)
        for method, path in HOTWORD_REQUIRED_OPERATIONS
        if method not in paths.get(path, {})
    )
    if missing_operations:
        fail("ASR hotword governance operations are incomplete", missing_operations)

    schemas = openapi.get("components", {}).get("schemas", {})

    statistics_operation = paths["/hotword-statistics"]["get"]
    statistics_parameters = {
        parameter.get("name")
        for parameter in statistics_operation.get("parameters", [])
        if isinstance(parameter, dict) and parameter.get("in") == "query"
    }
    if "hotword_pack_version_id" not in statistics_parameters:
        fail("hotword statistics must support immutable version filtering")

    statistics_schema = schemas.get("HotwordStatisticsResponse", {})
    statistics_data = statistics_schema.get("properties", {}).get("data", {})
    required_statistics_sections = {"summary", "items", "dimensions"}
    missing_statistics_sections = sorted(
        required_statistics_sections - set(statistics_data.get("required", []))
    )
    if missing_statistics_sections:
        fail(
            "hotword statistics response must expose data.summary/items/dimensions",
            missing_statistics_sections,
        )
    dimensions_ref = (
        statistics_data.get("properties", {}).get("dimensions", {}).get("$ref", "")
    )
    if not dimensions_ref.endswith("/HotwordStatisticDimensions"):
        fail("hotword statistics dimensions must use a typed schema")
    dimensions_schema = schemas.get("HotwordStatisticDimensions", {})
    required_dimensions = {
        "date_from",
        "date_to",
        "store_id",
        "provider",
        "model_version",
        "hotword_pack_version_id",
    }
    if set(dimensions_schema.get("required", [])) != required_dimensions:
        fail("hotword statistics dimensions do not match the frozen filters")

    for path, label in (
        ("/hotword-pack-versions/{version_id}/eval-runs", "eval"),
        ("/hotword-pack-versions/{version_id}/publish", "publish"),
    ):
        responses = paths[path]["post"].get("responses", {})
        if "202" not in responses or any(code in responses for code in ("200", "201")):
            fail(f"hotword {label} must return only 202 for accepted work")
        response_ref = (
            responses["202"]
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
            .get("$ref", "")
        )
        if not response_ref.endswith("/RunActionResponse"):
            fail(f"hotword {label} 202 response must be a RunActionResponse")

    run_action = schemas.get("RunAction", {})
    required_run_action_fields = {
        "id",
        "run_id",
        "run_type",
        "status",
        "trace_id",
        "affected_objects",
        "next_actions",
    }
    if not required_run_action_fields.issubset(set(run_action.get("required", []))):
        fail("RunAction is missing pending-operation observability fields")
    if "pending" not in set(schemas.get("RunStatus", {}).get("enum", [])):
        fail("RunAction status schema must permit pending")

    eval_request = schemas.get("CreateHotwordEvalRunRequest", {})
    eval_request_fields = set(eval_request.get("properties", {}))
    required_eval_request_fields = {
        "eval_dataset_id",
        "provider",
        "expected_resource_version",
    }
    if not required_eval_request_fields.issubset(set(eval_request.get("required", []))):
        fail("hotword eval request is missing frozen-binding inputs")
    forbidden_eval_request_fields = {
        "baseline_metrics",
        "candidate_metrics",
        "gate",
        "lock_result",
    }
    leaked_eval_fields = sorted(forbidden_eval_request_fields & eval_request_fields)
    if leaked_eval_fields:
        fail(
            "hotword eval request must not accept client-reported results",
            leaked_eval_fields,
        )

    version_patch_request = schemas.get("UpdateHotwordPackVersionRequest", {})
    version_patch_fields = set(version_patch_request.get("properties", {}))
    if "provider" not in version_patch_fields:
        fail("hotword version patch must accept a validating build provider")
    if "provider" in set(version_patch_request.get("required", [])):
        fail("hotword version patch provider must remain optional")
    forbidden_client_build_outputs = {"compiled_provider", "provider_artifact_ref"}
    leaked_build_outputs = sorted(forbidden_client_build_outputs & version_patch_fields)
    if leaked_build_outputs:
        fail(
            "hotword version patch must not accept trusted build outputs",
            leaked_build_outputs,
        )
    provider_description = (
        version_patch_request.get("properties", {})
        .get("provider", {})
        .get("description", "")
    )
    required_provider_contract_tokens = {
        "status=validating",
        "compiled_provider",
        "受信构建完成回执",
    }
    missing_provider_contract_tokens = sorted(
        token
        for token in required_provider_contract_tokens
        if token not in provider_description
    )
    if missing_provider_contract_tokens:
        fail(
            "hotword version patch provider lacks build-only safeguards",
            missing_provider_contract_tokens,
        )

    audio_request = schemas.get("AudioIntelligenceRunRequest", {})
    audio_properties = audio_request.get("properties", {})
    required_audio_fields = {
        "execution_mode",
        "language",
        "hotword_pack_version_id",
        "return_word_timestamps",
    }
    missing_audio_fields = sorted(required_audio_fields - set(audio_properties))
    if missing_audio_fields:
        fail(
            "AudioIntelligenceRunRequest is missing hotword governance fields",
            missing_audio_fields,
        )

    completion = schemas.get("RunCompletionReceiptRequest", {})
    result_properties = (
        completion.get("properties", {}).get("result_ref", {}).get("properties", {})
    )
    required_completion_fields = {
        "word_timestamps_storage_object_id",
        "hotword_diagnostics",
    }
    missing_completion_fields = sorted(
        required_completion_fields - set(result_properties)
    )
    if missing_completion_fields:
        fail(
            "audio intelligence completion receipt is missing diagnostics fields",
            missing_completion_fields,
        )

    required_eval_completion_fields = {
        "hotword_pack_version_id",
        "eval_dataset_id",
        "content_sha256",
        "manifest_storage_object_id",
        "provider",
        "provider_artifact_ref",
        "baseline_metrics",
        "candidate_metrics",
        "locked",
    }
    missing_eval_completion_fields = sorted(
        required_eval_completion_fields - set(result_properties)
    )
    if missing_eval_completion_fields:
        fail(
            "trusted hotword eval completion is missing frozen bindings or results",
            missing_eval_completion_fields,
        )
    required_publish_completion_fields = {
        "hotword_pack_version_id",
        "pack_id",
        "task_version_id",
        "content_sha256",
        "provider",
        "provider_artifact_ref",
    }
    missing_publish_completion_fields = sorted(
        required_publish_completion_fields - set(result_properties)
    )
    if missing_publish_completion_fields:
        fail(
            "trusted hotword publish completion is missing frozen bindings or draft reference",
            missing_publish_completion_fields,
        )

    diagnostics = schemas.get("HotwordDiagnostics", {})
    diagnostics_properties = set(diagnostics.get("properties", {}))
    diagnostics_required = set(diagnostics.get("required", []))
    required_diagnostics = {
        "hotword_pack_version_id",
        *HOTWORD_DIAGNOSTIC_TERM_FIELDS,
        "diagnostics_storage_object_id",
    }
    if not required_diagnostics.issubset(diagnostics_required):
        fail("HotwordDiagnostics must require canonical terms and object reference")
    if not HOTWORD_DIAGNOSTIC_TERM_FIELDS.issubset(diagnostics_properties):
        fail("HotwordDiagnostics canonical term arrays are incomplete")
    if "false_boost_terms" in diagnostics_properties:
        fail("HotwordDiagnostics contains deprecated false_boost_terms alias")

    for schema_name, required_fields in (
        ("HotwordVersionItem", {"source_type"}),
        ("HotwordItemCreateRequest", {"source_type"}),
        ("HotwordItemPatchRequest", {"source_type"}),
        ("HotwordPackVersion", {"compiled_provider"}),
        ("Badcase", {"hotword_pack_version_id", "evidence_storage_object_id"}),
        (
            "CreateBadcaseRequest",
            {"hotword_pack_version_id", "evidence_storage_object_id"},
        ),
    ):
        missing_fields = sorted(
            required_fields - set(schemas.get(schema_name, {}).get("properties", {}))
        )
        if missing_fields:
            fail(f"{schema_name} is missing hotword governance fields", missing_fields)

    for schema_name in (
        "HotwordVersionItem",
        "HotwordItemCreateRequest",
        "HotwordItemPatchRequest",
    ):
        actual_source_types = set(
            schemas.get(schema_name, {})
            .get("properties", {})
            .get("source_type", {})
            .get("enum", [])
        )
        if actual_source_types != HOTWORD_ITEM_SOURCE_TYPES:
            fail(f"{schema_name}.source_type enum does not match the contract")

    request_schemas_with_legacy_hotwords = sorted(
        schema_name
        for schema_name, schema in schemas.items()
        if schema_name.endswith("Request")
        and "hotwords_ref" in schema.get("properties", {})
    )
    if request_schemas_with_legacy_hotwords:
        fail(
            "legacy hotwords_ref must remain read-only",
            request_schemas_with_legacy_hotwords,
        )

    actual_states = set(schemas.get("HotwordPackVersionStatus", {}).get("enum", []))
    if actual_states != HOTWORD_VERSION_STATES:
        fail(
            "HotwordPackVersionStatus enum does not match the frozen state machine",
            {
                "missing": sorted(HOTWORD_VERSION_STATES - actual_states),
                "unexpected": sorted(actual_states - HOTWORD_VERSION_STATES),
            },
        )

    actual_error_types = set(schemas.get("AsrHotwordErrorType", {}).get("enum", []))
    if actual_error_types != HOTWORD_ERROR_TYPES:
        fail(
            "ASR hotword badcase error types do not match the contract",
            {
                "missing": sorted(HOTWORD_ERROR_TYPES - actual_error_types),
                "unexpected": sorted(actual_error_types - HOTWORD_ERROR_TYPES),
            },
        )

    db_schema = (ROOT / "db-schema.md").read_text(encoding="utf-8")
    missing_tables = sorted(
        table for table in HOTWORD_TABLES if f"`{table}`" not in db_schema
    )
    if missing_tables:
        fail("ASR hotword governance strong tables are missing", missing_tables)

    event_contracts = (ROOT / "event-contracts.md").read_text(encoding="utf-8")
    missing_events = sorted(
        event for event in HOTWORD_EVENT_TYPES if f"`{event}`" not in event_contracts
    )
    if missing_events:
        fail("ASR hotword governance events are missing", missing_events)
    eval_requested_line = next(
        (
            line
            for line in event_contracts.splitlines()
            if "`hotword_pack_version.eval-requested`" in line
        ),
        "",
    )
    client_result_tokens = {"`baseline_metrics`", "`candidate_metrics`", "`gate`"}
    leaked_event_results = sorted(
        token for token in client_result_tokens if token in eval_requested_line
    )
    if leaked_event_results:
        fail(
            "hotword eval-requested event must not carry client-reported results",
            leaked_event_results,
        )
    frozen_eval_tokens = {
        "`content_sha256`",
        "`manifest_storage_object_id`",
        "`compiled_provider`",
        "`provider_artifact_ref`",
        "`eval_dataset_id`",
    }
    missing_frozen_tokens = sorted(
        token for token in frozen_eval_tokens if token not in eval_requested_line
    )
    if missing_frozen_tokens:
        fail(
            "hotword eval-requested event is missing frozen bindings",
            missing_frozen_tokens,
        )
    required_event_payload_tokens = {
        "hotword_pack_version.build-requested": {
            "`run_id`",
            "`version_id`",
            "`content_sha256`",
            "`manifest_storage_object_id`",
            "`target_provider`",
        },
        "hotword_pack_version.built": {
            "`run_id`",
            "`version_id`",
            "`compiled_provider`",
            "`provider_artifact_ref`",
        },
        "hotword_pack_version.publish-requested": {
            "`run_id`",
            "`version_id`",
            "`eval_run_id`",
            "`content_sha256`",
            "`compiled_provider`",
            "`provider_artifact_ref`",
            "`model_approved_by`",
            "`project_admin_confirmed_by`",
        },
        "hotword_pack_version.published": {
            "`run_id`",
            "`pack_id`",
            "`version_id`",
            "`eval_run_id`",
            "`task_version_id`",
            "`content_sha256`",
            "`compiled_provider`",
            "`provider_artifact_ref`",
        },
    }
    for event_type, expected_tokens in required_event_payload_tokens.items():
        event_line = next(
            (
                line
                for line in event_contracts.splitlines()
                if f"`{event_type}`" in line
            ),
            "",
        )
        missing_tokens = sorted(
            token for token in expected_tokens if token not in event_line
        )
        if missing_tokens:
            fail(
                f"{event_type} payload is missing async frozen-binding fields",
                missing_tokens,
            )

    state_machines = (ROOT / "state-machines.md").read_text(encoding="utf-8")
    gate_tokens = {
        "可信出现不少于 30",
        "单词不少于 3",
        "相对下降不少于 20%",
        "召回提升不少于 3pp",
        "误增强率增幅不超过 0.5pp",
        "CER/WER 退化不超过 0.2pp",
        "F1 退化不超过 0.5pp",
        "延迟及分钟成本增幅均不超过 5%",
    }
    missing_gate_tokens = sorted(
        token for token in gate_tokens if token not in state_machines
    )
    if missing_gate_tokens:
        fail("ASR hotword release gates are incomplete", missing_gate_tokens)
    build_transition_line = next(
        (
            line
            for line in state_machines.splitlines()
            if "`start_validation_and_build`" in line
        ),
        "",
    )
    rebuild_source_states = {
        "`draft`",
        "`ready_for_eval`",
        "`gate_blocked`",
        "`review_required`",
        "`approved`",
    }
    missing_rebuild_states = sorted(
        state for state in rebuild_source_states if state not in build_transition_line
    )
    if missing_rebuild_states:
        fail(
            "hotword build transition does not cover rebuildable candidate states",
            missing_rebuild_states,
        )

    ok(
        "ASR hotword operations, async receipts, frozen bindings, schemas, events, and gates are complete"
    )


def validate_label_mapping_http_contract(openapi: dict[str, Any]) -> None:
    paths = openapi.get("paths", {})
    schemas = openapi.get("components", {}).get("schemas", {})
    errors: list[str] = []
    required_error_statuses = {"400", "401", "403", "404", "409", "422", "429", "503"}
    required_parameter_names = {
        "XTenantId",
        "XProjectId",
        "XRequestId",
        "IdempotencyKey",
    }

    def schema_ref(value: dict[str, Any]) -> str:
        return (
            value.get("content", {})
            .get("application/json", {})
            .get("schema", {})
            .get("$ref", "")
        )

    for path, (
        request_schema_name,
        response_schema_name,
        success_status,
        operation_id,
    ) in LABEL_MAPPING_HTTP_CONTRACT.items():
        operation = paths.get(path, {}).get("post", {})
        if not operation:
            errors.append(f"POST {path} is missing")
            continue
        if set(operation.get("tags", [])) != {"label-mappings"}:
            errors.append(f"POST {path} must use only the label-mappings tag")
        if operation.get("operationId") != operation_id:
            errors.append(f"POST {path} operationId drifted")

        expected_parameter_names = set(required_parameter_names)
        if "{id}" in path:
            expected_parameter_names.add("Id")
        actual_parameter_names = {
            parameter.get("$ref", "").rsplit("/", 1)[-1]
            for parameter in operation.get("parameters", [])
            if isinstance(parameter, dict) and "$ref" in parameter
        }
        if actual_parameter_names != expected_parameter_names:
            errors.append(f"POST {path} context/idempotency parameters drifted")

        request_body = operation.get("requestBody", {})
        if request_body.get("required") is not True:
            errors.append(f"POST {path} requestBody must be required")
        if not schema_ref(request_body).endswith(f"/{request_schema_name}"):
            errors.append(f"POST {path} must use {request_schema_name}")

        responses = operation.get("responses", {})
        success_responses = {
            status
            for status in responses
            if status.isdigit() and status.startswith("2")
        }
        if success_responses != {success_status}:
            errors.append(f"POST {path} success status drifted")
        if not schema_ref(responses.get(success_status, {})).endswith(
            f"/{response_schema_name}"
        ):
            errors.append(f"POST {path} must return {response_schema_name}")
        if not required_error_statuses.issubset(responses):
            missing = sorted(required_error_statuses - set(responses))
            errors.append(f"POST {path} is missing errors: {', '.join(missing)}")
        for status in sorted(required_error_statuses | {"default"}):
            if not responses.get(status, {}).get("$ref", "").endswith("/Error"):
                errors.append(f"POST {path} {status} must use Error response")

    strict_schema_names = {
        *LABEL_MAPPING_REQUEST_FIELDS,
        *LABEL_MAPPING_RESPONSE_RESULTS,
        *LABEL_MAPPING_RESPONSE_RESULTS.values(),
        "LabelMappingCompatibilityEvidenceRequest",
        "LabelMappingIdentityItemRequest",
        "LabelMappingRenameItemRequest",
        "LabelMappingReplaceItemRequest",
        "LabelMappingMergeItemRequest",
        "LabelMappingRetireItemRequest",
        "LabelMappingSplitRecomputeItemRequest",
        "LabelMappingScope",
        "LabelMappingLabelVersionRef",
        "LabelMappingCoverageResult",
        "LabelMappingCompatibilityEvidenceResult",
        "LabelMappingCompiledTargetResult",
        "LabelMappingCompiledItemResult",
        "LabelMappingEdgeCanonicalManifest",
        "LabelMappingBundleSourceResult",
        "LabelMappingBundleMemberResult",
        "LabelMappingBundleRelationStepResult",
        "LabelMappingBundlePathResult",
        "LabelMappingBundleCanonicalManifest",
    }
    for schema_name in sorted(strict_schema_names):
        if schemas.get(schema_name, {}).get("additionalProperties") is not False:
            errors.append(f"{schema_name} must set additionalProperties=false")

    for schema_name, expected_fields in LABEL_MAPPING_REQUEST_FIELDS.items():
        schema = schemas.get(schema_name, {})
        if set(schema.get("required", [])) != expected_fields:
            errors.append(f"{schema_name} required fields drifted")
        if set(schema.get("properties", {})) != expected_fields:
            errors.append(f"{schema_name} exposes server-generated fields")

    item_union = schemas.get("LabelMappingItemRequest", {})
    expected_item_refs = {
        f"#/components/schemas/LabelMapping{relation.title().replace('-', '')}ItemRequest"
        for relation in LABEL_MAPPING_RELATIONS
    }
    actual_item_refs = {
        branch.get("$ref", "")
        for branch in item_union.get("oneOf", [])
        if isinstance(branch, dict)
    }
    discriminator = item_union.get("discriminator", {})
    if actual_item_refs != expected_item_refs:
        errors.append("LabelMappingItemRequest oneOf branches drifted")
    if discriminator.get("propertyName") != "relation":
        errors.append("LabelMappingItemRequest discriminator must be relation")
    if set(discriminator.get("mapping", {})) != LABEL_MAPPING_RELATIONS:
        errors.append("LabelMappingItemRequest discriminator mapping drifted")
    create_item_ref = (
        schemas.get("LabelMappingCreateRequest", {})
        .get("properties", {})
        .get("items", {})
        .get("items", {})
        .get("$ref", "")
    )
    if not create_item_ref.endswith("/LabelMappingItemRequest"):
        errors.append(
            "LabelMappingCreateRequest.items must use LabelMappingItemRequest"
        )

    for response_name, result_name in LABEL_MAPPING_RESPONSE_RESULTS.items():
        response_schema = schemas.get(response_name, {})
        if set(response_schema.get("required", [])) != {"data", "meta"}:
            errors.append(f"{response_name} must require data/meta")
        if set(response_schema.get("properties", {})) != {"data", "meta"}:
            errors.append(f"{response_name} envelope fields drifted")
        data_ref = response_schema.get("properties", {}).get("data", {}).get("$ref", "")
        meta_ref = response_schema.get("properties", {}).get("meta", {}).get("$ref", "")
        if not data_ref.endswith(f"/{result_name}"):
            errors.append(f"{response_name}.data must use {result_name}")
        if not meta_ref.endswith("/ApiMeta"):
            errors.append(f"{response_name}.meta must use ApiMeta")

    fully_required_results = {
        *LABEL_MAPPING_RESPONSE_RESULTS.values(),
        "LabelMappingCoverageResult",
        "LabelMappingCompatibilityEvidenceResult",
        "LabelMappingCompiledTargetResult",
        "LabelMappingCompiledItemResult",
        "LabelMappingEdgeCanonicalManifest",
        "LabelMappingBundleSourceResult",
        "LabelMappingBundleMemberResult",
        "LabelMappingBundlePathResult",
        "LabelMappingBundleCanonicalManifest",
    }
    for schema_name in sorted(fully_required_results):
        schema = schemas.get(schema_name, {})
        if set(schema.get("required", [])) != set(schema.get("properties", {})):
            errors.append(f"{schema_name} must require every response field")

    edge_manifest_ref = (
        schemas.get("LabelMappingDryRunResult", {})
        .get("properties", {})
        .get("canonical_manifest", {})
        .get("$ref", "")
    )
    bundle_manifest = schemas.get("LabelMappingBundleCanonicalManifest", {})
    typed_bundle_refs = {
        "sources": "LabelMappingBundleSourceResult",
        "members": "LabelMappingBundleMemberResult",
        "paths": "LabelMappingBundlePathResult",
    }
    if not edge_manifest_ref.endswith("/LabelMappingEdgeCanonicalManifest"):
        errors.append("LabelMappingDryRunResult.canonical_manifest is not typed")
    for field, schema_name in typed_bundle_refs.items():
        ref = (
            bundle_manifest.get("properties", {})
            .get(field, {})
            .get("items", {})
            .get("$ref", "")
        )
        if not ref.endswith(f"/{schema_name}"):
            errors.append(f"LabelMappingBundleCanonicalManifest.{field} is not typed")

    for path in (
        "/label-mapping-versions/{id}/approve",
        "/label-mapping-bundles/publish",
    ):
        description = paths.get(path, {}).get("post", {}).get("description", "")
        missing_tokens = {
            token
            for token in {"自然人", "project_admin", "系统", "不得代签"}
            if token not in description
        }
        if missing_tokens:
            errors.append(f"POST {path} natural-person guard drifted")
    bundle_description = schemas.get("LabelMappingBundlePublishRequest", {}).get(
        "description", ""
    )
    missing_dynamic_tokens = {
        token
        for token in {"latest", "head", "current"}
        if token not in bundle_description
    }
    if missing_dynamic_tokens:
        errors.append("LabelMappingBundlePublishRequest dynamic-head guard drifted")

    if errors:
        fail("label mapping HTTP contract drifted", errors)
    ok(
        "label mapping dry-run/create/validate/approve/publish HTTP contract is strict and typed"
    )


def validate_label_fact_set_http_contract(openapi: dict[str, Any]) -> None:
    paths = openapi.get("paths", {})
    schemas = openapi.get("components", {}).get("schemas", {})
    errors: list[str] = []
    required_error_statuses = {"400", "401", "403", "404", "409", "422", "429", "503"}
    base_parameter_names = {
        "XTenantId",
        "XProjectId",
        "XRequestId",
        "IdempotencyKey",
    }

    def schema_ref(value: dict[str, Any]) -> str:
        return (
            value.get("content", {})
            .get("application/json", {})
            .get("schema", {})
            .get("$ref", "")
        )

    for path, (
        request_schema_name,
        response_schema_name,
        success_status,
        operation_id,
    ) in LABEL_FACT_SET_HTTP_CONTRACT.items():
        operation = paths.get(path, {}).get("post", {})
        if not operation:
            errors.append(f"POST {path} is missing")
            continue
        if set(operation.get("tags", [])) != {"label-fact-sets"}:
            errors.append(f"POST {path} must use only the label-fact-sets tag")
        if operation.get("operationId") != operation_id:
            errors.append(f"POST {path} operationId drifted")

        expected_parameter_names = set(base_parameter_names)
        if "{id}" in path:
            expected_parameter_names.add("Id")
        actual_parameter_names = {
            parameter.get("$ref", "").rsplit("/", 1)[-1]
            for parameter in operation.get("parameters", [])
            if isinstance(parameter, dict) and "$ref" in parameter
        }
        if actual_parameter_names != expected_parameter_names:
            errors.append(f"POST {path} context/idempotency parameters drifted")

        request_body = operation.get("requestBody", {})
        if request_body.get("required") is not True:
            errors.append(f"POST {path} requestBody must be required")
        if not schema_ref(request_body).endswith(f"/{request_schema_name}"):
            errors.append(f"POST {path} must use {request_schema_name}")

        responses = operation.get("responses", {})
        success_responses = {
            status
            for status in responses
            if status.isdigit() and status.startswith("2")
        }
        if success_responses != {success_status}:
            errors.append(f"POST {path} success status drifted")
        if not schema_ref(responses.get(success_status, {})).endswith(
            f"/{response_schema_name}"
        ):
            errors.append(f"POST {path} must return {response_schema_name}")
        if not required_error_statuses.issubset(responses):
            missing = sorted(required_error_statuses - set(responses))
            errors.append(f"POST {path} is missing errors: {', '.join(missing)}")
        for status in sorted(required_error_statuses | {"default"}):
            if not responses.get(status, {}).get("$ref", "").endswith("/Error"):
                errors.append(f"POST {path} {status} must use Error response")

    strict_schema_names = {
        *LABEL_FACT_SET_REQUEST_FIELDS,
        *LABEL_FACT_SET_RESPONSE_RESULTS,
        *LABEL_FACT_SET_RESPONSE_RESULTS.values(),
    }
    for schema_name in sorted(strict_schema_names):
        if schemas.get(schema_name, {}).get("additionalProperties") is not False:
            errors.append(f"{schema_name} must set additionalProperties=false")

    for schema_name, field_contract in LABEL_FACT_SET_REQUEST_FIELDS.items():
        schema = schemas.get(schema_name, {})
        if set(schema.get("required", [])) != field_contract["required"]:
            errors.append(f"{schema_name} required fields drifted")
        if set(schema.get("properties", {})) != field_contract["properties"]:
            errors.append(
                f"{schema_name} exposes unexpected or server-generated fields"
            )

    for envelope_name, result_name in LABEL_FACT_SET_RESPONSE_RESULTS.items():
        response_schema = schemas.get(envelope_name, {})
        if set(response_schema.get("required", [])) != {"data", "meta"}:
            errors.append(f"{envelope_name} must require data/meta")
        if set(response_schema.get("properties", {})) != {"data", "meta"}:
            errors.append(f"{envelope_name} envelope fields drifted")
        data_ref = response_schema.get("properties", {}).get("data", {}).get("$ref", "")
        meta_ref = response_schema.get("properties", {}).get("meta", {}).get("$ref", "")
        if not data_ref.endswith(f"/{result_name}"):
            errors.append(f"{envelope_name}.data must use {result_name}")
        if not meta_ref.endswith("/ApiMeta"):
            errors.append(f"{envelope_name}.meta must use ApiMeta")

    for result_name in LABEL_FACT_SET_RESPONSE_RESULTS.values():
        result_schema = schemas.get(result_name, {})
        if set(result_schema.get("required", [])) != set(
            result_schema.get("properties", {})
        ):
            errors.append(f"{result_name} must require every emitted field")

    create_schema = schemas.get("LabelFactSetCreateRequest", {})
    create_properties = create_schema.get("properties", {})
    if create_properties.get("fact_as_of", {}).get("format") != "date-time":
        errors.append("LabelFactSetCreateRequest.fact_as_of must be date-time")
    if create_properties.get("row_count", {}).get("minimum") != 0:
        errors.append("LabelFactSetCreateRequest.row_count must be non-negative")
    for field in (
        "partition_manifest_sha256",
        "source_manifest_sha256",
        "result_manifest_sha256",
    ):
        if create_properties.get(field, {}).get("pattern") != "^[0-9a-f]{64}$":
            errors.append(
                f"LabelFactSetCreateRequest.{field} must be lowercase SHA-256"
            )

    promotion_action = (
        schemas.get("LabelFactSetPublishPromoteRequest", {})
        .get("properties", {})
        .get("action", {})
    )
    if set(promotion_action.get("enum", [])) != {"bootstrap", "promote"}:
        errors.append("LabelFactSet promotions must exclude rollback")
    rollback_action = (
        schemas.get("LabelFactSetRollbackRequest", {})
        .get("properties", {})
        .get("action", {})
    )
    if (
        rollback_action.get("enum") != ["rollback"]
        or rollback_action.get("default") != "rollback"
    ):
        errors.append("LabelFactSet rollback action must be server-path fixed")

    for path in (
        "/label-fact-sets/{id}/approvals",
        "/label-fact-sets/{id}/promotions",
        "/label-fact-sets/{id}/rollbacks",
    ):
        description = paths.get(path, {}).get("post", {}).get("description", "")
        missing_tokens = {
            token
            for token in {"自然人", "project_admin", "系统", "不得代签"}
            if token not in description
        }
        if missing_tokens:
            errors.append(f"POST {path} natural-person guard drifted")

    promotion_description = (
        paths.get("/label-fact-sets/{id}/promotions", {})
        .get("post", {})
        .get("description", "")
    )
    rollback_description = (
        paths.get("/label-fact-sets/{id}/rollbacks", {})
        .get("post", {})
        .get("description", "")
    )
    if not {"CAS", "ledger", "generation"}.issubset(
        set(promotion_description.replace("，", " ").replace("。", " ").split())
    ):
        for token in {"CAS", "ledger", "generation"}:
            if token not in promotion_description:
                errors.append(f"FactSet promotion description is missing {token}")
    for token in {"CAS", "ledger", "previous", "不删除"}:
        if token not in rollback_description:
            errors.append(f"FactSet rollback description is missing {token}")

    if errors:
        fail("label FactSet HTTP contract drifted", errors)
    ok(
        "label FactSet candidate/validate/approve/publish/promote/rollback HTTP contract is strict and typed"
    )


def validate_manual_label_http_contract(openapi: dict[str, Any]) -> None:
    paths = openapi.get("paths", {})
    schemas = openapi.get("components", {}).get("schemas", {})
    errors: list[str] = []
    required_error_statuses = {"400", "401", "403", "404", "409", "422", "429", "503"}
    expected_parameters = {
        "XTenantId",
        "XProjectId",
        "XRequestId",
        "IdempotencyKey",
        "Id",
        "AnnotationId",
    }

    def schema_ref(value: dict[str, Any]) -> str:
        return (
            value.get("content", {})
            .get("application/json", {})
            .get("schema", {})
            .get("$ref", "")
        )

    def variant_refs(value: dict[str, Any]) -> set[str]:
        schema = (
            value.get("content", {}).get("application/json", {}).get("schema", value)
        )
        variants = schema.get("anyOf", schema.get("oneOf", []))
        return {
            variant.get("$ref", "").rsplit("/", 1)[-1]
            for variant in variants
            if isinstance(variant, dict) and variant.get("$ref")
        }

    create_operation = paths.get("/audio-sessions/{id}/annotations", {}).get("post", {})
    create_request = create_operation.get("requestBody", {})
    if "ManualLabelDraftCreateRequest" not in variant_refs(create_request):
        errors.append(
            "POST /audio-sessions/{id}/annotations must include strict ManualLabelDraftCreateRequest"
        )
    if "ManualLabelDraftMutationEnvelope" not in variant_refs(
        create_operation.get("responses", {}).get("201", {})
    ):
        errors.append("manual label draft create response is not typed")

    for path, (
        request_schema_name,
        response_contract,
        operation_id,
    ) in MANUAL_LABEL_HTTP_CONTRACT.items():
        operation = paths.get(path, {}).get("post", {})
        if not operation:
            errors.append(f"POST {path} is missing")
            continue
        if set(operation.get("tags", [])) != {"audio-sessions"}:
            errors.append(f"POST {path} must use only the audio-sessions tag")
        if operation.get("operationId") != operation_id:
            errors.append(f"POST {path} operationId drifted")
        parameter_names = {
            parameter.get("$ref", "").rsplit("/", 1)[-1]
            for parameter in operation.get("parameters", [])
            if isinstance(parameter, dict) and parameter.get("$ref")
        }
        if parameter_names != expected_parameters:
            errors.append(f"POST {path} context/idempotency/path parameters drifted")
        request_body = operation.get("requestBody", {})
        if request_body.get("required") is not True:
            errors.append(f"POST {path} requestBody must be required")
        if not schema_ref(request_body).endswith(f"/{request_schema_name}"):
            errors.append(f"POST {path} must use {request_schema_name}")

        responses = operation.get("responses", {})
        success_responses = {
            status
            for status in responses
            if status.isdigit() and status.startswith("2")
        }
        if success_responses != set(response_contract):
            errors.append(f"POST {path} success statuses drifted")
        for status, response_schema_name in response_contract.items():
            if not schema_ref(responses.get(status, {})).endswith(
                f"/{response_schema_name}"
            ):
                errors.append(
                    f"POST {path} {status} must return {response_schema_name}"
                )
        if not required_error_statuses.issubset(responses):
            errors.append(f"POST {path} is missing governed error responses")
        for status in sorted(required_error_statuses | {"default"}):
            if not responses.get(status, {}).get("$ref", "").endswith("/Error"):
                errors.append(f"POST {path} {status} must use Error response")

    strict_schemas = {
        "ManualLabelEvidenceRef",
        "ManualLabelDraftMutationResponse",
        "ManualLabelDraftSubmissionResponse",
        "ManualLabelMappingPathResult",
        "ManualLabelDraftRebasePreviewDocument",
        "ManualLabelDraftRebasePreviewResponse",
        "ManualLabelDraftRebaseConfirmResponse",
        *MANUAL_LABEL_REQUEST_FIELDS,
        *MANUAL_LABEL_RESPONSE_RESULTS,
    }
    for schema_name in sorted(strict_schemas):
        if schemas.get(schema_name, {}).get("additionalProperties") is not False:
            errors.append(f"{schema_name} must set additionalProperties=false")

    for schema_name, field_contract in MANUAL_LABEL_REQUEST_FIELDS.items():
        schema = schemas.get(schema_name, {})
        if set(schema.get("required", [])) != field_contract["required"]:
            errors.append(f"{schema_name} required fields drifted")
        if set(schema.get("properties", {})) != field_contract["properties"]:
            errors.append(f"{schema_name} exposes server-generated fields")

    rebase_variants = {
        item.get("$ref", "").rsplit("/", 1)[-1]
        for item in schemas.get("ManualLabelDraftRebaseRequest", {}).get("oneOf", [])
    }
    if rebase_variants != {
        "ManualLabelDraftRebasePreviewRequest",
        "ManualLabelDraftRebaseConfirmRequest",
    }:
        errors.append("ManualLabelDraftRebaseRequest variants drifted")
    if (
        schemas.get("ManualLabelDraftRebaseRequest", {})
        .get("discriminator", {})
        .get("propertyName")
        != "action"
    ):
        errors.append("ManualLabelDraftRebaseRequest must discriminate on action")

    for envelope_name, result_name in MANUAL_LABEL_RESPONSE_RESULTS.items():
        envelope_schema = schemas.get(envelope_name, {})
        if set(envelope_schema.get("required", [])) != {"data", "meta"} or set(
            envelope_schema.get("properties", {})
        ) != {"data", "meta"}:
            errors.append(f"{envelope_name} envelope fields drifted")
        data_ref = envelope_schema.get("properties", {}).get("data", {}).get("$ref", "")
        meta_ref = envelope_schema.get("properties", {}).get("meta", {}).get("$ref", "")
        if not data_ref.endswith(f"/{result_name}"):
            errors.append(f"{envelope_name}.data must use {result_name}")
        if not meta_ref.endswith("/ApiMeta"):
            errors.append(f"{envelope_name}.meta must use ApiMeta")

    fully_required_results = {
        "ManualLabelDraftMutationResponse",
        "ManualLabelDraftSubmissionResponse",
        "ManualLabelMappingPathResult",
        "ManualLabelDraftRebasePreviewDocument",
        "ManualLabelDraftRebasePreviewResponse",
        "ManualLabelDraftRebaseConfirmResponse",
    }
    for schema_name in sorted(fully_required_results):
        schema = schemas.get(schema_name, {})
        if set(schema.get("required", [])) != set(schema.get("properties", {})):
            errors.append(f"{schema_name} must require every emitted field")

    create_properties = schemas.get("ManualLabelDraftCreateRequest", {}).get(
        "properties", {}
    )
    if create_properties.get("annotation_kind", {}).get("enum") != ["label-fact-draft"]:
        errors.append("ManualLabelDraftCreateRequest discriminator drifted")
    if create_properties.get("occurred_at", {}).get("format") != "date-time":
        errors.append("ManualLabelDraftCreateRequest.occurred_at must be date-time")
    forbidden_request_fields = {
        "draft_sha256",
        "fact_id",
        "decision_id",
        "audit_id",
        "outbox_event_id",
        "trace_id",
        "status",
    }
    for schema_name in MANUAL_LABEL_REQUEST_FIELDS:
        exposed = forbidden_request_fields & set(
            schemas.get(schema_name, {}).get("properties", {})
        )
        if exposed:
            errors.append(f"{schema_name} exposes server fields: {sorted(exposed)}")

    submit_confirmation = (
        schemas.get("ManualLabelDraftSubmitRequest", {})
        .get("properties", {})
        .get("confirmation", {})
        .get("enum")
    )
    confirm_confirmation = (
        schemas.get("ManualLabelDraftRebaseConfirmRequest", {})
        .get("properties", {})
        .get("confirmation", {})
        .get("enum")
    )
    if submit_confirmation != ["submit-frozen-manual-label"]:
        errors.append("manual label submission confirmation drifted")
    if confirm_confirmation != ["confirm-reviewed-manual-label-rebase"]:
        errors.append("manual label rebase confirmation drifted")

    submission_description = (
        paths.get("/audio-sessions/{id}/annotations/{annotation_id}/submissions", {})
        .get("post", {})
        .get("description", "")
    )
    rebase_description = (
        paths.get("/audio-sessions/{id}/annotations/{annotation_id}/rebases", {})
        .get("post", {})
        .get("description", "")
    )
    for token in {"STALE_LABEL_VERSION", "禁止静默改标", "rebase", "LabelFact"}:
        if token not in submission_description:
            errors.append(f"manual label submission description is missing {token}")
    for token in {"preview_sha256", "新 draft", "旧 draft", "二次确认", "value_type"}:
        if token not in rebase_description:
            errors.append(f"manual label rebase description is missing {token}")

    if errors:
        fail("manual label draft HTTP contract drifted", errors)
    ok(
        "manual label draft/create/submit/stale/rebase HTTP contract is strict and typed"
    )


def validate_label_lifecycle_http_contract(openapi: dict[str, Any]) -> None:
    paths = openapi.get("paths", {})
    schemas = openapi.get("components", {}).get("schemas", {})
    errors: list[str] = []

    for path, (
        request_schema_name,
        response_schema_name,
    ) in LABEL_LIFECYCLE_HTTP_CONTRACT.items():
        operation = paths.get(path, {}).get("post", {})
        if not operation:
            errors.append(f"POST {path} is missing")
            continue
        parameter_refs = {
            parameter.get("$ref", "")
            for parameter in operation.get("parameters", [])
            if isinstance(parameter, dict)
        }
        if not any(ref.endswith("/IdempotencyKey") for ref in parameter_refs):
            errors.append(f"POST {path} is missing Idempotency-Key")
        request_ref = (
            operation.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
            .get("$ref", "")
        )
        if not request_ref.endswith(f"/{request_schema_name}"):
            errors.append(f"POST {path} must use {request_schema_name}")
        response_ref = (
            operation.get("responses", {})
            .get("200", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
            .get("$ref", "")
        )
        if not response_ref.endswith(f"/{response_schema_name}"):
            errors.append(f"POST {path} must return {response_schema_name}")

    strict_schema_names = {
        "LabelVersionDeprecationPreflightRequest",
        "LabelVersionTransitionRequest",
        "LabelVersionEnvironmentReference",
        "LabelVersionInFlightRunReference",
        "LabelVersionLifecycleBlocker",
        "LabelVersionDownstreamImpact",
        "LabelVersionDeprecationPreflight",
        "LabelVersionDeprecationPreflightResponse",
        "LabelVersionTransition",
        "LabelVersionTransitionResponse",
    }
    for schema_name in sorted(strict_schema_names):
        if schemas.get(schema_name, {}).get("additionalProperties") is not False:
            errors.append(f"{schema_name} must set additionalProperties=false")

    preflight_request = schemas.get("LabelVersionDeprecationPreflightRequest", {})
    transition_request = schemas.get("LabelVersionTransitionRequest", {})
    required_preflight_request = {"expected_resource_version", "reason"}
    required_transition_request = {
        "action",
        "expected_resource_version",
        "reason",
    }
    if set(preflight_request.get("required", [])) != required_preflight_request:
        errors.append("LabelVersionDeprecationPreflightRequest required fields drifted")
    if set(transition_request.get("required", [])) != required_transition_request:
        errors.append("LabelVersionTransitionRequest required fields drifted")

    permitted_preflight_request_fields = {
        "expected_resource_version",
        "replacement_label_version_id",
        "mapping_bundle_id",
        "reason",
        "impact_cursor",
        "impact_limit",
    }
    permitted_transition_request_fields = {
        "action",
        *permitted_preflight_request_fields,
    }
    if (
        set(preflight_request.get("properties", {}))
        != permitted_preflight_request_fields
    ):
        errors.append("LabelVersionDeprecationPreflightRequest exposes server fields")
    if (
        set(transition_request.get("properties", {}))
        != permitted_transition_request_fields
    ):
        errors.append("LabelVersionTransitionRequest exposes server fields")

    preflight_response = schemas.get("LabelVersionDeprecationPreflight", {})
    transition_response = schemas.get("LabelVersionTransition", {})
    if set(preflight_response.get("required", [])) != (
        LABEL_LIFECYCLE_PREFLIGHT_RESPONSE_FIELDS
    ):
        errors.append("LabelVersionDeprecationPreflight required fields drifted")
    if set(preflight_response.get("properties", {})) != (
        LABEL_LIFECYCLE_PREFLIGHT_RESPONSE_FIELDS
    ):
        errors.append("LabelVersionDeprecationPreflight properties drifted")
    if set(transition_response.get("required", [])) != (
        LABEL_LIFECYCLE_TRANSITION_RESPONSE_FIELDS
    ):
        errors.append("LabelVersionTransition required fields drifted")
    if set(transition_response.get("properties", {})) != (
        LABEL_LIFECYCLE_TRANSITION_RESPONSE_FIELDS
    ):
        errors.append("LabelVersionTransition properties drifted")

    blocker_types = set(
        schemas.get("LabelVersionLifecycleBlocker", {})
        .get("properties", {})
        .get("reference_type", {})
        .get("enum", [])
    )
    if blocker_types != {
        "active-head",
        "draining-deployment",
        "in-flight-run",
        "downstream-impact",
        "impact-scan",
    }:
        errors.append("LabelVersionLifecycleBlocker.reference_type drifted")
    in_flight_fields = {
        "run_id",
        "run_status",
        "environment",
        "head_generation",
        "active_deployment_id",
        "active_bundle_sha256",
    }
    in_flight_schema = schemas.get("LabelVersionInFlightRunReference", {})
    if (
        set(in_flight_schema.get("required", [])) != in_flight_fields
        or set(in_flight_schema.get("properties", {})) != in_flight_fields
    ):
        errors.append("LabelVersionInFlightRunReference fields drifted")

    if errors:
        fail("label lifecycle HTTP contract drifted", errors)
    ok("label lifecycle preflight/transition HTTP contract is strict and typed")


def validate_label_lifecycle_statistics_contract(openapi: dict[str, Any]) -> None:
    if not LABEL_LIFECYCLE_ADR_PATH.exists():
        fail("label lifecycle/statistics ADR is missing")

    schemas = openapi.get("components", {}).get("schemas", {})
    expected_enums = {
        "LabelVersionStatus": LABEL_VERSION_STATES,
        "LabelTaxonomyMode": LABEL_TAXONOMY_MODES,
        "LabelMappingRelation": LABEL_MAPPING_RELATIONS,
        "LabelMappingCompatibility": LABEL_MAPPING_COMPATIBILITIES,
        "LabelComparabilityStatus": LABEL_COMPARABILITY_STATUSES,
        "LabelVersionApplicability": LABEL_VERSION_APPLICABILITY,
    }
    enum_errors: dict[str, dict[str, list[str]]] = {}
    for schema_name, expected in expected_enums.items():
        actual = set(schemas.get(schema_name, {}).get("enum", []))
        if actual != expected:
            enum_errors[schema_name] = {
                "missing": sorted(expected - actual),
                "unexpected": sorted(actual - expected),
            }
    if enum_errors:
        fail("label lifecycle/statistics enums drifted", enum_errors)

    label_scope = schemas.get("LabelMetricScope", {})
    missing_scope_fields = sorted(
        LABEL_METRIC_SCOPE_REQUIRED_FIELDS
        - set(label_scope.get("properties", {}))
        - set(label_scope.get("required", []))
    )
    non_required_scope_fields = sorted(
        LABEL_METRIC_SCOPE_REQUIRED_FIELDS - set(label_scope.get("required", []))
    )
    if missing_scope_fields or non_required_scope_fields:
        fail(
            "LabelMetricScope must freeze every reproducibility field",
            {
                "missing_properties": missing_scope_fields,
                "not_required": non_required_scope_fields,
            },
        )
    taxonomy_mode_ref = (
        label_scope.get("properties", {}).get("taxonomy_mode", {}).get("$ref", "")
    )
    if not taxonomy_mode_ref.endswith("/LabelTaxonomyMode"):
        fail("LabelMetricScope.taxonomy_mode must use LabelTaxonomyMode")

    create_request = schemas.get("CreateInsightMetricRunRequest", {})
    label_scope_ref = (
        create_request.get("properties", {}).get("label_scope", {}).get("$ref", "")
    )
    if not label_scope_ref.endswith("/LabelMetricScope"):
        fail("CreateInsightMetricRunRequest must accept typed label_scope")
    request_description = create_request.get("description", "")
    required_fail_closed_tokens = {
        "label_version_applicability=none",
        "label_scope",
        "fail closed",
    }
    missing_request_tokens = sorted(
        token
        for token in required_fail_closed_tokens
        if token not in request_description
    )
    if missing_request_tokens:
        fail(
            "metric-run request does not document label scope fail-closed behavior",
            missing_request_tokens,
        )

    metric_scope = schemas.get("InsightMetricScope", {})
    if "label_version_applicability" not in set(metric_scope.get("required", [])):
        fail("InsightMetricScope must freeze label_version_applicability")
    if "label_scope" not in set(metric_scope.get("required", [])):
        fail("InsightMetricScope must freeze label_scope, including explicit null")
    applicability_ref = (
        metric_scope.get("properties", {})
        .get("label_version_applicability", {})
        .get("$ref", "")
    )
    if not applicability_ref.endswith("/LabelVersionApplicability"):
        fail("InsightMetricScope.label_version_applicability must use the frozen enum")

    metric_result = schemas.get("MetricResult", {})
    required_result_fields = {
        "label_version_applicability",
        "label_scope",
        "comparability_status",
        "comparability_reason_codes",
        "scope_sha256",
        "source_manifest_sha256",
        "content_sha256",
    }
    missing_result_fields = sorted(
        required_result_fields - set(metric_result.get("required", []))
    )
    if missing_result_fields:
        fail(
            "MetricResult is missing immutable label-scope fields",
            missing_result_fields,
        )
    comparability_ref = (
        metric_result.get("properties", {})
        .get("comparability_status", {})
        .get("$ref", "")
    )
    if not comparability_ref.endswith("/LabelComparabilityStatus"):
        fail("MetricResult.comparability_status must use LabelComparabilityStatus")

    metric_parameters = {
        parameter.get("name")
        for parameter in openapi.get("paths", {})
        .get("/insights/metrics", {})
        .get("get", {})
        .get("parameters", [])
        if isinstance(parameter, dict) and parameter.get("in") == "query"
    }
    required_metric_parameters = {
        "label_version_applicability",
        "taxonomy_mode",
        "source_label_version_id",
        "target_label_version_id",
        "mapping_bundle_id",
        "fact_set_generation",
        "fact_as_of",
    }
    missing_metric_parameters = sorted(required_metric_parameters - metric_parameters)
    if missing_metric_parameters:
        fail(
            "insight metric query cannot freeze label statistics scope",
            missing_metric_parameters,
        )

    report = schemas.get("InsightReport", {})
    required_report_fields = {"metric_result_ids", "metric_scope_sha256"}
    missing_report_fields = sorted(
        required_report_fields - set(report.get("required", []))
    )
    if missing_report_fields:
        fail(
            "InsightReport must freeze metric result IDs and scope hash",
            missing_report_fields,
        )

    for filename, required_tokens in LABEL_LIFECYCLE_DOC_REQUIREMENTS.items():
        document = (ROOT / filename).read_text(encoding="utf-8")
        missing_tokens = sorted(
            token for token in required_tokens if token not in document
        )
        if missing_tokens:
            fail(
                f"{filename} is missing label lifecycle/statistics contract tokens",
                missing_tokens,
            )

    adr = LABEL_LIFECYCLE_ADR_PATH.read_text(encoding="utf-8")
    adr_invariants = {
        "历史事实不可改写",
        "mapping_bundle",
        "occurred_at",
        "recorded_at",
        "fact_as_of",
        "coverage-gap",
        "structural-break",
        "split-recompute",
        "ReleaseBundleHead",
        "FactSet",
        "LABEL_MAPPING_SEMANTIC_HASH_CHANGED",
        "LABEL_MAPPING_COMPATIBILITY_EVIDENCE_REQUIRED",
        "LABEL_MAPPING_REDUCER_REQUIRED",
        "LABEL_MAPPING_RECOMPUTE_REQUIRED",
        "LABEL_MAPPING_RETIRE_TARGET_FORBIDDEN",
        "LABEL_VERSION_ACTIVE_ENVIRONMENT_REFERENCE",
        "INSIGHT_MAPPING_BUNDLE_REQUIRED",
    }
    missing_adr_invariants = sorted(
        token for token in adr_invariants if token not in adr
    )
    if missing_adr_invariants:
        fail("label lifecycle/statistics ADR is incomplete", missing_adr_invariants)

    ok(
        "label lifecycle, mapping bundle, immutable fact, and statistics contracts are frozen"
    )


def validate_runtime_openapi_drift(openapi: dict[str, Any]) -> None:
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    try:
        from app.main import app
    except Exception as exc:  # pragma: no cover - developer environment guard
        fail(
            "runtime OpenAPI cannot be imported from FastAPI app", {"error": repr(exc)}
        )

    documented = {
        (path, method.lower())
        for path, spec in openapi.get("paths", {}).items()
        for method in spec
        if method.lower() in HTTP_METHODS
    }
    runtime_openapi = app.openapi()
    runtime = {
        (path.removeprefix(API_PREFIX), method.lower())
        for path, spec in runtime_openapi.get("paths", {}).items()
        for method in spec
        if path.startswith(API_PREFIX) and method.lower() in HTTP_METHODS
    }
    missing_from_doc = sorted(runtime - documented)
    stale_in_doc = sorted(documented - runtime)
    if missing_from_doc or stale_in_doc:
        fail(
            "OpenAPI document drifted from FastAPI runtime routes",
            {
                "runtime_not_documented": [
                    {"method": method.upper(), "path": path}
                    for path, method in missing_from_doc
                ],
                "documented_not_runtime": [
                    {"method": method.upper(), "path": path}
                    for path, method in stale_in_doc
                ],
            },
        )

    def documented_query_parameter_names(operation: dict[str, Any]) -> set[str]:
        names: set[str] = set()
        reusable = openapi.get("components", {}).get("parameters", {})
        for raw_parameter in operation.get("parameters", []):
            parameter = raw_parameter
            if isinstance(raw_parameter, dict) and raw_parameter.get("$ref"):
                parameter = reusable.get(
                    str(raw_parameter["$ref"]).rsplit("/", 1)[-1],
                    {},
                )
            if isinstance(parameter, dict) and parameter.get("in") == "query":
                name = parameter.get("name")
                if isinstance(name, str):
                    names.add(name)
        return names

    documented_metric_queries = documented_query_parameter_names(
        openapi.get("paths", {}).get("/insights/metrics", {}).get("get", {})
    )
    runtime_metric_queries = {
        str(parameter["name"])
        for parameter in runtime_openapi.get("paths", {})
        .get(f"{API_PREFIX}/insights/metrics", {})
        .get("get", {})
        .get("parameters", [])
        if isinstance(parameter, dict)
        and parameter.get("in") == "query"
        and isinstance(parameter.get("name"), str)
    }
    if documented_metric_queries != runtime_metric_queries:
        fail(
            "insight metric query parameters drifted from FastAPI runtime",
            {
                "documented_not_runtime": sorted(
                    documented_metric_queries - runtime_metric_queries
                ),
                "runtime_not_documented": sorted(
                    runtime_metric_queries - documented_metric_queries
                ),
            },
        )
    ok(f"OpenAPI matches FastAPI runtime operations: {len(runtime)}/{len(documented)}")


def collect_ids(items: list[dict[str, Any]], key: str) -> set[str]:
    return {str(item[key]) for item in items if key in item}


def validate_seed_fixture() -> None:
    with SEED_PATH.open("r", encoding="utf-8") as fh:
        seed = json.load(fh)

    tenant_id = seed["context"]["tenant"]["tenant_id"]
    project_id = seed["context"]["project"]["project_id"]
    if tenant_id != "aurora_auto" or project_id != "sales_qa":
        fail(
            "seed context must preserve stable tenant/project IDs",
            {"tenant_id": tenant_id, "project_id": project_id},
        )

    audio_sessions = collect_ids(
        seed["audio_evidence"]["audio_sessions"], "audio_session_id"
    )
    evidence_packs = collect_ids(
        seed["audio_evidence"]["evidence_packs"], "evidence_pack_id"
    )
    documents = collect_ids(seed["business_events"]["documents"], "document_id")
    event_links = collect_ids(seed["business_events"]["event_links"], "id")
    label_versions = collect_ids(seed["labeling"]["label_versions"], "label_version_id")
    prompt_versions = collect_ids(
        seed["labeling"]["prompt_versions"], "prompt_version_id"
    )
    eval_datasets = collect_ids(seed["evaluation"]["eval_datasets"], "dataset_id")
    task_runs = collect_ids(seed["tasking"]["task_runs"], "task_run_id")
    asset_keys = collect_ids(seed["data_assets"], "asset_key")
    hotword = seed.get("hotword_governance", {})
    hotword_packs = collect_ids(hotword.get("hotword_packs", []), "pack_id")
    hotword_versions = collect_ids(
        hotword.get("hotword_pack_versions", []), "version_id"
    )

    relation_errors: list[str] = []

    for pack in seed["audio_evidence"]["evidence_packs"]:
        if pack["audio_session_id"] not in audio_sessions:
            relation_errors.append(
                f"evidence_pack {pack['evidence_pack_id']} references missing audio_session"
            )

    for link in seed["business_events"]["event_links"]:
        if link["audio_session_id"] not in audio_sessions:
            relation_errors.append(
                f"event_link {link['id']} references missing audio_session"
            )
        if link.get("document_ref") and link["document_ref"] not in documents:
            relation_errors.append(
                f"event_link {link['id']} references missing document"
            )
        for evidence_ref in link.get("evidence_refs", []):
            if evidence_ref.get("evidence_pack_id") not in evidence_packs:
                relation_errors.append(
                    f"event_link {link['id']} references missing evidence_pack"
                )

    for candidate in seed["labeling"]["label_candidates"]:
        if candidate["label_version_id"] not in label_versions:
            relation_errors.append(
                f"label_candidate {candidate['candidate_id']} references missing label_version"
            )
        if candidate["prompt_version"] not in prompt_versions:
            relation_errors.append(
                f"label_candidate {candidate['candidate_id']} references missing prompt_version"
            )
        if candidate["evidence_pack_id"] not in evidence_packs:
            relation_errors.append(
                f"label_candidate {candidate['candidate_id']} references missing evidence_pack"
            )

    for task in seed["review_and_feedback"]["human_review_tasks"]:
        if task["evidence_pack_id"] not in evidence_packs:
            relation_errors.append(
                f"human_review_task {task['id']} references missing evidence_pack"
            )
        if task["asset_key"] not in asset_keys:
            relation_errors.append(
                f"human_review_task {task['id']} references missing asset_key"
            )

    for badcase in seed["review_and_feedback"]["badcases"]:
        if badcase["source_evidence_pack_id"] not in evidence_packs:
            relation_errors.append(
                f"badcase {badcase['badcase_id']} references missing evidence_pack"
            )

    for pack in hotword.get("hotword_packs", []):
        if pack.get("current_version_id") not in hotword_versions:
            relation_errors.append(
                f"hotword_pack {pack.get('pack_id')} references missing current version"
            )

    for version in hotword.get("hotword_pack_versions", []):
        if version.get("pack_id") not in hotword_packs:
            relation_errors.append(
                f"hotword_pack_version {version.get('version_id')} references missing pack"
            )
        baseline_version_id = version.get("baseline_version_id")
        if baseline_version_id and baseline_version_id not in hotword_versions:
            relation_errors.append(
                f"hotword_pack_version {version.get('version_id')} references missing baseline version"
            )

    for item in hotword.get("hotword_version_items", []):
        if item.get("version_id") not in hotword_versions:
            relation_errors.append(
                f"hotword item {item.get('item_id')} references missing version"
            )
        if item.get("source_badcase_id") and item[
            "source_badcase_id"
        ] not in collect_ids(seed["review_and_feedback"]["badcases"], "badcase_id"):
            relation_errors.append(
                f"hotword item {item.get('item_id')} references missing badcase"
            )

    for eval_run in seed["evaluation"]["eval_runs"]:
        if eval_run["dataset_id"] not in eval_datasets:
            relation_errors.append(
                f"eval_run {eval_run['eval_run_id']} references missing dataset"
            )

    for asset in seed["data_assets"]:
        latest_run_id = asset.get("latest_run_id")
        if latest_run_id and latest_run_id not in task_runs:
            relation_errors.append(
                f"data_asset {asset['asset_key']} references missing task_run"
            )
        for upstream in asset.get("upstream", []):
            if upstream not in asset_keys:
                relation_errors.append(
                    f"data_asset {asset['asset_key']} has missing upstream {upstream}"
                )
        for downstream in asset.get("downstream", []):
            if downstream not in asset_keys and not downstream.startswith(
                "auris/insights/"
            ):
                relation_errors.append(
                    f"data_asset {asset['asset_key']} has missing downstream {downstream}"
                )

    for payload in seed["storage_refs"]["qdrant_payload_examples"]:
        missing_fields = sorted(QDRANT_PAYLOAD_REQUIRED_FIELDS - set(payload))
        if missing_fields:
            relation_errors.append(
                f"qdrant payload {payload.get('source_id', '<missing source_id>')} missing required fields {missing_fields}"
            )
        if payload["asset_key"] not in asset_keys:
            relation_errors.append(
                f"qdrant payload references missing asset_key {payload['asset_key']}"
            )
        if payload["source_id"] not in evidence_packs:
            relation_errors.append(
                f"qdrant payload references missing source evidence_pack {payload['source_id']}"
            )
        if payload["evidence_id"] not in evidence_packs:
            relation_errors.append(
                f"qdrant payload references missing evidence_id {payload['evidence_id']}"
            )
        if (
            payload.get("label_version_id")
            and payload["label_version_id"] not in label_versions
        ):
            relation_errors.append(
                f"qdrant payload references missing label_version_id {payload['label_version_id']}"
            )

    if relation_errors:
        fail("seed fixture contains broken references", relation_errors)

    if len(audio_sessions) < 1 or len(evidence_packs) < 3 or len(asset_keys) < 3:
        fail("seed fixture does not meet minimum demo depth")

    if "event_quote_122718" not in event_links:
        fail("seed fixture must include quote event link used by listening/data flows")

    expected_pack = next(
        (
            pack
            for pack in hotword.get("hotword_packs", [])
            if pack.get("name") == "汽车销售热词包"
        ),
        None,
    )
    if not expected_pack:
        fail("seed fixture must include 汽车销售热词包")
    canonical_seed_version_id = "hwpv-auto-sales-v1-8"
    if expected_pack.get("current_version_id") != canonical_seed_version_id:
        fail(
            "汽车销售热词包 seed must preserve canonical version id",
            {
                "expected": canonical_seed_version_id,
                "actual": expected_pack.get("current_version_id"),
            },
        )
    expected_version = next(
        (
            version
            for version in hotword.get("hotword_pack_versions", [])
            if version.get("version_id") == expected_pack.get("current_version_id")
        ),
        None,
    )
    if (
        not expected_version
        or expected_version.get("version") != "v1.8"
        or expected_version.get("status") != "published"
        or expected_version.get("compiled_provider") != "auris-audio-stack"
        or not expected_version.get("provider_artifact_ref")
        or not expected_version.get("content_sha256")
        or not expected_version.get("manifest_storage_object_id")
    ):
        fail("汽车销售热词包 seed must point to published immutable v1.8")
    version_items = [
        item
        for item in hotword.get("hotword_version_items", [])
        if item.get("version_id") == expected_pack.get("current_version_id")
    ]
    if len(version_items) < 3:
        fail("汽车销售热词包 v1.8 must include at least three seed terms")
    invalid_source_types = sorted(
        {
            str(item.get("source_type"))
            for item in version_items
            if item.get("source_type") not in HOTWORD_ITEM_SOURCE_TYPES
        }
    )
    if invalid_source_types:
        fail(
            "hotword seed contains invalid or missing source_type", invalid_source_types
        )
    if any(
        item.get("category") in {"customer_name", "phone", "license_plate", "vin"}
        for item in version_items
    ):
        fail("hotword seed contains forbidden sensitive entity category")

    legacy_task_version = next(
        (
            item
            for item in seed["tasking"]["task_versions"]
            if item.get("task_version_id") == "task_version_v3_2_1"
        ),
        None,
    )
    if (
        not legacy_task_version
        or legacy_task_version.get("hotwords_ref") != "汽车销售热词包 v1.8"
        or legacy_task_version.get("hotword_pack_version_id")
        != expected_pack.get("current_version_id")
        or legacy_task_version.get("hotword_binding_mode") != "legacy-read-through"
    ):
        fail("legacy hotwords_ref seed must read through to the immutable v1.8 version")

    hotword_badcase = next(
        (
            item
            for item in seed["review_and_feedback"]["badcases"]
            if item.get("badcase_id") == "A-4107"
        ),
        None,
    )
    if not hotword_badcase or hotword_badcase.get("capability") != "asr-hotword":
        fail("seed fixture must include canonical ASR hotword badcase A-4107")
    evidence_storage_object_id = hotword_badcase.get("evidence_storage_object_id")
    evidence_storage_object = next(
        (
            item
            for item in hotword.get("storage_objects", [])
            if item.get("storage_object_id") == evidence_storage_object_id
        ),
        None,
    )
    if (
        not evidence_storage_object_id
        or not evidence_storage_object
        or evidence_storage_object.get("source_id") != "A-4107"
        or evidence_storage_object.get("source_type") != "asr_hotword_evidence"
        or evidence_storage_object.get("status") != "verified"
        or hotword_badcase.get("hotword_pack_version_id") != "hwpv-auto-sales-v1-8"
        or hotword_badcase.get("root_trace_id") != "trace_hotword_pack_auto_sales"
        or hotword_badcase.get("evidence_ref")
        != f"storage-object:{evidence_storage_object_id}"
    ):
        fail(
            "A-4107 must bind immutable version, governed evidence StorageObject and root trace"
        )

    ok("seed fixture references are consistent")


def validate_placeholders() -> None:
    hits: list[str] = []
    for path in ROOT.glob(DOC_GLOB):
        text = path.read_text(encoding="utf-8")
        for index, line in enumerate(text.splitlines(), start=1):
            if PLACEHOLDER_PATTERN.search(line):
                hits.append(f"{path.name}:{index}: {line}")
    if hits:
        fail("backend spec still contains placeholders or deprecated paths", hits)
    ok("no placeholder markers or deprecated asset-id paths")


def main() -> None:
    openapi = load_openapi()
    validate_openapi_local_references(openapi)
    validate_openapi_contract(openapi)
    validate_openapi_quality(openapi)
    validate_hotword_contract(openapi)
    validate_label_mapping_http_contract(openapi)
    validate_label_fact_set_http_contract(openapi)
    validate_manual_label_http_contract(openapi)
    validate_label_lifecycle_http_contract(openapi)
    validate_label_lifecycle_statistics_contract(openapi)
    validate_runtime_openapi_drift(openapi)
    validate_seed_fixture()
    validate_placeholders()
    print("[OK] backend spec pack validation complete")


if __name__ == "__main__":
    main()
