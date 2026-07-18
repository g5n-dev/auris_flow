from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import (
    AuditLog,
    IdempotencyRecord,
    LabelMappingBundle,
    LabelMappingBundleMember,
    LabelMappingBundlePath,
    LabelMappingBundleSource,
    LabelMappingItem,
    LabelMappingItemTarget,
    LabelMappingVersion,
    LabelTaxonomy,
    LabelVersion,
    LabelVersionItem,
    OutboxEvent,
)
from app.schemas.label_mapping import (
    LabelMappingApprovalRequest,
    LabelMappingBundlePublishRequest,
    LabelMappingCreateRequest,
    LabelMappingValidationRequest,
)
from app.services.label_mapping_service import (
    approve_label_mapping_version,
    create_label_mapping_version,
    dry_run_label_mapping_edge,
    publish_label_mapping_bundle,
    validate_label_mapping_version,
)

TENANT_ID = "tenant_label_mapping_service"
PROJECT_ID = "project_label_mapping_service"
TAXONOMY_ID = "taxonomy_label_mapping_service"
SOURCE_VERSION_ID = "lv_mapping_source"
TARGET_VERSION_ID = "lv_mapping_target"


def _ctx(
    key: str,
    *,
    user_id: str = "u_mapping_admin",
    roles: tuple[str, ...] = ("project_admin",),
    actor_kind: str = "human",
) -> RequestContext:
    return RequestContext(
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        user_id=user_id,
        roles=roles,
        request_id=f"request-{key}",
        trace_id=f"trace-{key}",
        idempotency_key=key,
        actor_kind=actor_kind,
    )


def _label_item(
    version_id: str,
    label_id: str,
    *,
    canonical_name: str,
    aggregation_rule: dict[str, object] | None = None,
) -> LabelVersionItem:
    return LabelVersionItem(
        label_version_item_id=f"lvi-{version_id}-{label_id}",
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        label_version_id=version_id,
        label_id=label_id,
        canonical_name=canonical_name,
        aliases=[],
        value_type="boolean",
        risk_level="low",
        mutual_exclusion_group=None,
        parent_ids=[],
        aggregation_rule=aggregation_rule or {"mode": "presence"},
        status="active",
        definition_sha256=None,
        trace_id=f"trace-{version_id}-{label_id}",
    )


