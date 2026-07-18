from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal

import pytest
from pydantic import JsonValue
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.context import RequestContext
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
from app.schemas.label_fact_sets import (
    LabelFactSetApproveRequest,
    LabelFactSetCreateRequest,
    LabelFactSetPromoteRequest,
    LabelFactSetValidateRequest,
)
from app.services.label_fact_set_service import (
    approve_label_fact_set,
    create_label_fact_set,
    promote_label_fact_set,
    strict_canonical_sha256,
    validate_label_fact_set,
    verify_label_fact_set_head_chain,
)

TENANT_ID = "tenant_fact_sets"
PROJECT_ID = "project_fact_sets"
TARGET_VERSION_ID = "label_version_fact_sets_v1"
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


def _ctx(
    idempotency_key: str,
    *,
    user_id: str = "admin_fact_sets",
    roles: tuple[str, ...] = ("project_admin",),
    actor_kind: str = "human",
    tenant_id: str = TENANT_ID,
    project_id: str = PROJECT_ID,
    trace_id: str = "trace_fact_sets_root",
) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        project_id=project_id,
        user_id=user_id,
        roles=roles,
        request_id=f"request_{idempotency_key}",
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        actor_kind=actor_kind,
    )


def _seed_target_version(
    session: Session,
    *,
    tenant_id: str = TENANT_ID,
    project_id: str = PROJECT_ID,
    label_version_id: str = TARGET_VERSION_ID,
    suffix: str = "main",
) -> LabelVersion:
    taxonomy = LabelTaxonomy(
        taxonomy_id=f"taxonomy_fact_sets_{suffix}",
        tenant_id=tenant_id,
        project_id=project_id,
        name=f"FactSet taxonomy {suffix}",
        description="FactSet integration fixture",
        status="active",
        resource_version=1,
        content_sha256=_sha([tenant_id, project_id, "taxonomy", suffix]),
        trace_id=f"trace_taxonomy_{suffix}",
        payload={"fixture": True},
    )
    version = LabelVersion(
        label_version_id=label_version_id,
        tenant_id=tenant_id,
        project_id=project_id,
        status="published",
        resource_version=7,
        taxonomy_id=taxonomy.taxonomy_id,
        semantic_version="1.0.0",
        artifact_status="published",
        artifact_published_at=datetime(2026, 7, 18, 0, 0, tzinfo=UTC),
        content_sha256=_sha([tenant_id, project_id, "label-version", suffix]),
        trace_id=f"trace_label_version_{suffix}",
        payload={"fixture": True},
    )
    session.add_all([taxonomy, version])
    session.flush()
    return version


def _create_request(
    ordinal: int,
    *,
    target_label_version_id: str = TARGET_VERSION_ID,
    fact_namespace: str = FACT_NAMESPACE,
    row_count: int = 2,
) -> LabelFactSetCreateRequest:
    partition_manifest: dict[str, JsonValue] = {
        "schema_version": "auris.label-fact-partitions/1",
        "partitions": [
            {
                "partition_key": f"2026-07-{ordinal:02d}",
                "row_count": row_count,
                "content_sha256": _sha(["partition", ordinal]),
            }
        ],
    }
    return LabelFactSetCreateRequest(
        fact_namespace=fact_namespace,
        target_label_version_id=target_label_version_id,
        fact_as_of=datetime(2026, 7, min(ordinal, 28), 12, 0, tzinfo=UTC),
        partition_manifest=partition_manifest,
        partition_manifest_sha256=_sha(partition_manifest),
        source_manifest_sha256=_sha(["source", ordinal]),
        result_manifest_sha256=_sha(["result", ordinal]),
        row_count=row_count,
    )


