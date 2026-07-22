from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from app.api.routers import label_mappings
from app.core.errors import ApiError

CREATE_BODY: dict[str, Any] = {
    "mapping_version": "1.0.0",
    "source_label_version_id": "lv_mapping_http_source",
    "target_label_version_id": "lv_mapping_http_target",
    "expected_source_resource_version": 3,
    "expected_target_resource_version": 5,
    "items": [
        {
            "relation": "identity",
            "source_label_id": "label_quote",
            "target_label_id": "label_quote",
        }
    ],
}

PUBLISH_BODY: dict[str, Any] = {
    "mapping_version_ids": ["lmv_mapping_http_v1"],
    "expected_mapping_resource_versions": {"lmv_mapping_http_v1": 3},
    "source_label_version_ids": ["lv_mapping_http_source"],
    "expected_source_resource_versions": {"lv_mapping_http_source": 3},
    "target_label_version_id": "lv_mapping_http_target",
    "expected_target_resource_version": 5,
}

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
COVERAGE = {
    "active_source_item_count": 1,
    "coverage_gap_source_label_ids": [],
    "disposition_count": 1,
    "exact_count": 1,
    "metric_dependent_count": 0,
    "normalizable_count": 1,
    "recompute_required_source_label_ids": [],
    "structural_break_count": 0,
    "unmapped_source_label_ids": [],
}
COMPILED_ITEM = {
    "allowed_metric_families": ["presence", "distinct-count"],
    "comparability_status": "comparable",
    "compatibility": "exact",
    "compatibility_evidence": None,
    "content_sha256": SHA_B,
    "lineage_key": None,
    "merge_group_sha256": None,
    "metric_grain": None,
    "reducer": None,
    "relation": "identity",
    "requires_recompute": False,
    "source_definition_sha256": SHA_A,
    "source_label_id": "label_quote",
    "source_semantic_sha256": SHA_A,
    "target_semantic_sha256": SHA_A,
    "targets": [
        {
            "content_sha256": SHA_C,
            "definition_sha256": SHA_A,
            "semantic_sha256": SHA_A,
            "target_label_id": "label_quote",
            "target_order": 0,
        }
    ],
}
SCOPE = {"project_id": "sales_qa", "tenant_id": "aurora_auto"}
SOURCE_LABEL_VERSION_REF = {
    "content_sha256": SHA_A,
    "label_version_id": "lv_mapping_http_source",
    "resource_version": 3,
    "taxonomy_id": "taxonomy_sales_qa",
}
TARGET_LABEL_VERSION_REF = {
    "content_sha256": SHA_B,
    "label_version_id": "lv_mapping_http_target",
    "resource_version": 5,
    "taxonomy_id": "taxonomy_sales_qa",
}
EDGE_CANONICAL_MANIFEST = {
    "compiler_version": "label-mapping-compiler/1.0.0",
    "coverage": COVERAGE,
    "items": [COMPILED_ITEM],
    "mapping_version": "1.0.0",
    "metric_registry_version": "label-metric-compatibility/1",
    "schema_version": "auris.label-mapping-edge/1",
    "scope": SCOPE,
    "source_label_version": SOURCE_LABEL_VERSION_REF,
    "target_label_version": TARGET_LABEL_VERSION_REF,
}
COMPILED_EDGE_RESULT = {
    "mapping_version": "1.0.0",
    "compiler_version": "label-mapping-compiler/1.0.0",
    "metric_registry_version": "label-metric-compatibility/1",
    "source_label_version_id": "lv_mapping_http_source",
    "target_label_version_id": "lv_mapping_http_target",
    "source_resource_version": 3,
    "target_resource_version": 5,
    "content_sha256": SHA_D,
    "canonical_manifest_sha256": SHA_D,
    "coverage": COVERAGE,
    "items": [COMPILED_ITEM],
    "canonical_manifest": EDGE_CANONICAL_MANIFEST,
}
BUNDLE_RELATION_STEP = {
    "comparability_status": "comparable",
    "compatibility": "exact",
    "content_sha256": SHA_B,
    "mapping_version_id": "lmv_mapping_http_v1",
    "relation": "identity",
    "source_label_id": "label_quote",
    "source_label_version_id": "lv_mapping_http_source",
    "target_label_ids": ["label_quote"],
    "target_label_version_id": "lv_mapping_http_target",
}
BUNDLE_CANONICAL_MANIFEST = {
    "compiler_version": "label-mapping-bundle-compiler/1.0.0",
    "members": [
        {
            "edge_content_sha256": SHA_D,
            "edge_order": 0,
            "mapping_resource_version": 3,
            "mapping_version_id": "lmv_mapping_http_v1",
            "source_label_version_id": "lv_mapping_http_source",
            "target_label_version_id": "lv_mapping_http_target",
        }
    ],
    "metric_registry_version": "label-metric-compatibility/1",
    "paths": [
        {
            "comparability_status": "comparable",
            "coverage_gap": False,
            "lineage_key": None,
            "mapping_version_ids": ["lmv_mapping_http_v1"],
            "metric_family": "presence",
            "metric_grain": None,
            "path_sha256": SHA_C,
            "reducer": None,
            "relation_path": [BUNDLE_RELATION_STEP],
            "requires_recompute": False,
            "source_label_id": "label_quote",
            "source_label_version_id": "lv_mapping_http_source",
            "target_label_id": "label_quote",
            "target_label_version_id": "lv_mapping_http_target",
        }
    ],
    "schema_version": "auris.label-mapping-bundle/1",
    "scope": SCOPE,
    "source_manifest_sha256": SHA_A,
    "sources": [
        {
            "content_sha256": SHA_D,
            "source_label_version_id": "lv_mapping_http_source",
            "source_order": 0,
            "source_resource_version": 3,
            "version_content_sha256": SHA_A,
        }
    ],
    "target_label_version": TARGET_LABEL_VERSION_REF,
    "taxonomy_id": "taxonomy_sales_qa",
}
SERVICE_RESULTS: dict[str, dict[str, Any]] = {
    "dry_run_label_mapping_edge": {
        **COMPILED_EDGE_RESULT,
        "persisted": False,
    },
    "create_label_mapping_version": {
        **COMPILED_EDGE_RESULT,
        "persisted": True,
        "mapping_version_id": "lmv_mapping_http_v1",
        "status": "draft",
        "resource_version": 1,
        "deduplicated": False,
        "audit_id": 11,
        "outbox_event_id": 12,
        "trace_id": "replaced-by-stub",
    },
    "validate_label_mapping_version": {
        "mapping_version_id": "lmv_mapping_http_v1",
        "mapping_version": "1.0.0",
        "status": "validated",
        "resource_version": 2,
        "content_sha256": SHA_D,
        "compiler_version": "label-mapping-compiler/1.0.0",
        "metric_registry_version": "label-metric-compatibility/1",
        "coverage": COVERAGE,
        "already_validated": False,
        "audit_id": 13,
        "outbox_event_id": 14,
        "trace_id": "replaced-by-stub",
    },
    "approve_label_mapping_version": {
        "mapping_version_id": "lmv_mapping_http_v1",
        "status": "approved",
        "resource_version": 3,
        "content_sha256": SHA_D,
        "approval_id": "lma_mapping_http_v1",
        "approved_by": "u_admin_001",
        "approved_at": "2026-07-18T07:00:00+00:00",
        "deduplicated": False,
        "audit_id": 15,
        "outbox_event_id": 16,
        "trace_id": "replaced-by-stub",
    },
    "publish_label_mapping_bundle": {
        "mapping_bundle_id": "lmb_mapping_http_v1",
        "status": "published",
        "resource_version": 1,
        "source_label_version_ids": ["lv_mapping_http_source"],
        "target_label_version_id": "lv_mapping_http_target",
        "mapping_version_ids": ["lmv_mapping_http_v1"],
        "source_manifest_sha256": SHA_A,
        "canonical_manifest_sha256": SHA_B,
        "compiler_version": "label-mapping-bundle-compiler/1.0.0",
        "metric_registry_version": "label-metric-compatibility/1",
        "member_count": 1,
        "path_count": 1,
        "canonical_manifest": BUNDLE_CANONICAL_MANIFEST,
        "approval_id": "lmba_mapping_http_v1",
        "approved_by": "u_admin_001",
        "deduplicated": False,
        "audit_id": 17,
        "outbox_event_id": 18,
        "trace_id": "replaced-by-stub",
    },
}