def _seed_strong_label_versions(*, target_status: str = "candidate") -> None:
    with SessionLocal.begin() as session:
        session.add(
            LabelTaxonomy(
                taxonomy_id=TAXONOMY_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                name="映射持久化测试标签体系",
                description="冻结边、子项和目标的集成测试基线",
                status="active",
                resource_version=1,
                content_sha256="1" * 64,
                trace_id="trace-taxonomy-label-mapping",
                payload={"schema_version": "auris.label-taxonomy/1"},
            )
        )
        session.add_all(
            [
                LabelVersion(
                    label_version_id=SOURCE_VERSION_ID,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    status="published",
                    resource_version=1,
                    taxonomy_id=TAXONOMY_ID,
                    semantic_version="mapping-source-1.0.0",
                    artifact_status="published",
                    content_sha256="a" * 64,
                    trace_id="trace-mapping-source",
                    payload={"schema_version": "auris.label-version/1"},
                ),
                LabelVersion(
                    label_version_id=TARGET_VERSION_ID,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    status=target_status,
                    resource_version=1,
                    taxonomy_id=TAXONOMY_ID,
                    semantic_version="mapping-target-2.0.0",
                    artifact_status=target_status,
                    content_sha256="b" * 64,
                    trace_id="trace-mapping-target",
                    payload={"schema_version": "auris.label-version/1"},
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                _label_item(SOURCE_VERSION_ID, "label_a", canonical_name="标签 A"),
                _label_item(SOURCE_VERSION_ID, "label_b", canonical_name="标签 B"),
                _label_item(TARGET_VERSION_ID, "label_a", canonical_name="标签 A"),
                _label_item(TARGET_VERSION_ID, "label_b", canonical_name="标签 B"),
                _label_item(
                    TARGET_VERSION_ID,
                    "label_merged",
                    canonical_name="标签 A 或 B",
                ),
                _label_item(
                    TARGET_VERSION_ID,
                    "label_extra",
                    canonical_name="额外标签",
                ),
            ]
        )


def _merge_request(*, mapping_version: str = "mapping-1.0.0") -> LabelMappingCreateRequest:
    return LabelMappingCreateRequest.model_validate(
        {
            "mapping_version": mapping_version,
            "source_label_version_id": SOURCE_VERSION_ID,
            "target_label_version_id": TARGET_VERSION_ID,
            "expected_source_resource_version": 1,
            "expected_target_resource_version": 1,
            "items": [
                {
                    "relation": "merge",
                    "source_label_ids": ["label_a", "label_b"],
                    "target_label_id": "label_merged",
                    "allowed_metric_families": ["presence", "distinct-count"],
                    "metric_grain": "business-event",
                    "lineage_key": "event_id",
                    "reducer": "presence-any",
                }
            ],
        }
    )


def _identity_request(*, mapping_version: str = "mapping-1.0.0") -> LabelMappingCreateRequest:
    return LabelMappingCreateRequest.model_validate(
        {
            "mapping_version": mapping_version,
            "source_label_version_id": SOURCE_VERSION_ID,
            "target_label_version_id": TARGET_VERSION_ID,
            "expected_source_resource_version": 1,
            "expected_target_resource_version": 1,
            "items": [
                {
                    "relation": "identity",
                    "source_label_id": "label_a",
                    "target_label_id": "label_a",
                },
                {
                    "relation": "identity",
                    "source_label_id": "label_b",
                    "target_label_id": "label_b",
                },
            ],
        }
    )


def _seed_identity_versions(
    versions: dict[str, tuple[str, ...]],
) -> None:
    with SessionLocal.begin() as session:
        session.add(
            LabelTaxonomy(
                taxonomy_id=TAXONOMY_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                name="Bundle DAG 测试标签体系",
                description="多源与多跳发布基线",
                status="active",
                resource_version=1,
                content_sha256="1" * 64,
                trace_id="trace-taxonomy-bundle-dag",
                payload={"schema_version": "auris.label-taxonomy/1"},
            )
        )
        for order, (version_id, _label_ids) in enumerate(sorted(versions.items())):
            digest_character = chr(ord("a") + order)
            session.add(
                LabelVersion(
                    label_version_id=version_id,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    status="published",
                    resource_version=1,
                    taxonomy_id=TAXONOMY_ID,
                    semantic_version=f"bundle-dag-{order + 1}.0.0",
                    artifact_status="published",
                    content_sha256=digest_character * 64,
                    trace_id=f"trace-{version_id}",
                    payload={"schema_version": "auris.label-version/1"},
                )
            )
        session.flush()
        for version_id, label_ids in versions.items():
            session.add_all(
                _label_item(version_id, label_id, canonical_name=label_id) for label_id in label_ids
            )


def _identity_edge_request(
    source_version_id: str,
    target_version_id: str,
    label_ids: tuple[str, ...],
    *,
    mapping_version: str,
) -> LabelMappingCreateRequest:
    return LabelMappingCreateRequest.model_validate(
        {
            "mapping_version": mapping_version,
            "source_label_version_id": source_version_id,
            "target_label_version_id": target_version_id,
            "expected_source_resource_version": 1,
            "expected_target_resource_version": 1,
            "items": [
                {
                    "relation": "identity",
                    "source_label_id": label_id,
                    "target_label_id": label_id,
                }
                for label_id in label_ids
            ],
        }
    )


def _create_validate_approve_edge(
    request: LabelMappingCreateRequest,
    *,
    key_prefix: str,
) -> dict[str, object]:
    with SessionLocal.begin() as session:
        created = create_label_mapping_version(
            session,
            _ctx(f"{key_prefix}-create"),
            request,
        )
    mapping_version_id = str(created["mapping_version_id"])
    with SessionLocal.begin() as session:
        validate_label_mapping_version(
            session,
            _ctx(f"{key_prefix}-validate"),
            mapping_version_id,
            LabelMappingValidationRequest(expected_resource_version=1),
        )
    with SessionLocal.begin() as session:
        return approve_label_mapping_version(
            session,
            _ctx(f"{key_prefix}-approve"),
            mapping_version_id,
            LabelMappingApprovalRequest(
                expected_resource_version=2,
                reason="Bundle 发布前确认映射语义与统计口径",
            ),
        )


def _bundle_request(
    mapping_version_ids: list[str],
    source_label_version_ids: list[str],
    target_label_version_id: str,
    *,
    expected_mapping_resource_version: int = 3,
) -> LabelMappingBundlePublishRequest:
    return LabelMappingBundlePublishRequest(
        mapping_version_ids=mapping_version_ids,
        expected_mapping_resource_versions={
            mapping_version_id: expected_mapping_resource_version
            for mapping_version_id in mapping_version_ids
        },
        source_label_version_ids=source_label_version_ids,
        expected_source_resource_versions={
            source_label_version_id: 1 for source_label_version_id in source_label_version_ids
        },
        target_label_version_id=target_label_version_id,
        expected_target_resource_version=1,
    )


def _scope_count(session, model: type[object]) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(model)
            .where(  # type: ignore[attr-defined]
                model.tenant_id == TENANT_ID,  # type: ignore[attr-defined]
                model.project_id == PROJECT_ID,  # type: ignore[attr-defined]
            )
        )
        or 0
    )