def _create_to_approved(
    session: Session,
    *,
    ordinal: int,
    key_prefix: str,
) -> tuple[LabelFactSetCreateRequest, dict[str, object]]:
    request = _create_request(ordinal)
    created = create_label_fact_set(
        session,
        _ctx(f"{key_prefix}-create"),
        request,
    )
    fact_set_id = str(created["fact_set_id"])
    validated = validate_label_fact_set(
        session,
        _ctx(f"{key_prefix}-validate", roles=("model_engineer",)),
        fact_set_id,
        LabelFactSetValidateRequest(
            expected_manifest_sha256=str(created["manifest_sha256"]),
        ),
    )
    assert validated["status"] == "validated"
    approved = approve_label_fact_set(
        session,
        _ctx(f"{key_prefix}-approve"),
        fact_set_id,
        LabelFactSetApproveRequest(
            expected_manifest_sha256=str(created["manifest_sha256"]),
            approval_id=f"approval_{key_prefix}",
            reason="FactSet manifest and lineage verified",
        ),
    )
    assert approved["status"] == "approved"
    return request, approved


def _promote_request(
    *,
    action: Literal["bootstrap", "promote", "rollback"],
    expected_generation: int,
    current_fact_set_id: str | None = None,
    current_manifest_sha256: str | None = None,
) -> LabelFactSetPromoteRequest:
    return LabelFactSetPromoteRequest(
        environment=ENVIRONMENT,
        action=action,
        expected_generation=expected_generation,
        expected_current_fact_set_id=current_fact_set_id,
        expected_current_manifest_sha256=current_manifest_sha256,
    )


def _count(
    session: Session,
    model: type[object],
    *criteria: ColumnElement[bool],
) -> int:
    statement = select(func.count()).select_from(model)
    for criterion in criteria:
        statement = statement.where(criterion)
    return int(session.scalar(statement) or 0)


def test_candidate_creation_freezes_strict_manifest_and_actor_body_idempotency() -> None:
    request = _create_request(1)
    ctx = _ctx("fact-set-create-idempotent")

    with SessionLocal() as session:
        version = _seed_target_version(session)
        response = create_label_fact_set(session, ctx, request)
        session.commit()

        stored = session.get(LabelFactSet, response["fact_set_id"])
        assert stored is not None
        assert stored.status == "candidate"
        assert stored.fact_namespace == request.fact_namespace
        assert stored.target_label_version_id == version.label_version_id
        assert stored.fact_as_of.replace(tzinfo=UTC) == request.fact_as_of
        assert stored.partition_manifest == request.partition_manifest
        assert stored.partition_manifest_sha256 == request.partition_manifest_sha256
        assert stored.source_manifest_sha256 == request.source_manifest_sha256
        assert stored.result_manifest_sha256 == request.result_manifest_sha256
        assert stored.row_count == request.row_count
        assert stored.root_trace_id == ctx.trace_id
        assert stored.action_trace_id == ctx.trace_id
        assert stored.payload["trace_anchor"] == {
            "action_trace_id": ctx.trace_id,
            "root_trace_id": ctx.trace_id,
        }
        assert stored.manifest_sha256 == strict_canonical_sha256(stored.payload["frozen_manifest"])
        assert response["manifest_sha256"] == stored.manifest_sha256
        assert (
            _count(
                session,
                AuditLog,
                AuditLog.action == "label_fact_set.created",
                AuditLog.object_id == stored.fact_set_id,
            )
            == 1
        )
        assert (
            _count(
                session,
                OutboxEvent,
                OutboxEvent.event_type == "label_fact_set.created",
                OutboxEvent.aggregate_id == stored.fact_set_id,
            )
            == 1
        )

    with SessionLocal() as session:
        replay = create_label_fact_set(session, ctx, request)
        session.commit()
        assert replay == response
        assert _count(session, LabelFactSet) == 1

    with SessionLocal() as session:
        with pytest.raises(ApiError) as body_conflict:
            create_label_fact_set(
                session,
                ctx,
                request.model_copy(update={"row_count": request.row_count + 1}),
            )
        session.rollback()
        assert body_conflict.value.code == "IDEMPOTENCY_KEY_CONFLICT"

    with SessionLocal() as session:
        with pytest.raises(ApiError) as actor_conflict:
            create_label_fact_set(
                session,
                replace(ctx, user_id="another_admin"),
                request,
            )
        session.rollback()
        assert actor_conflict.value.code == "LABEL_FACT_SET_IDEMPOTENCY_ACTOR_CONFLICT"


