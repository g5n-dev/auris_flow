from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.routers import label_fact_sets
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import (
    AuditLog,
    IdempotencyRecord,
    LabelFactSet,
    LabelFactSetHead,
    LabelFactSetHeadEvent,
    LabelTaxonomy,
    LabelVersion,
    OutboxEvent,
)

TENANT_ID = "aurora_auto"
PROJECT_ID = "sales_qa"
FACT_NAMESPACE = "production"
ENVIRONMENT = "production"


def _sha(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _headers(
    auth_headers: dict[str, str],
    key: str | None,
    *,
    token: str = "dev-token",
) -> dict[str, str]:
    headers = {**auth_headers, "Authorization": f"Bearer {token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _error_payload(request: Request, exc: ApiError) -> dict[str, Any]:
    trace_id = getattr(request.state, "trace_id", None) or f"trace_{uuid.uuid4().hex}"
    request.state.trace_id = trace_id
    return {
        "error": {
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
            "status": exc.status_code,
            "retryable": exc.retryable,
            "trace_id": trace_id,
            "idempotency_key": request.headers.get("Idempotency-Key"),
        }
    }


@pytest.fixture
def fact_set_client() -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def expose_trace_header(request: Request, call_next):
        response = await call_next(request)
        trace_id = getattr(request.state, "trace_id", None)
        if trace_id:
            response.headers["X-Trace-Id"] = trace_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(request, exc),
        )

    app.include_router(label_fact_sets.router, prefix="/api/v1")
    return TestClient(app)


def _seed_target_version(label_version_id: str) -> None:
    suffix = label_version_id.rsplit("_", 1)[-1]
    with SessionLocal() as session:
        taxonomy = LabelTaxonomy(
            taxonomy_id=f"taxonomy_fact_set_http_{suffix}",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            name=f"FactSet HTTP taxonomy {suffix}",
            description="FactSet HTTP contract fixture",
            status="active",
            resource_version=1,
            content_sha256=_sha(["taxonomy", suffix]),
            trace_id=f"trace_fact_set_taxonomy_{suffix}",
            payload={"fixture": True},
        )
        version = LabelVersion(
            label_version_id=label_version_id,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            status="published",
            resource_version=7,
            taxonomy_id=taxonomy.taxonomy_id,
            semantic_version="1.0.0",
            artifact_status="published",
            artifact_published_at=datetime(2026, 7, 18, 0, 0, tzinfo=UTC),
            content_sha256=_sha(["label-version", suffix]),
            trace_id=f"trace_fact_set_version_{suffix}",
            payload={"fixture": True},
        )
        session.add_all([taxonomy, version])
        session.commit()


def _create_body(label_version_id: str, ordinal: int) -> dict[str, Any]:
    partition_manifest = {
        "schema_version": "auris.label-fact-partitions/1",
        "partitions": [
            {
                "partition_key": f"2026-07-{ordinal:02d}",
                "row_count": ordinal,
                "content_sha256": _sha(["partition", ordinal]),
            }
        ],
    }
    return {
        "fact_namespace": FACT_NAMESPACE,
        "target_label_version_id": label_version_id,
        "fact_as_of": f"2026-07-{ordinal:02d}T12:00:00+00:00",
        "partition_manifest": partition_manifest,
        "partition_manifest_sha256": _sha(partition_manifest),
        "source_manifest_sha256": _sha(["source", ordinal]),
        "result_manifest_sha256": _sha(["result", ordinal]),
        "row_count": ordinal,
    }


def _create_validate_approve(
    client: TestClient,
    auth_headers: dict[str, str],
    *,
    label_version_id: str,
    ordinal: int,
    key_prefix: str,
) -> dict[str, Any]:
    created_response = client.post(
        "/api/v1/label-fact-sets",
        json=_create_body(label_version_id, ordinal),
        headers=_headers(auth_headers, f"{key_prefix}-create", token="model-token"),
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()["data"]
    validated_response = client.post(
        f"/api/v1/label-fact-sets/{created['fact_set_id']}/validations",
        json={"expected_manifest_sha256": created["manifest_sha256"]},
        headers=_headers(auth_headers, f"{key_prefix}-validate", token="model-token"),
    )
    assert validated_response.status_code == 200, validated_response.text
    approved_response = client.post(
        f"/api/v1/label-fact-sets/{created['fact_set_id']}/approvals",
        json={
            "expected_manifest_sha256": created["manifest_sha256"],
            "approval_id": f"approval_{key_prefix}",
            "reason": "完整 Manifest、血缘和分区校验通过",
        },
        headers=_headers(auth_headers, f"{key_prefix}-approve"),
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()["data"]
    assert approved["status"] == "approved"
    return approved


def test_fact_set_http_runs_candidate_to_publish_promote_and_rollback_closed_loop(
    fact_set_client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    first_version_id = "label_version_fact_set_http_one"
    second_version_id = "label_version_fact_set_http_two"
    _seed_target_version(first_version_id)
    _seed_target_version(second_version_id)

    first_body = _create_body(first_version_id, 1)
    create_headers = _headers(
        auth_headers,
        "fact-set-http-first-create",
        token="model-token",
    )
    first_create = fact_set_client.post(
        "/api/v1/label-fact-sets",
        json=first_body,
        headers=create_headers,
    )
    replay = fact_set_client.post(
        "/api/v1/label-fact-sets",
        json=first_body,
        headers=create_headers,
    )
    assert first_create.status_code == replay.status_code == 201
    assert replay.json() == first_create.json()
    assert set(first_create.json()) == {"data", "meta"}
    first_created = first_create.json()["data"]
    assert first_created["status"] == "candidate"
    assert first_create.json()["meta"]["trace_id"] == first_create.headers["X-Trace-Id"]

    first_validate = fact_set_client.post(
        f"/api/v1/label-fact-sets/{first_created['fact_set_id']}/validations",
        json={"expected_manifest_sha256": first_created["manifest_sha256"]},
        headers=_headers(auth_headers, "fact-set-http-first-validate", token="model-token"),
    )
    assert first_validate.status_code == 200, first_validate.text
    assert first_validate.json()["data"]["status"] == "validated"

    first_approve = fact_set_client.post(
        f"/api/v1/label-fact-sets/{first_created['fact_set_id']}/approvals",
        json={
            "expected_manifest_sha256": first_created["manifest_sha256"],
            "approval_id": "approval_fact_set_http_first",
            "reason": "首个生产 FactSet 已完成人工核验",
        },
        headers=_headers(auth_headers, "fact-set-http-first-approve"),
    )
    assert first_approve.status_code == 200, first_approve.text
    assert first_approve.json()["data"]["status"] == "approved"

    bootstrap = fact_set_client.post(
        f"/api/v1/label-fact-sets/{first_created['fact_set_id']}/promotions",
        json={
            "environment": ENVIRONMENT,
            "action": "bootstrap",
            "expected_generation": 0,
            "expected_current_fact_set_id": None,
            "expected_current_manifest_sha256": None,
        },
        headers=_headers(auth_headers, "fact-set-http-bootstrap"),
    )
    assert bootstrap.status_code == 200, bootstrap.text
    bootstrap_data = bootstrap.json()["data"]
    assert bootstrap_data["action"] == "bootstrap"
    assert bootstrap_data["generation"] == 1
    assert bootstrap_data["current_fact_set_id"] == first_created["fact_set_id"]

    second = _create_validate_approve(
        fact_set_client,
        auth_headers,
        label_version_id=second_version_id,
        ordinal=2,
        key_prefix="fact-set-http-second",
    )
    promotion = fact_set_client.post(
        f"/api/v1/label-fact-sets/{second['fact_set_id']}/promotions",
        json={
            "environment": ENVIRONMENT,
            "action": "promote",
            "expected_generation": bootstrap_data["generation"],
            "expected_current_fact_set_id": bootstrap_data["current_fact_set_id"],
            "expected_current_manifest_sha256": bootstrap_data["current_manifest_sha256"],
        },
        headers=_headers(auth_headers, "fact-set-http-promote"),
    )
    assert promotion.status_code == 200, promotion.text
    promotion_data = promotion.json()["data"]
    assert promotion_data["action"] == "promote"
    assert promotion_data["generation"] == 2
    assert promotion_data["current_fact_set_id"] == second["fact_set_id"]
    assert promotion_data["previous_fact_set_id"] == first_created["fact_set_id"]

    rollback = fact_set_client.post(
        f"/api/v1/label-fact-sets/{first_created['fact_set_id']}/rollbacks",
        json={
            "environment": ENVIRONMENT,
            "expected_generation": promotion_data["generation"],
            "expected_current_fact_set_id": promotion_data["current_fact_set_id"],
            "expected_current_manifest_sha256": promotion_data["current_manifest_sha256"],
        },
        headers=_headers(auth_headers, "fact-set-http-rollback"),
    )
    assert rollback.status_code == 200, rollback.text
    rollback_data = rollback.json()["data"]
    assert rollback_data["action"] == "rollback"
    assert rollback_data["generation"] == 3
    assert rollback_data["current_fact_set_id"] == first_created["fact_set_id"]

    with SessionLocal() as session:
        head = session.scalar(select(LabelFactSetHead))
        assert head is not None
        assert head.generation == 3
        assert head.current_fact_set_id == first_created["fact_set_id"]
        assert session.scalar(select(func.count()).select_from(LabelFactSet)) == 2
        assert session.scalar(select(func.count()).select_from(LabelFactSetHeadEvent)) == 3
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.object_type.in_(("label_fact_set", "label_fact_set_head")))
            )
            == 9
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.event_type.in_(
                        (
                            "label_fact_set.created",
                            "label_fact_set.validated",
                            "label_fact_set.approved",
                            "label_fact_set.promoted",
                        )
                    )
                )
            )
            == 9
        )
        first_stored = session.get(LabelFactSet, first_created["fact_set_id"])
        assert first_stored is not None
        assert first_stored.root_trace_id == first_create.json()["meta"]["trace_id"]
        http_records = session.scalars(
            select(IdempotencyRecord).where(IdempotencyRecord.operation.like("http.%"))
        ).all()
        assert len(http_records) == 9
        assert all(record.state == "completed" for record in http_records)