def test_dry_run_is_deterministic_and_has_no_side_effects() -> None:
    _seed_strong_label_versions()
    request = _merge_request()

    with SessionLocal() as session:
        first = dry_run_label_mapping_edge(session, _ctx("dry-run-a"), request)
        second = dry_run_label_mapping_edge(session, _ctx("dry-run-b"), request)

        assert first == second
        assert first["persisted"] is False
        assert first["content_sha256"] == first["canonical_manifest_sha256"]
        assert first["coverage"] == {
            "active_source_item_count": 2,
            "coverage_gap_source_label_ids": [],
            "disposition_count": 2,
            "exact_count": 0,
            "metric_dependent_count": 2,
            "normalizable_count": 2,
            "recompute_required_source_label_ids": [],
            "structural_break_count": 0,
            "unmapped_source_label_ids": [],
        }
        assert _scope_count(session, LabelMappingVersion) == 0
        assert _scope_count(session, AuditLog) == 0
        assert _scope_count(session, OutboxEvent) == 0


def test_create_freezes_parent_items_targets_and_emits_audit_outbox() -> None:
    _seed_strong_label_versions()
    request = _merge_request()

    with SessionLocal.begin() as session:
        dry_run = dry_run_label_mapping_edge(session, _ctx("dry-before-create"), request)
        created = create_label_mapping_version(session, _ctx("create-mapping"), request)

        mapping = session.get(LabelMappingVersion, created["mapping_version_id"])
        items = list(
            session.scalars(
                select(LabelMappingItem)
                .where(LabelMappingItem.mapping_version_id == created["mapping_version_id"])
                .order_by(LabelMappingItem.source_label_id)
            )
        )
        targets = list(
            session.scalars(
                select(LabelMappingItemTarget)
                .where(LabelMappingItemTarget.mapping_version_id == created["mapping_version_id"])
                .order_by(
                    LabelMappingItemTarget.mapping_item_id,
                    LabelMappingItemTarget.target_order,
                )
            )
        )

        assert mapping is not None
        assert mapping.status == "draft"
        assert mapping.resource_version == 1
        assert mapping.source_resource_version == 1
        assert mapping.target_resource_version == 1
        assert mapping.content_sha256 == dry_run["content_sha256"]
        assert mapping.payload["canonical_manifest"] == dry_run["canonical_manifest"]
        assert len(items) == 2
        assert {item.source_label_id for item in items} == {"label_a", "label_b"}
        assert {item.relation for item in items} == {"merge"}
        assert len(targets) == 2
        assert {target.target_label_id for target in targets} == {"label_merged"}
        assert created["status"] == "draft"
        assert created["deduplicated"] is False
        assert created["audit_id"] > 0
        assert created["outbox_event_id"] > 0
        assert _scope_count(session, AuditLog) == 1
        assert _scope_count(session, OutboxEvent) == 1
        event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.tenant_id == TENANT_ID,
                OutboxEvent.project_id == PROJECT_ID,
            )
        )
        assert event is not None
        assert event.event_type == "label_mapping_version.created"
        assert event.payload["resource_version"] == 1
        assert "canonical_manifest" not in event.payload["data"]