def test_creation_and_validation_fail_closed_on_hash_scope_and_target_drift() -> None:
    with SessionLocal() as session:
        _seed_target_version(session)
        invalid_request = _create_request(2).model_copy(
            update={"partition_manifest_sha256": "f" * 64}
        )
        with pytest.raises(ApiError) as mismatch:
            create_label_fact_set(
                session,
                _ctx("fact-set-invalid-partition-hash"),
                invalid_request,
            )
        session.rollback()
        assert mismatch.value.code == "LABEL_FACT_SET_PARTITION_HASH_MISMATCH"
        assert _count(session, LabelFactSet) == 0
        assert _count(session, AuditLog, AuditLog.action == "label_fact_set.created") == 0
        assert (
            _count(
                session,
                OutboxEvent,
                OutboxEvent.event_type == "label_fact_set.created",
            )
            == 0
        )
        assert _count(session, IdempotencyRecord) == 0

    with SessionLocal() as session:
        _seed_target_version(session, suffix="drift")
        request = _create_request(3)
        created = create_label_fact_set(session, _ctx("fact-set-drift-create"), request)
        session.commit()

        version = session.get(LabelVersion, TARGET_VERSION_ID)
        assert version is not None
        version.content_sha256 = "e" * 64
        version.resource_version += 1
        session.commit()

    with SessionLocal() as session:
        with pytest.raises(ApiError) as drift:
            validate_label_fact_set(
                session,
                _ctx("fact-set-drift-validate", roles=("model_engineer",)),
                str(created["fact_set_id"]),
                LabelFactSetValidateRequest(
                    expected_manifest_sha256=str(created["manifest_sha256"]),
                ),
            )
        session.rollback()
        assert drift.value.code == "LABEL_FACT_SET_TARGET_ANCHOR_DRIFT"
        fact_set = session.get(LabelFactSet, created["fact_set_id"])
        assert fact_set is not None and fact_set.status == "candidate"

        foreign_ctx = _ctx(
            "fact-set-cross-scope",
            tenant_id="tenant_other",
            project_id="project_other",
            roles=("model_engineer",),
        )
        with pytest.raises(ApiError) as not_found:
            validate_label_fact_set(
                session,
                foreign_ctx,
                str(created["fact_set_id"]),
                LabelFactSetValidateRequest(
                    expected_manifest_sha256=str(created["manifest_sha256"]),
                ),
            )
        session.rollback()
        assert not_found.value.code == "LABEL_FACT_SET_NOT_FOUND"