def test_fact_set_http_enforces_writer_rbac_and_natural_person_approval_guards(
    fact_set_client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    version_id = "label_version_fact_set_http_guard"
    _seed_target_version(version_id)
    body = _create_body(version_id, 3)

    forbidden = fact_set_client.post(
        "/api/v1/label-fact-sets",
        json=body,
        headers=_headers(auth_headers, "fact-set-http-forbidden", token="annotator-token"),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"

    created = fact_set_client.post(
        "/api/v1/label-fact-sets",
        json=body,
        headers=_headers(auth_headers, "fact-set-http-guard-create", token="model-token"),
    ).json()["data"]
    validated_response = fact_set_client.post(
        f"/api/v1/label-fact-sets/{created['fact_set_id']}/validations",
        json={"expected_manifest_sha256": created["manifest_sha256"]},
        headers=_headers(auth_headers, "fact-set-http-guard-validate", token="model-token"),
    )
    assert validated_response.status_code == 200

    system_approval = fact_set_client.post(
        f"/api/v1/label-fact-sets/{created['fact_set_id']}/approvals",
        json={
            "expected_manifest_sha256": created["manifest_sha256"],
            "approval_id": "approval_system_forbidden",
            "reason": "系统身份不得代签",
        },
        headers=_headers(auth_headers, "fact-set-http-system-approve", token="system-token"),
    )
    assert system_approval.status_code == 403
    assert system_approval.json()["error"]["code"] == "AGENT_LABEL_FACT_SET_APPROVAL_FORBIDDEN"

    with SessionLocal() as session:
        stored = session.get(LabelFactSet, created["fact_set_id"])
        assert stored is not None and stored.status == "validated"
        assert stored.approval_id is None


def test_fact_set_http_idempotency_is_actor_bound_and_rejects_body_reuse(
    fact_set_client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    version_id = "label_version_fact_set_http_idempotency"
    _seed_target_version(version_id)
    body = _create_body(version_id, 4)
    key = "fact-set-http-actor-bound"

    first = fact_set_client.post(
        "/api/v1/label-fact-sets",
        json=body,
        headers=_headers(auth_headers, key, token="model-token"),
    )
    assert first.status_code == 201

    actor_takeover = fact_set_client.post(
        "/api/v1/label-fact-sets",
        json=body,
        headers=_headers(auth_headers, key),
    )
    assert actor_takeover.status_code == 409
    assert actor_takeover.json()["error"]["code"] == "LABEL_FACT_SET_IDEMPOTENCY_ACTOR_CONFLICT"

    body_conflict = fact_set_client.post(
        "/api/v1/label-fact-sets",
        json={**body, "row_count": 99},
        headers=_headers(auth_headers, key, token="model-token"),
    )
    assert body_conflict.status_code == 409
    assert body_conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(LabelFactSet)) == 1


def test_fact_set_http_requires_idempotency_and_returns_typed_validation_errors(
    fact_set_client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    version_id = "label_version_fact_set_http_validation"
    _seed_target_version(version_id)
    body = _create_body(version_id, 5)

    missing_key = fact_set_client.post(
        "/api/v1/label-fact-sets",
        json=body,
        headers=_headers(auth_headers, None, token="model-token"),
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    forged = fact_set_client.post(
        "/api/v1/label-fact-sets",
        json={**body, "client_status": "published"},
        headers=_headers(auth_headers, "fact-set-http-invalid", token="model-token"),
    )
    assert forged.status_code == 422
    assert forged.json()["error"]["code"] == "VALIDATION_ERROR"
    assert forged.json()["error"]["details"][0]["code"] == "extra_forbidden"


def test_fact_set_router_openapi_freezes_plural_kebab_case_write_resources(
    client: TestClient,
) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "/api/v1/label-fact-sets": ("201", "postLabelFactSets"),
        "/api/v1/label-fact-sets/{id}/validations": (
            "200",
            "postLabelFactSetsByIdValidations",
        ),
        "/api/v1/label-fact-sets/{id}/approvals": (
            "200",
            "postLabelFactSetsByIdApprovals",
        ),
        "/api/v1/label-fact-sets/{id}/promotions": (
            "200",
            "postLabelFactSetsByIdPromotions",
        ),
        "/api/v1/label-fact-sets/{id}/rollbacks": (
            "200",
            "postLabelFactSetsByIdRollbacks",
        ),
    }
    for path, (success_status, operation_id) in expected.items():
        operation = paths[path]["post"]
        assert operation["operationId"] == operation_id
        assert success_status in operation["responses"]
        assert {"400", "401", "403", "404", "409", "422", "429", "503"}.issubset(
            operation["responses"]
        )