def test_create_replays_exactly_and_rejects_changed_body_for_same_key() -> None:
    _seed_strong_label_versions()
    request = _merge_request()
    ctx = _ctx("create-replay")

    with SessionLocal.begin() as session:
        first = create_label_mapping_version(session, ctx, request)
    with SessionLocal.begin() as session:
        replay = create_label_mapping_version(session, ctx, request)
        assert replay == first
        assert _scope_count(session, LabelMappingVersion) == 1
        assert _scope_count(session, LabelMappingItem) == 2
        assert _scope_count(session, LabelMappingItemTarget) == 2
        assert _scope_count(session, AuditLog) == 1
        assert _scope_count(session, OutboxEvent) == 1
        assert _scope_count(session, IdempotencyRecord) == 1

    changed = _identity_request()
    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        create_label_mapping_version(session, ctx, changed)
    assert exc_info.value.code == "IDEMPOTENCY_KEY_CONFLICT"


def test_content_dedupe_reuses_frozen_mapping_without_duplicate_events() -> None:
    _seed_strong_label_versions()
    request = _merge_request()

    with SessionLocal.begin() as session:
        first = create_label_mapping_version(session, _ctx("create-first"), request)
    with SessionLocal.begin() as session:
        duplicate = create_label_mapping_version(session, _ctx("create-dedupe"), request)

        assert duplicate["mapping_version_id"] == first["mapping_version_id"]
        assert duplicate["content_sha256"] == first["content_sha256"]
        assert duplicate["deduplicated"] is True
        assert duplicate["audit_id"] == first["audit_id"]
        assert duplicate["outbox_event_id"] == first["outbox_event_id"]
        assert _scope_count(session, LabelMappingVersion) == 1
        assert _scope_count(session, AuditLog) == 1
        assert _scope_count(session, OutboxEvent) == 1
        assert _scope_count(session, IdempotencyRecord) == 2


def test_same_mapping_version_with_different_content_conflicts() -> None:
    _seed_strong_label_versions()
    with SessionLocal.begin() as session:
        create_label_mapping_version(session, _ctx("create-version"), _merge_request())

    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        create_label_mapping_version(
            session,
            _ctx("create-version-conflict"),
            _identity_request(),
        )

    assert exc_info.value.code == "LABEL_MAPPING_VERSION_CONFLICT"
    assert exc_info.value.status_code == 409


def test_validate_recompiles_frozen_request_and_replays_without_new_events() -> None:
    _seed_strong_label_versions()
    with SessionLocal.begin() as session:
        created = create_label_mapping_version(
            session,
            _ctx("create-for-validation"),
            _merge_request(),
        )

    validation = LabelMappingValidationRequest(expected_resource_version=1)
    ctx = _ctx("validate-mapping")
    with SessionLocal.begin() as session:
        first = validate_label_mapping_version(
            session,
            ctx,
            created["mapping_version_id"],
            validation,
        )
        assert first["status"] == "validated"
        assert first["resource_version"] == 2
        assert first["audit_id"] > created["audit_id"]
        assert first["outbox_event_id"] > created["outbox_event_id"]

    with SessionLocal.begin() as session:
        replay = validate_label_mapping_version(
            session,
            ctx,
            created["mapping_version_id"],
            validation,
        )
        assert replay == first
        assert _scope_count(session, LabelMappingVersion) == 1
        assert _scope_count(session, LabelMappingItem) == 2
        assert _scope_count(session, LabelMappingItemTarget) == 2
        assert _scope_count(session, AuditLog) == 2
        assert _scope_count(session, OutboxEvent) == 2
        mapping = session.get(LabelMappingVersion, created["mapping_version_id"])
        assert mapping is not None and mapping.resource_version == 2


def test_validate_rejects_label_resource_drift_without_mutating_mapping() -> None:
    _seed_strong_label_versions()
    with SessionLocal.begin() as session:
        created = create_label_mapping_version(
            session,
            _ctx("create-for-resource-drift"),
            _merge_request(),
        )
    with SessionLocal.begin() as session:
        source = session.get(LabelVersion, SOURCE_VERSION_ID)
        assert source is not None
        source.resource_version = 2

    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        validate_label_mapping_version(
            session,
            _ctx("validate-resource-drift"),
            created["mapping_version_id"],
            LabelMappingValidationRequest(expected_resource_version=1),
        )

    assert exc_info.value.code == "LABEL_MAPPING_RESOURCE_VERSION_CONFLICT"
    assert exc_info.value.status_code == 409
    with SessionLocal() as session:
        mapping = session.get(LabelMappingVersion, created["mapping_version_id"])
        assert mapping is not None
        assert mapping.status == "draft"
        assert mapping.resource_version == 1
        assert _scope_count(session, AuditLog) == 1
        assert _scope_count(session, OutboxEvent) == 1