def test_validate_approve_publish_state_machine_rejects_system_and_non_admin() -> None:
    with SessionLocal() as session:
        _seed_target_version(session)
        created = create_label_fact_set(
            session,
            _ctx("fact-set-state-create", roles=("model_engineer",)),
            _create_request(4),
        )
        session.commit()

    fact_set_id = str(created["fact_set_id"])
    approval_request = LabelFactSetApproveRequest(
        expected_manifest_sha256=str(created["manifest_sha256"]),
        approval_id="approval_state_machine",
        reason="Ready for controlled promotion",
    )

    with SessionLocal() as session:
        with pytest.raises(ApiError) as invalid_state:
            approve_label_fact_set(
                session,
                _ctx("fact-set-approve-candidate"),
                fact_set_id,
                approval_request,
            )
        session.rollback()
        assert invalid_state.value.code == "LABEL_FACT_SET_STATE_CONFLICT"

        validated = validate_label_fact_set(
            session,
            _ctx("fact-set-state-validate", roles=("model_engineer",)),
            fact_set_id,
            LabelFactSetValidateRequest(
                expected_manifest_sha256=str(created["manifest_sha256"]),
            ),
        )
        session.commit()
        assert validated["status"] == "validated"

    with SessionLocal() as session:
        with pytest.raises(ApiError) as system_denied:
            approve_label_fact_set(
                session,
                _ctx(
                    "fact-set-system-approve",
                    user_id="system",
                    roles=("system",),
                    actor_kind="system",
                ),
                fact_set_id,
                approval_request,
            )
        session.rollback()
        assert system_denied.value.code == "AGENT_LABEL_FACT_SET_APPROVAL_FORBIDDEN"

        with pytest.raises(ApiError) as reviewer_denied:
            approve_label_fact_set(
                session,
                _ctx(
                    "fact-set-reviewer-approve",
                    user_id="reviewer",
                    roles=("review_arbitrator",),
                ),
                fact_set_id,
                approval_request,
            )
        session.rollback()
        assert reviewer_denied.value.code == "FORBIDDEN"

        approved = approve_label_fact_set(
            session,
            _ctx("fact-set-admin-approve"),
            fact_set_id,
            approval_request,
        )
        session.commit()
        assert approved["status"] == "approved"
        fact_set = session.get(LabelFactSet, fact_set_id)
        assert fact_set is not None
        assert fact_set.approved_by == "admin_fact_sets"
        assert fact_set.approval_id == approval_request.approval_id

    with SessionLocal() as session:
        with pytest.raises(ApiError) as promote_denied:
            promote_label_fact_set(
                session,
                _ctx(
                    "fact-set-system-promote",
                    user_id="system",
                    roles=("system",),
                    actor_kind="system",
                ),
                fact_set_id,
                _promote_request(action="bootstrap", expected_generation=0),
            )
        session.rollback()
        assert promote_denied.value.code == "AGENT_LABEL_FACT_SET_PROMOTION_FORBIDDEN"
        assert _count(session, LabelFactSetHead) == 0
        assert _count(session, LabelFactSetHeadEvent) == 0