def _service_result(service_name: str, trace_id: str) -> dict[str, Any]:
    result = deepcopy(SERVICE_RESULTS[service_name])
    if "trace_id" in result:
        result["trace_id"] = trace_id
    return result


def _headers(auth_headers: dict[str, str], key: str, *, token: str = "dev-token") -> dict[str, str]:
    return {
        **auth_headers,
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }


@pytest.mark.parametrize(
    ("path", "body", "service_name", "expected_status", "request_type", "resource_id"),
    [
        (
            "/api/v1/label-mapping-versions/dry-run",
            CREATE_BODY,
            "dry_run_label_mapping_edge",
            200,
            "LabelMappingCreateRequest",
            None,
        ),
        (
            "/api/v1/label-mapping-versions",
            CREATE_BODY,
            "create_label_mapping_version",
            201,
            "LabelMappingCreateRequest",
            None,
        ),
        (
            "/api/v1/label-mapping-versions/lmv_mapping_http_v1/validate",
            {"expected_resource_version": 1},
            "validate_label_mapping_version",
            200,
            "LabelMappingValidationRequest",
            "lmv_mapping_http_v1",
        ),
        (
            "/api/v1/label-mapping-versions/lmv_mapping_http_v1/approve",
            {"expected_resource_version": 2, "reason": "确认映射证据与覆盖率"},
            "approve_label_mapping_version",
            200,
            "LabelMappingApprovalRequest",
            "lmv_mapping_http_v1",
        ),
        (
            "/api/v1/label-mapping-bundles/publish",
            PUBLISH_BODY,
            "publish_label_mapping_bundle",
            201,
            "LabelMappingBundlePublishRequest",
            None,
        ),
    ],
)
def test_mapping_http_endpoints_delegate_once_and_return_a_uniform_envelope(
    client,
    auth_headers,
    monkeypatch,
    path: str,
    body: dict[str, Any],
    service_name: str,
    expected_status: int,
    request_type: str,
    resource_id: str | None,
) -> None:
    calls: list[dict[str, Any]] = []

    def service_stub(session, ctx, *args):
        request = args[-1]
        calls.append(
            {
                "resource_id": args[0] if len(args) == 2 else None,
                "request_type": type(request).__name__,
                "tenant_id": ctx.tenant_id,
                "project_id": ctx.project_id,
                "user_id": ctx.user_id,
                "idempotency_key": ctx.idempotency_key,
            }
        )
        return _service_result(service_name, ctx.trace_id)

    monkeypatch.setattr(label_mappings, service_name, service_stub)

    response = client.post(
        path,
        json=body,
        headers=_headers(auth_headers, f"mapping-http-{service_name}"),
    )

    assert response.status_code == expected_status, response.text
    assert set(response.json()) == {"data", "meta"}
    assert response.json()["data"] == _service_result(
        service_name,
        response.json()["meta"]["trace_id"],
    )
    assert response.json()["meta"]["request_id"] == auth_headers["X-Request-Id"]
    assert response.headers["X-Trace-Id"] == response.json()["meta"]["trace_id"]
    assert calls == [
        {
            "resource_id": resource_id,
            "request_type": request_type,
            "tenant_id": "aurora_auto",
            "project_id": "sales_qa",
            "user_id": "u_admin_001",
            "idempotency_key": f"mapping-http-{service_name}",
        }
    ]