def test_validate_rejects_terminal_mapping_and_persisted_child_drift() -> None:
    _seed_strong_label_versions()
    with SessionLocal.begin() as session:
        terminal = create_label_mapping_version(
            session,
            _ctx("create-terminal"),
            _merge_request(mapping_version="mapping-terminal"),
        )
        drifted = create_label_mapping_version(
            session,
            _ctx("create-drifted"),
            _merge_request(mapping_version="mapping-drifted"),
        )

    with SessionLocal.begin() as session:
        terminal_mapping = session.get(
            LabelMappingVersion,
            terminal["mapping_version_id"],
        )
        assert terminal_mapping is not None
        terminal_mapping.status = "published"

        drifted_item = session.scalar(
            select(LabelMappingItem)
            .where(LabelMappingItem.mapping_version_id == drifted["mapping_version_id"])
            .order_by(LabelMappingItem.source_label_id)
        )
        assert drifted_item is not None
        session.add(
            LabelMappingItemTarget(
                mapping_item_target_id="lmit-intentional-drift",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                mapping_version_id=drifted["mapping_version_id"],
                mapping_item_id=drifted_item.mapping_item_id,
                target_label_version_id=TARGET_VERSION_ID,
                target_label_id="label_extra",
                target_order=1,
                content_sha256="9" * 64,
                trace_id="trace-intentional-drift",
                payload={"intentional_test_drift": True},
            )
        )

    with SessionLocal() as session, pytest.raises(ApiError) as terminal_error:
        validate_label_mapping_version(
            session,
            _ctx("validate-terminal"),
            terminal["mapping_version_id"],
            LabelMappingValidationRequest(expected_resource_version=1),
        )
    assert terminal_error.value.code == "LABEL_MAPPING_TERMINAL_IMMUTABLE"

    with SessionLocal() as session, pytest.raises(ApiError) as drift_error:
        validate_label_mapping_version(
            session,
            _ctx("validate-child-drift"),
            drifted["mapping_version_id"],
            LabelMappingValidationRequest(expected_resource_version=1),
        )
    assert drift_error.value.code == "LABEL_MAPPING_CONTENT_DRIFT"
    assert drift_error.value.status_code == 409


def test_human_admin_approves_validated_edge_and_actor_body_replays_without_events() -> None:
    _seed_strong_label_versions(target_status="published")
    with SessionLocal.begin() as session:
        created = create_label_mapping_version(
            session,
            _ctx("approval-create"),
            _merge_request(),
        )
    mapping_version_id = str(created["mapping_version_id"])
    with SessionLocal.begin() as session:
        validate_label_mapping_version(
            session,
            _ctx("approval-validate"),
            mapping_version_id,
            LabelMappingValidationRequest(expected_resource_version=1),
        )

    request = LabelMappingApprovalRequest(
        expected_resource_version=2,
        reason="人工确认映射定义和统计折叠口径",
    )
    ctx = _ctx("approval-human")
    with SessionLocal.begin() as session:
        first = approve_label_mapping_version(
            session,
            ctx,
            mapping_version_id,
            request,
        )
    with SessionLocal.begin() as session:
        replay = approve_label_mapping_version(
            session,
            ctx,
            mapping_version_id,
            request,
        )
        deduplicated = approve_label_mapping_version(
            session,
            _ctx("approval-human-new-key"),
            mapping_version_id,
            request,
        )
        mapping = session.get(LabelMappingVersion, mapping_version_id)

        assert replay == first
        assert deduplicated["approval_id"] == first["approval_id"]
        assert deduplicated["deduplicated"] is True
        assert mapping is not None
        assert mapping.status == "approved"
        assert mapping.resource_version == 3
        assert mapping.approval_id == first["approval_id"]
        assert mapping.approved_by == "u_mapping_admin"
        assert mapping.approved_at is not None
        assert _scope_count(session, AuditLog) == 3
        assert _scope_count(session, OutboxEvent) == 3