def test_bootstrap_promote_and_rollback_switch_whole_head_with_verifiable_hash_chain() -> None:
    with SessionLocal() as session:
        _seed_target_version(session)
        _, approved_one = _create_to_approved(session, ordinal=5, key_prefix="fact-set-one")
        first = promote_label_fact_set(
            session,
            _ctx("fact-set-one-bootstrap"),
            str(approved_one["fact_set_id"]),
            _promote_request(action="bootstrap", expected_generation=0),
        )
        session.commit()
        assert first["action"] == "bootstrap"
        assert first["generation"] == 1

        first_set = session.get(LabelFactSet, approved_one["fact_set_id"])
        assert first_set is not None and first_set.status == "published"
        frozen_first = {
            "approval_id": first_set.approval_id,
            "approved_at": first_set.approved_at,
            "approved_by": first_set.approved_by,
            "manifest_sha256": first_set.manifest_sha256,
            "payload": deepcopy(first_set.payload),
            "status": first_set.status,
            "updated_at": first_set.updated_at,
        }

        _, approved_two = _create_to_approved(session, ordinal=6, key_prefix="fact-set-two")
        second = promote_label_fact_set(
            session,
            _ctx("fact-set-two-promote"),
            str(approved_two["fact_set_id"]),
            _promote_request(
                action="promote",
                expected_generation=1,
                current_fact_set_id=str(first["current_fact_set_id"]),
                current_manifest_sha256=str(first["current_manifest_sha256"]),
            ),
        )
        session.commit()
        assert second["generation"] == 2
        assert second["previous_fact_set_id"] == approved_one["fact_set_id"]

        session.refresh(first_set)
        assert {
            "approval_id": first_set.approval_id,
            "approved_at": first_set.approved_at,
            "approved_by": first_set.approved_by,
            "manifest_sha256": first_set.manifest_sha256,
            "payload": first_set.payload,
            "status": first_set.status,
            "updated_at": first_set.updated_at,
        } == frozen_first

        rolled_back = promote_label_fact_set(
            session,
            _ctx("fact-set-one-rollback"),
            str(approved_one["fact_set_id"]),
            _promote_request(
                action="rollback",
                expected_generation=2,
                current_fact_set_id=str(second["current_fact_set_id"]),
                current_manifest_sha256=str(second["current_manifest_sha256"]),
            ),
        )
        session.commit()
        assert rolled_back["generation"] == 3
        assert rolled_back["current_fact_set_id"] == approved_one["fact_set_id"]
        assert rolled_back["previous_fact_set_id"] == approved_two["fact_set_id"]

        head = session.scalar(
            select(LabelFactSetHead).where(
                LabelFactSetHead.tenant_id == TENANT_ID,
                LabelFactSetHead.project_id == PROJECT_ID,
                LabelFactSetHead.environment == ENVIRONMENT,
                LabelFactSetHead.fact_namespace == FACT_NAMESPACE,
            )
        )
        assert head is not None
        assert head.generation == 3
        assert head.current_fact_set_id == approved_one["fact_set_id"]

        events = list(
            session.scalars(
                select(LabelFactSetHeadEvent)
                .where(
                    LabelFactSetHeadEvent.tenant_id == TENANT_ID,
                    LabelFactSetHeadEvent.project_id == PROJECT_ID,
                    LabelFactSetHeadEvent.environment == ENVIRONMENT,
                    LabelFactSetHeadEvent.fact_namespace == FACT_NAMESPACE,
                )
                .order_by(LabelFactSetHeadEvent.generation)
            )
        )
        assert [event.action for event in events] == ["bootstrap", "promote", "rollback"]
        assert events[0].payload["previous_event_sha256"] is None
        assert events[1].payload["previous_event_sha256"] == events[0].content_sha256
        assert events[2].payload["previous_event_sha256"] == events[1].content_sha256
        verification = verify_label_fact_set_head_chain(
            session,
            _ctx("fact-set-chain-read"),
            environment=ENVIRONMENT,
            fact_namespace=FACT_NAMESPACE,
        )
        assert verification == {
            "event_count": 3,
            "generation": 3,
            "head_event_sha256": events[-1].content_sha256,
            "head_id": head.fact_set_head_id,
        }
        assert (
            _count(
                session,
                AuditLog,
                AuditLog.action.in_(
                    {
                        "label_fact_set_head.bootstrap",
                        "label_fact_set_head.promote",
                        "label_fact_set_head.rollback",
                    }
                ),
            )
            == 3
        )
        assert (
            _count(
                session,
                OutboxEvent,
                OutboxEvent.event_type == "label_fact_set.promoted",
            )
            == 3
        )

        with pytest.raises(IntegrityError, match="append-only label_fact_set_head_events"):
            session.execute(
                text(
                    "UPDATE label_fact_set_head_events SET actor_id = 'tampered' "
                    "WHERE head_event_id = :event_id"
                ),
                {"event_id": events[-1].head_event_id},
            )
        session.rollback()