def test_mapping_http_contract_requires_idempotency_before_dry_run(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    called = False

    def service_stub(*_args):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(label_mappings, "dry_run_label_mapping_edge", service_stub)

    response = client.post(
        "/api/v1/label-mapping-versions/dry-run",
        json=CREATE_BODY,
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert response.json()["error"]["idempotency_key"] is None
    assert called is False


def test_mapping_http_contract_rejects_forged_compiler_output_with_typed_error_envelope(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    called = False

    def service_stub(*_args):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(label_mappings, "create_label_mapping_version", service_stub)

    response = client.post(
        "/api/v1/label-mapping-versions",
        json={**CREATE_BODY, "canonical_manifest": {"forged": True}},
        headers=_headers(auth_headers, "mapping-http-forged-output"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["status"] == 422
    assert response.json()["error"]["details"][0]["code"] == "extra_forbidden"
    assert response.json()["error"]["trace_id"] == response.headers["X-Trace-Id"]
    assert called is False


def test_mapping_http_contract_enforces_candidate_write_rbac(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    called = False

    def service_stub(*_args):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(label_mappings, "create_label_mapping_version", service_stub)

    response = client.post(
        "/api/v1/label-mapping-versions",
        json=CREATE_BODY,
        headers=_headers(
            auth_headers,
            "mapping-http-rbac",
            token="annotator-token",
        ),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert response.json()["error"]["details"][0]["action"] == ("label_mapping_versions.create")
    assert called is False


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/api/v1/label-mapping-versions/lmv_mapping_http_v1/approve",
            {"expected_resource_version": 2, "reason": "系统身份不得代签"},
        ),
        ("/api/v1/label-mapping-bundles/publish", PUBLISH_BODY),
    ],
)
def test_mapping_approval_and_bundle_publish_preserve_natural_human_guard(
    client,
    auth_headers,
    path: str,
    body: dict[str, Any],
) -> None:
    response = client.post(
        path,
        json=body,
        headers=_headers(
            auth_headers,
            f"mapping-http-system-{path.rsplit('/', 1)[-1]}",
            token="system-token",
        ),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "LABEL_MAPPING_HUMAN_APPROVAL_REQUIRED"


def test_mapping_service_conflicts_keep_the_shared_error_contract(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    def conflict_stub(*_args):
        raise ApiError(
            "RESOURCE_VERSION_CONFLICT",
            "标签映射版本 resource_version 已变化",
            409,
            details=[
                {
                    "actual_resource_version": 4,
                    "expected_resource_version": 3,
                }
            ],
        )

    monkeypatch.setattr(label_mappings, "validate_label_mapping_version", conflict_stub)

    response = client.post(
        "/api/v1/label-mapping-versions/lmv_mapping_http_v1/validate",
        json={"expected_resource_version": 3},
        headers=_headers(auth_headers, "mapping-http-conflict"),
    )

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "RESOURCE_VERSION_CONFLICT",
        "message": "标签映射版本 resource_version 已变化",
        "details": [
            {
                "actual_resource_version": 4,
                "expected_resource_version": 3,
            }
        ],
        "status": 409,
        "retryable": False,
        "request_id": auth_headers["X-Request-Id"],
        "trace_id": response.headers["X-Trace-Id"],
        "idempotency_key": "mapping-http-conflict",
    }


def test_mapping_runtime_openapi_exposes_only_the_frozen_minimum_write_contract(client) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "/api/v1/label-mapping-versions/dry-run": (
            "200",
            "postLabelMappingVersionsDryRun",
            "LabelMappingCreateRequest",
            "LabelMappingDryRunResponse",
        ),
        "/api/v1/label-mapping-versions": (
            "201",
            "postLabelMappingVersions",
            "LabelMappingCreateRequest",
            "LabelMappingVersionCreateResponse",
        ),
        "/api/v1/label-mapping-versions/{id}/validate": (
            "200",
            "postLabelMappingVersionsByIdValidate",
            "LabelMappingValidationRequest",
            "LabelMappingValidationResponse",
        ),
        "/api/v1/label-mapping-versions/{id}/approve": (
            "200",
            "postLabelMappingVersionsByIdApprove",
            "LabelMappingApprovalRequest",
            "LabelMappingApprovalResponse",
        ),
        "/api/v1/label-mapping-bundles/publish": (
            "201",
            "postLabelMappingBundlesPublish",
            "LabelMappingBundlePublishRequest",
            "LabelMappingBundlePublishResponse",
        ),
    }

    for path, (success_status, operation_id, request_schema, response_schema) in expected.items():
        operation = paths[path]["post"]
        assert operation["operationId"] == operation_id
        assert success_status in operation["responses"]
        assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
            f"/{request_schema}"
        )
        assert operation["responses"][success_status]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith(f"/{response_schema}")
        assert {"400", "401", "403", "404", "409", "422", "429", "503"}.issubset(
            operation["responses"]
        )


def test_mapping_runtime_openapi_models_are_strict_and_typed(client) -> None:
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    for schema_name in (
        "LabelMappingDryRunResult",
        "LabelMappingVersionCreateResult",
        "LabelMappingValidationResult",
        "LabelMappingApprovalResult",
        "LabelMappingBundlePublishResult",
    ):
        assert schemas[schema_name]["additionalProperties"] is False

    compiled_items = schemas["LabelMappingDryRunResult"]["properties"]["items"]
    assert compiled_items["items"]["$ref"].endswith("/LabelMappingCompiledItemResult")
    assert schemas["LabelMappingCompiledItemResult"]["additionalProperties"] is False
    assert schemas["LabelMappingCoverageResult"]["additionalProperties"] is False

    edge_manifest = schemas["LabelMappingDryRunResult"]["properties"]["canonical_manifest"]["$ref"]
    assert edge_manifest.endswith("/LabelMappingEdgeCanonicalManifest")
    assert schemas["LabelMappingEdgeCanonicalManifest"]["additionalProperties"] is False

    bundle_manifest = schemas["LabelMappingBundlePublishResult"]["properties"][
        "canonical_manifest"
    ]["$ref"]
    assert bundle_manifest.endswith("/LabelMappingBundleCanonicalManifest")
    bundle_schema = schemas["LabelMappingBundleCanonicalManifest"]
    assert bundle_schema["additionalProperties"] is False
    for field, result_schema in {
        "sources": "LabelMappingBundleSourceResult",
        "members": "LabelMappingBundleMemberResult",
        "paths": "LabelMappingBundlePathResult",
    }.items():
        assert bundle_schema["properties"][field]["items"]["$ref"].endswith(f"/{result_schema}")


def test_mapping_http_idempotency_replays_the_exact_envelope_without_reinvoking_service(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    calls = 0

    def service_stub(session, ctx, request):
        nonlocal calls
        calls += 1
        result = _service_result("create_label_mapping_version", ctx.trace_id)
        result["mapping_version_id"] = "lmv_http_exact_replay"
        return result

    monkeypatch.setattr(label_mappings, "create_label_mapping_version", service_stub)
    headers = _headers(auth_headers, "mapping-http-exact-replay")

    first = client.post("/api/v1/label-mapping-versions", json=CREATE_BODY, headers=headers)
    replay = client.post("/api/v1/label-mapping-versions", json=CREATE_BODY, headers=headers)

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert calls == 1