def test_approval_rejects_system_stale_cas_and_child_drift() -> None:
    _seed_strong_label_versions(target_status="published")
    with SessionLocal.begin() as session:
        created = create_label_mapping_version(
            session,
            _ctx("approval-guards-create"),
            _merge_request(),
        )
    mapping_version_id = str(created["mapping_version_id"])
    with SessionLocal.begin() as session:
        validate_label_mapping_version(
            session,
            _ctx("approval-guards-validate"),
            mapping_version_id,
            LabelMappingValidationRequest(expected_resource_version=1),
        )

    system_ctx = _ctx(
        "approval-system",
        user_id="system",
        roles=("system", "project_admin"),
        actor_kind="service",
    )
    with SessionLocal() as session, pytest.raises(ApiError) as system_error:
        approve_label_mapping_version(
            session,
            system_ctx,
            mapping_version_id,
            LabelMappingApprovalRequest(
                expected_resource_version=2,
                reason="系统不能代替自然人审批",
            ),
        )
    assert system_error.value.code == "LABEL_MAPPING_HUMAN_APPROVAL_REQUIRED"

    with SessionLocal() as session, pytest.raises(ApiError) as cas_error:
        approve_label_mapping_version(
            session,
            _ctx("approval-stale-cas"),
            mapping_version_id,
            LabelMappingApprovalRequest(
                expected_resource_version=1,
                reason="陈旧 CAS",
            ),
        )
    assert cas_error.value.code == "RESOURCE_VERSION_CONFLICT"

    with SessionLocal.begin() as session:
        mapping_item = session.scalar(
            select(LabelMappingItem)
            .where(LabelMappingItem.mapping_version_id == mapping_version_id)
            .order_by(LabelMappingItem.source_label_id)
        )
        assert mapping_item is not None
        session.add(
            LabelMappingItemTarget(
                mapping_item_target_id="lmit-approval-intentional-drift",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                mapping_version_id=mapping_version_id,
                mapping_item_id=mapping_item.mapping_item_id,
                target_label_version_id=TARGET_VERSION_ID,
                target_label_id="label_extra",
                target_order=1,
                content_sha256="8" * 64,
                trace_id="trace-approval-intentional-drift",
                payload={"intentional_test_drift": True},
            )
        )

    with SessionLocal() as session, pytest.raises(ApiError) as drift_error:
        approve_label_mapping_version(
            session,
            _ctx("approval-child-drift"),
            mapping_version_id,
            LabelMappingApprovalRequest(
                expected_resource_version=2,
                reason="漂移映射不得审批",
            ),
        )
    assert drift_error.value.code == "LABEL_MAPPING_CONTENT_DRIFT"
    with SessionLocal() as session:
        mapping = session.get(LabelMappingVersion, mapping_version_id)
        assert mapping is not None and mapping.status == "validated"
        assert _scope_count(session, AuditLog) == 2
        assert _scope_count(session, OutboxEvent) == 2