def test_stale_generation_and_manifest_drift_leave_no_partial_promotion() -> None:
    with SessionLocal() as session:
        _seed_target_version(session)
        _, approved_one = _create_to_approved(session, ordinal=7, key_prefix="cas-one")
        first = promote_label_fact_set(
            session,
            _ctx("cas-one-bootstrap"),
            str(approved_one["fact_set_id"]),
            _promote_request(action="bootstrap", expected_generation=0),
        )
        _, approved_two = _create_to_approved(session, ordinal=8, key_prefix="cas-two")
        _, approved_three = _create_to_approved(session, ordinal=9, key_prefix="cas-three")
        session.commit()

    with SessionLocal() as session:
        second = promote_label_fact_set(
            session,
            _ctx("cas-two-promote"),
            str(approved_two["fact_set_id"]),
            _promote_request(
                action="promote",
                expected_generation=1,
                current_fact_set_id=str(first["current_fact_set_id"]),
                current_manifest_sha256=str(first["current_manifest_sha256"]),
            ),
        )
        session.commit()
        assert second["generation"] == 2

    with SessionLocal() as session:
        with pytest.raises(ApiError) as stale:
            promote_label_fact_set(
                session,
                _ctx("cas-three-stale-promote"),
                str(approved_three["fact_set_id"]),
                _promote_request(
                    action="promote",
                    expected_generation=1,
                    current_fact_set_id=str(first["current_fact_set_id"]),
                    current_manifest_sha256=str(first["current_manifest_sha256"]),
                ),
            )
        session.rollback()
        assert stale.value.code == "LABEL_FACT_SET_HEAD_GENERATION_CONFLICT"

        head = session.scalar(select(LabelFactSetHead))
        target = session.get(LabelFactSet, approved_three["fact_set_id"])
        assert head is not None and head.generation == 2
        assert head.current_fact_set_id == approved_two["fact_set_id"]
        assert target is not None and target.status == "approved"
        assert _count(session, LabelFactSetHeadEvent) == 2
        assert (
            _count(
                session,
                OutboxEvent,
                OutboxEvent.event_type == "label_fact_set.promoted",
            )
            == 2
        )

        target.result_manifest_sha256 = "f" * 64
        session.commit()

    with SessionLocal() as session:
        with pytest.raises(ApiError) as drift:
            promote_label_fact_set(
                session,
                _ctx("cas-three-drift-promote"),
                str(approved_three["fact_set_id"]),
                _promote_request(
                    action="promote",
                    expected_generation=2,
                    current_fact_set_id=str(second["current_fact_set_id"]),
                    current_manifest_sha256=str(second["current_manifest_sha256"]),
                ),
            )
        session.rollback()
        assert drift.value.code == "LABEL_FACT_SET_CONTENT_DRIFT"

        head = session.scalar(select(LabelFactSetHead))
        assert head is not None and head.generation == 2
        assert head.current_fact_set_id == approved_two["fact_set_id"]
        assert _count(session, LabelFactSetHeadEvent) == 2
        assert (
            _count(
                session,
                AuditLog,
                AuditLog.action.in_(
                    {"label_fact_set_head.bootstrap", "label_fact_set_head.promote"}
                ),
            )
            == 2
        )


def test_existing_head_without_append_only_ledger_anchor_fails_closed() -> None:
    with SessionLocal() as session:
        _seed_target_version(session)
        _, approved_current = _create_to_approved(
            session,
            ordinal=10,
            key_prefix="missing-anchor-current",
        )
        _, approved_target = _create_to_approved(
            session,
            ordinal=11,
            key_prefix="missing-anchor-target",
        )
        current = session.get(LabelFactSet, approved_current["fact_set_id"])
        assert current is not None
        current.status = "published"
        session.add(
            LabelFactSetHead(
                fact_set_head_id="lfsh_missing_anchor",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                environment=ENVIRONMENT,
                fact_namespace=FACT_NAMESPACE,
                current_fact_set_id=current.fact_set_id,
                current_manifest_sha256=current.manifest_sha256,
                previous_fact_set_id=None,
                previous_manifest_sha256=None,
                generation=1,
                status="active",
                root_trace_id="trace_missing_anchor",
                action_trace_id="trace_missing_anchor",
                trace_id="trace_missing_anchor",
                payload={
                    "schema_version": "auris.label-fact-set-head/1",
                    "last_event_sha256": "a" * 64,
                },
            )
        )
        session.commit()

    with SessionLocal() as session:
        with pytest.raises(ApiError) as missing_anchor:
            promote_label_fact_set(
                session,
                _ctx("missing-anchor-promote"),
                str(approved_target["fact_set_id"]),
                _promote_request(
                    action="promote",
                    expected_generation=1,
                    current_fact_set_id=str(approved_current["fact_set_id"]),
                    current_manifest_sha256=str(approved_current["manifest_sha256"]),
                ),
            )
        session.rollback()
        assert missing_anchor.value.code == "LABEL_FACT_SET_HEAD_LEDGER_MISSING"
        head = session.get(LabelFactSetHead, "lfsh_missing_anchor")
        target = session.get(LabelFactSet, approved_target["fact_set_id"])
        assert head is not None and head.generation == 1
        assert head.current_fact_set_id == approved_current["fact_set_id"]
        assert target is not None and target.status == "approved"
        assert _count(session, LabelFactSetHeadEvent) == 0
        assert (
            _count(
                session,
                OutboxEvent,
                OutboxEvent.event_type == "label_fact_set.promoted",
            )
            == 0
        )