def test_publish_direct_bundle_is_atomic_published_and_content_deduplicated() -> None:
    _seed_strong_label_versions(target_status="published")
    approved = _create_validate_approve_edge(
        _merge_request(),
        key_prefix="direct-bundle",
    )
    mapping_version_id = str(approved["mapping_version_id"])
    request = _bundle_request(
        [mapping_version_id],
        [SOURCE_VERSION_ID],
        TARGET_VERSION_ID,
    )
    ctx = _ctx("publish-direct-bundle")

    with SessionLocal.begin() as session:
        first = publish_label_mapping_bundle(session, ctx, request)
    with SessionLocal.begin() as session:
        replay = publish_label_mapping_bundle(session, ctx, request)
        duplicate = publish_label_mapping_bundle(
            session,
            _ctx("publish-direct-bundle-new-key"),
            request,
        )
        bundle = session.get(LabelMappingBundle, first["mapping_bundle_id"])
        sources = list(
            session.scalars(
                select(LabelMappingBundleSource).where(
                    LabelMappingBundleSource.mapping_bundle_id == first["mapping_bundle_id"]
                )
            )
        )
        members = list(
            session.scalars(
                select(LabelMappingBundleMember).where(
                    LabelMappingBundleMember.mapping_bundle_id == first["mapping_bundle_id"]
                )
            )
        )
        paths = list(
            session.scalars(
                select(LabelMappingBundlePath).where(
                    LabelMappingBundlePath.mapping_bundle_id == first["mapping_bundle_id"]
                )
            )
        )

        assert replay == first
        assert duplicate["mapping_bundle_id"] == first["mapping_bundle_id"]
        assert duplicate["deduplicated"] is True
        assert bundle is not None
        assert bundle.status == "published"
        assert bundle.resource_version == 1
        assert bundle.approved_by == "u_mapping_admin"
        assert bundle.approved_at is not None and bundle.published_at is not None
        assert bundle.canonical_manifest_sha256 == first["canonical_manifest_sha256"]
        assert bundle.payload["canonical_manifest"] == first["canonical_manifest"]
        assert len(sources) == 1
        assert len(members) == 1
        assert len(paths) == 4
        assert {path.target_label_id for path in paths} == {"label_merged"}
        assert {path.reducer for path in paths} == {"presence-any"}
        assert _scope_count(session, LabelMappingBundle) == 1
        assert _scope_count(session, AuditLog) == 4
        assert _scope_count(session, OutboxEvent) == 4
        published_event = session.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.tenant_id == TENANT_ID,
                OutboxEvent.project_id == PROJECT_ID,
                OutboxEvent.event_type == "label_mapping_bundle.published",
            )
            .order_by(OutboxEvent.event_id.desc())
        )
        assert published_event is not None
        assert published_event.payload["resource_version"] == 1
        assert "canonical_manifest" not in published_event.payload["data"]


def test_publish_rejects_system_and_mapping_cas_without_half_artifact() -> None:
    _seed_strong_label_versions(target_status="published")
    approved = _create_validate_approve_edge(
        _merge_request(),
        key_prefix="publish-guards",
    )
    mapping_version_id = str(approved["mapping_version_id"])
    valid_request = _bundle_request(
        [mapping_version_id],
        [SOURCE_VERSION_ID],
        TARGET_VERSION_ID,
    )

    with SessionLocal() as session, pytest.raises(ApiError) as system_error:
        publish_label_mapping_bundle(
            session,
            _ctx(
                "publish-system",
                user_id="system",
                roles=("system", "project_admin"),
                actor_kind="service",
            ),
            valid_request,
        )
    assert system_error.value.code == "LABEL_MAPPING_HUMAN_APPROVAL_REQUIRED"

    stale_request = _bundle_request(
        [mapping_version_id],
        [SOURCE_VERSION_ID],
        TARGET_VERSION_ID,
        expected_mapping_resource_version=2,
    )
    with SessionLocal() as session, pytest.raises(ApiError) as cas_error:
        publish_label_mapping_bundle(
            session,
            _ctx("publish-stale-cas"),
            stale_request,
        )
    assert cas_error.value.code == "RESOURCE_VERSION_CONFLICT"
    with SessionLocal() as session:
        assert _scope_count(session, LabelMappingBundle) == 0
        assert _scope_count(session, LabelMappingBundleSource) == 0
        assert _scope_count(session, LabelMappingBundleMember) == 0
        assert _scope_count(session, LabelMappingBundlePath) == 0
        assert _scope_count(session, AuditLog) == 3
        assert _scope_count(session, OutboxEvent) == 3


def test_publish_revalidates_approved_edge_children_and_has_no_half_artifact() -> None:
    _seed_strong_label_versions(target_status="published")
    approved = _create_validate_approve_edge(
        _merge_request(),
        key_prefix="publish-child-drift",
    )
    mapping_version_id = str(approved["mapping_version_id"])
    with SessionLocal.begin() as session:
        mapping_item = session.scalar(
            select(LabelMappingItem)
            .where(LabelMappingItem.mapping_version_id == mapping_version_id)
            .order_by(LabelMappingItem.source_label_id)
        )
        assert mapping_item is not None
        session.add(
            LabelMappingItemTarget(
                mapping_item_target_id="lmit-publish-intentional-drift",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                mapping_version_id=mapping_version_id,
                mapping_item_id=mapping_item.mapping_item_id,
                target_label_version_id=TARGET_VERSION_ID,
                target_label_id="label_extra",
                target_order=1,
                content_sha256="7" * 64,
                trace_id="trace-publish-intentional-drift",
                payload={"intentional_test_drift": True},
            )
        )

    with SessionLocal() as session, pytest.raises(ApiError) as drift_error:
        publish_label_mapping_bundle(
            session,
            _ctx("publish-revalidate-drift"),
            _bundle_request(
                [mapping_version_id],
                [SOURCE_VERSION_ID],
                TARGET_VERSION_ID,
            ),
        )
    assert drift_error.value.code == "LABEL_MAPPING_CONTENT_DRIFT"
    with SessionLocal() as session:
        assert _scope_count(session, LabelMappingBundle) == 0
        assert _scope_count(session, LabelMappingBundleSource) == 0
        assert _scope_count(session, LabelMappingBundleMember) == 0
        assert _scope_count(session, LabelMappingBundlePath) == 0


def test_publish_multi_source_direct_bundle_freezes_complete_source_set() -> None:
    source_a = "lv_bundle_source_a"
    source_b = "lv_bundle_source_b"
    target = "lv_bundle_target"
    _seed_identity_versions(
        {
            source_a: ("label_a",),
            source_b: ("label_b",),
            target: ("label_a", "label_b"),
        }
    )
    edge_a = _create_validate_approve_edge(
        _identity_edge_request(
            source_a,
            target,
            ("label_a",),
            mapping_version="bundle-source-a-target",
        ),
        key_prefix="bundle-source-a",
    )
    edge_b = _create_validate_approve_edge(
        _identity_edge_request(
            source_b,
            target,
            ("label_b",),
            mapping_version="bundle-source-b-target",
        ),
        key_prefix="bundle-source-b",
    )

    with SessionLocal.begin() as session:
        result = publish_label_mapping_bundle(
            session,
            _ctx("publish-multi-source"),
            _bundle_request(
                [str(edge_b["mapping_version_id"]), str(edge_a["mapping_version_id"])],
                [source_b, source_a],
                target,
            ),
        )
        sources = list(
            session.scalars(
                select(LabelMappingBundleSource)
                .where(LabelMappingBundleSource.mapping_bundle_id == result["mapping_bundle_id"])
                .order_by(LabelMappingBundleSource.source_order)
            )
        )
        assert [source.source_label_version_id for source in sources] == [
            source_a,
            source_b,
        ]
        assert result["source_label_version_ids"] == [source_a, source_b]
        assert result["member_count"] == 2
        assert result["path_count"] == 4


def test_publish_multi_hop_bundle_freezes_ordered_relation_paths() -> None:
    version_1 = "lv_bundle_v1"
    version_2 = "lv_bundle_v2"
    version_3 = "lv_bundle_v3"
    _seed_identity_versions(
        {
            version_1: ("label_a",),
            version_2: ("label_a",),
            version_3: ("label_a",),
        }
    )
    edge_12 = _create_validate_approve_edge(
        _identity_edge_request(
            version_1,
            version_2,
            ("label_a",),
            mapping_version="bundle-v1-v2",
        ),
        key_prefix="bundle-v1-v2",
    )
    edge_23 = _create_validate_approve_edge(
        _identity_edge_request(
            version_2,
            version_3,
            ("label_a",),
            mapping_version="bundle-v2-v3",
        ),
        key_prefix="bundle-v2-v3",
    )
    mapping_ids = [
        str(edge_23["mapping_version_id"]),
        str(edge_12["mapping_version_id"]),
    ]

    with SessionLocal.begin() as session:
        result = publish_label_mapping_bundle(
            session,
            _ctx("publish-multi-hop"),
            _bundle_request(mapping_ids, [version_1], version_3),
        )
        members = list(
            session.scalars(
                select(LabelMappingBundleMember)
                .where(LabelMappingBundleMember.mapping_bundle_id == result["mapping_bundle_id"])
                .order_by(LabelMappingBundleMember.edge_order)
            )
        )
        paths = list(
            session.scalars(
                select(LabelMappingBundlePath).where(
                    LabelMappingBundlePath.mapping_bundle_id == result["mapping_bundle_id"]
                )
            )
        )
        assert [member.mapping_version_id for member in members] == [
            edge_12["mapping_version_id"],
            edge_23["mapping_version_id"],
        ]
        assert len(paths) == 2
        assert all(len(path.relation_path) == 2 for path in paths)
        assert all(
            path.mapping_version_ids
            == [edge_12["mapping_version_id"], edge_23["mapping_version_id"]]
            for path in paths
        )
