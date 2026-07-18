from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, assert_never

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.domain.label_mapping import (
    CompatibilityEvidence,
    CompiledEdge,
    EdgeCompileInput,
    IdentityIntent,
    LabelItemSnapshot,
    LabelVersionSnapshot,
    MappingCompatibility,
    MappingCompileError,
    MappingIntent,
    MergeIntent,
    RenameIntent,
    ReplaceIntent,
    RetireIntent,
    SplitRecomputeIntent,
    compile_edge,
    sha256_document,
)
from app.domain.label_mapping.bundle_compiler import (
    BundleCompileError,
    BundleCompileInput,
    BundleEdgeSnapshot,
    CompiledBundle,
    compile_bundle,
)
from app.models import (
    LabelMappingBundle,
    LabelMappingBundleMember,
    LabelMappingBundlePath,
    LabelMappingBundleSource,
    LabelMappingItem,
    LabelMappingItemTarget,
    LabelMappingVersion,
    LabelVersion,
    LabelVersionItem,
)
from app.repositories.label_mappings import (
    find_bundle_by_manifest_sha256,
    find_mapping_by_content_sha256,
    find_mapping_by_pair_and_version,
    get_label_versions,
    get_mapping_version,
    get_mapping_versions,
    list_bundle_members,
    list_bundle_paths,
    list_bundle_sources,
    list_label_version_items,
    list_mapping_items,
    list_mapping_targets,
)
from app.schemas.label_mapping import (
    CompatibilityEvidenceRequest,
    IdentityMappingItemRequest,
    LabelMappingApprovalRequest,
    LabelMappingBundlePublishRequest,
    LabelMappingCreateRequest,
    LabelMappingItemRequest,
    LabelMappingValidationRequest,
    MergeMappingItemRequest,
    RenameMappingItemRequest,
    ReplaceMappingItemRequest,
    RetireMappingItemRequest,
    SplitRecomputeMappingItemRequest,
)
from app.services.audit_service import record_audit
from app.services.idempotency_service import replay_or_conflict, save_idempotency_result
from app.services.outbox_service import enqueue_event

CREATE_OPERATION = "label_mapping_versions.create"
VALIDATE_OPERATION = "label_mapping_versions.validate"
APPROVE_OPERATION = "label_mapping_versions.approve"
PUBLISH_BUNDLE_OPERATION = "label_mapping_bundles.publish"
TERMINAL_MAPPING_STATUSES = frozenset({"published", "superseded", "archived"})
READ_ONLY_VALIDATED_STATUSES = frozenset({"validated", "review_required", "approved"})


def _request_document(request: LabelMappingCreateRequest) -> dict[str, Any]:
    return request.model_dump(mode="json", exclude_none=False)


def _request_hash(operation: str, body: dict[str, Any]) -> str:
    return sha256_document(
        {
            "body": body,
            "operation": operation,
            "schema_version": "auris.label-mapping-operation/1",
        }
    )


def _utc_iso(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat()


def _validation_hash(
    mapping_version_id: str,
    request: LabelMappingValidationRequest,
) -> str:
    return _request_hash(
        VALIDATE_OPERATION,
        {
            "mapping_version_id": mapping_version_id,
            "request": request.model_dump(mode="json", exclude_none=False),
        },
    )


def _compile_error(error: MappingCompileError) -> ApiError:
    conflict_codes = {
        "LABEL_MAPPING_RESOURCE_VERSION_CONFLICT",
        "LABEL_MAPPING_SCOPE_MISMATCH",
        "LABEL_MAPPING_SOURCE_NOT_PUBLISHED",
        "LABEL_MAPPING_TARGET_STATUS_INVALID",
        "LABEL_MAPPING_TAXONOMY_MISMATCH",
        "LABEL_MAPPING_SEMANTIC_HASH_CHANGED",
        "LABEL_VERSION_ITEM_HASH_DRIFT",
    }
    is_authoritative_content_error = bool(
        error.path
        and error.path.startswith(("source.", "target."))
        and error.code == "LABEL_MAPPING_INPUT_INVALID"
    )
    status_code = 409 if error.code in conflict_codes or is_authoritative_content_error else 422
    detail: dict[str, Any] = dict(error.details)
    if error.path is not None:
        detail["path"] = error.path
    return ApiError(
        error.code,
        str(error),
        status_code,
        details=[detail] if detail else [],
    )


def _require_strong_version(version: LabelVersion, *, role: str) -> None:
    missing_fields: list[str] = []
    if not isinstance(version.taxonomy_id, str) or not version.taxonomy_id:
        missing_fields.append("taxonomy_id")
    if not isinstance(version.artifact_status, str) or not version.artifact_status:
        missing_fields.append("artifact_status")
    if not isinstance(version.content_sha256, str) or len(version.content_sha256) != 64:
        missing_fields.append("content_sha256")
    if not isinstance(version.resource_version, int) or version.resource_version <= 0:
        missing_fields.append("resource_version")
    if missing_fields:
        raise ApiError(
            "LABEL_VERSION_MIGRATION_REQUIRED",
            f"{role} 标签版本尚未形成可冻结的强版本",
            409,
            details=[
                {
                    "label_version_id": version.label_version_id,
                    "missing_or_invalid_fields": missing_fields,
                    "role": role,
                }
            ],
        )


def _json_string_list(
    value: object,
    *,
    label_version_id: str,
    label_id: str,
    field: str,
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ApiError(
            "LABEL_VERSION_MIGRATION_REQUIRED",
            "标签版本子项尚未形成可编译的强结构",
            409,
            details=[
                {
                    "field": field,
                    "label_id": label_id,
                    "label_version_id": label_version_id,
                }
            ],
        )
    return tuple(value)


def _item_snapshot(item: LabelVersionItem) -> LabelItemSnapshot:
    aliases = _json_string_list(
        item.aliases,
        label_version_id=item.label_version_id,
        label_id=item.label_id,
        field="aliases",
    )
    parent_ids = _json_string_list(
        item.parent_ids,
        label_version_id=item.label_version_id,
        label_id=item.label_id,
        field="parent_ids",
    )
    if not isinstance(item.aggregation_rule, dict):
        raise ApiError(
            "LABEL_VERSION_MIGRATION_REQUIRED",
            "标签版本子项尚未形成可编译的强结构",
            409,
            details=[
                {
                    "field": "aggregation_rule",
                    "label_id": item.label_id,
                    "label_version_id": item.label_version_id,
                }
            ],
        )
    return LabelItemSnapshot(
        label_id=item.label_id,
        canonical_name=item.canonical_name,
        aliases=aliases,
        value_type=item.value_type,
        risk_level=item.risk_level,
        mutual_exclusion_group=item.mutual_exclusion_group,
        parent_ids=parent_ids,
        aggregation_rule=dict(item.aggregation_rule),
        status=item.status,
        definition_sha256=item.definition_sha256,
    )


def _version_snapshot(
    version: LabelVersion,
    items: list[LabelVersionItem],
    *,
    role: str,
) -> LabelVersionSnapshot:
    _require_strong_version(version, role=role)
    assert version.taxonomy_id is not None
    assert version.artifact_status is not None
    assert version.content_sha256 is not None
    return LabelVersionSnapshot(
        tenant_id=version.tenant_id,
        project_id=version.project_id,
        taxonomy_id=version.taxonomy_id,
        label_version_id=version.label_version_id,
        resource_version=version.resource_version,
        artifact_status=version.artifact_status,
        content_sha256=version.content_sha256,
        items=tuple(_item_snapshot(item) for item in items),
    )


def _evidence(
    request: CompatibilityEvidenceRequest | None,
) -> CompatibilityEvidence | None:
    if request is None:
        return None
    return CompatibilityEvidence(
        evidence_type=request.evidence_type,
        evidence_id=request.evidence_id,
        resource_version=request.resource_version,
        content_sha256=request.content_sha256,
    )


def _mapping_intent(item: LabelMappingItemRequest) -> MappingIntent:
    if isinstance(item, IdentityMappingItemRequest):
        return IdentityIntent(
            source_label_id=item.source_label_id,
            target_label_id=item.target_label_id,
            source_semantic_sha256=item.source_semantic_sha256,
            target_semantic_sha256=item.target_semantic_sha256,
        )
    if isinstance(item, RenameMappingItemRequest):
        return RenameIntent(
            source_label_id=item.source_label_id,
            target_label_id=item.target_label_id,
            source_semantic_sha256=item.source_semantic_sha256,
            target_semantic_sha256=item.target_semantic_sha256,
        )
    if isinstance(item, ReplaceMappingItemRequest):
        return ReplaceIntent(
            source_label_id=item.source_label_id,
            target_label_id=item.target_label_id,
            compatibility=MappingCompatibility(item.compatibility),
            compatibility_evidence=_evidence(item.compatibility_evidence),
        )
    if isinstance(item, MergeMappingItemRequest):
        return MergeIntent(
            source_label_ids=tuple(item.source_label_ids),
            target_label_id=item.target_label_id,
            allowed_metric_families=tuple(item.allowed_metric_families),
            metric_grain=item.metric_grain,
            lineage_key=item.lineage_key,
            reducer=item.reducer,
        )
    if isinstance(item, RetireMappingItemRequest):
        return RetireIntent(
            source_label_id=item.source_label_id,
            target_label_id=item.target_label_id,
        )
    if isinstance(item, SplitRecomputeMappingItemRequest):
        return SplitRecomputeIntent(
            source_label_id=item.source_label_id,
            target_label_ids=tuple(item.target_label_ids),
            requires_recompute=item.requires_recompute,
            allocation_weights=(
                tuple(item.allocation_weights) if item.allocation_weights is not None else None
            ),
            copy_existing_facts=item.copy_existing_facts,
        )
    assert_never(item)


def _compile_from_request(
    session: Session,
    ctx: RequestContext,
    request: LabelMappingCreateRequest,
    *,
    for_update: bool,
) -> CompiledEdge:
    versions = get_label_versions(
        session,
        ctx,
        (request.source_label_version_id, request.target_label_version_id),
        for_update=for_update,
    )
    source = versions.get(request.source_label_version_id)
    target = versions.get(request.target_label_version_id)
    if source is None or target is None:
        missing_ids = sorted(
            {
                request.source_label_version_id,
                request.target_label_version_id,
            }
            - set(versions)
        )
        raise ApiError(
            "LABEL_VERSION_NOT_FOUND",
            "源或目标标签版本不存在于当前租户项目范围",
            404,
            details=[{"missing_label_version_ids": missing_ids}],
        )

    source_items = list_label_version_items(
        session,
        ctx,
        source.label_version_id,
        for_update=for_update,
    )
    target_items = list_label_version_items(
        session,
        ctx,
        target.label_version_id,
        for_update=for_update,
    )
    compile_input = EdgeCompileInput(
        mapping_version=request.mapping_version,
        source=_version_snapshot(source, source_items, role="source"),
        target=_version_snapshot(target, target_items, role="target"),
        expected_source_resource_version=request.expected_source_resource_version,
        expected_target_resource_version=request.expected_target_resource_version,
        dispositions=tuple(_mapping_intent(item) for item in request.items),
    )
    try:
        return compile_edge(compile_input)
    except MappingCompileError as error:
        raise _compile_error(error) from error


def _compiled_result(compiled: CompiledEdge) -> dict[str, Any]:
    return {
        "persisted": False,
        "mapping_version": compiled.mapping_version,
        "compiler_version": compiled.compiler_version,
        "metric_registry_version": compiled.metric_registry_version,
        "source_label_version_id": compiled.source_label_version_id,
        "target_label_version_id": compiled.target_label_version_id,
        "source_resource_version": compiled.source_resource_version,
        "target_resource_version": compiled.target_resource_version,
        "content_sha256": compiled.content_sha256,
        "canonical_manifest_sha256": compiled.content_sha256,
        "coverage": compiled.coverage.to_document(),
        "items": [item.to_document() for item in compiled.items],
        "canonical_manifest": compiled.canonical_manifest,
    }


def dry_run_label_mapping_edge(
    session: Session,
    ctx: RequestContext,
    request: LabelMappingCreateRequest,
) -> dict[str, Any]:
    """Compile one edge against authoritative scoped versions without writes."""

    return _compiled_result(_compile_from_request(session, ctx, request, for_update=False))


def _deterministic_id(prefix: str, document: dict[str, Any]) -> str:
    return f"{prefix}{sha256_document(document)}"


def _mapping_id(ctx: RequestContext, compiled: CompiledEdge) -> str:
    return _deterministic_id(
        "lmv_",
        {
            "content_sha256": compiled.content_sha256,
            "project_id": ctx.project_id,
            "tenant_id": ctx.tenant_id,
        },
    )


def _mapping_item_id(
    ctx: RequestContext,
    mapping_version_id: str,
    source_label_id: str,
) -> str:
    return _deterministic_id(
        "lmi_",
        {
            "mapping_version_id": mapping_version_id,
            "project_id": ctx.project_id,
            "source_label_id": source_label_id,
            "tenant_id": ctx.tenant_id,
        },
    )


def _mapping_target_id(
    ctx: RequestContext,
    mapping_version_id: str,
    mapping_item_id: str,
    target_label_id: str,
    target_order: int,
) -> str:
    return _deterministic_id(
        "lmit_",
        {
            "mapping_item_id": mapping_item_id,
            "mapping_version_id": mapping_version_id,
            "project_id": ctx.project_id,
            "target_label_id": target_label_id,
            "target_order": target_order,
            "tenant_id": ctx.tenant_id,
        },
    )


def _mapping_payload(
    request: LabelMappingCreateRequest,
    compiled: CompiledEdge,
) -> dict[str, Any]:
    return {
        "schema_version": "auris.label-mapping-version/1",
        "compile_request": _request_document(request),
        "canonical_manifest": compiled.canonical_manifest,
        "compiler_version": compiled.compiler_version,
        "metric_registry_version": compiled.metric_registry_version,
        "coverage": compiled.coverage.to_document(),
    }


def _build_mapping_rows(
    ctx: RequestContext,
    request: LabelMappingCreateRequest,
    compiled: CompiledEdge,
) -> tuple[
    LabelMappingVersion,
    list[LabelMappingItem],
    list[LabelMappingItemTarget],
]:
    mapping_version_id = _mapping_id(ctx, compiled)
    mapping = LabelMappingVersion(
        mapping_version_id=mapping_version_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        source_label_version_id=compiled.source_label_version_id,
        target_label_version_id=compiled.target_label_version_id,
        mapping_version=compiled.mapping_version,
        status="draft",
        source_resource_version=compiled.source_resource_version,
        target_resource_version=compiled.target_resource_version,
        resource_version=1,
        content_sha256=compiled.content_sha256,
        root_trace_id=ctx.trace_id,
        trace_id=ctx.trace_id,
        payload=_mapping_payload(request, compiled),
    )
    mapping_items: list[LabelMappingItem] = []
    mapping_targets: list[LabelMappingItemTarget] = []
    for compiled_item in compiled.items:
        mapping_item_id = _mapping_item_id(
            ctx,
            mapping_version_id,
            compiled_item.source_label_id,
        )
        mapping_items.append(
            LabelMappingItem(
                mapping_item_id=mapping_item_id,
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                mapping_version_id=mapping_version_id,
                source_label_version_id=compiled.source_label_version_id,
                target_label_version_id=compiled.target_label_version_id,
                source_label_id=compiled_item.source_label_id,
                relation=compiled_item.relation.value,
                compatibility=compiled_item.compatibility.value,
                comparability_status=compiled_item.comparability_status.value,
                allowed_metric_families=list(compiled_item.allowed_metric_families),
                metric_grain=compiled_item.metric_grain,
                lineage_key=compiled_item.lineage_key,
                reducer=compiled_item.reducer,
                requires_recompute=compiled_item.requires_recompute,
                source_semantic_sha256=compiled_item.source_semantic_sha256,
                target_semantic_sha256=compiled_item.target_semantic_sha256,
                compatibility_evidence_ref=(
                    compiled_item.compatibility_evidence.to_document()
                    if compiled_item.compatibility_evidence is not None
                    else None
                ),
                content_sha256=compiled_item.content_sha256,
                trace_id=ctx.trace_id,
                payload={
                    "schema_version": "auris.label-mapping-item/1",
                    "source_definition_sha256": compiled_item.source_definition_sha256,
                    "merge_group_sha256": compiled_item.merge_group_sha256,
                },
            )
        )
        for target in compiled_item.targets:
            mapping_targets.append(
                LabelMappingItemTarget(
                    mapping_item_target_id=_mapping_target_id(
                        ctx,
                        mapping_version_id,
                        mapping_item_id,
                        target.target_label_id,
                        target.target_order,
                    ),
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    mapping_version_id=mapping_version_id,
                    mapping_item_id=mapping_item_id,
                    target_label_version_id=compiled.target_label_version_id,
                    target_label_id=target.target_label_id,
                    target_order=target.target_order,
                    content_sha256=target.content_sha256,
                    trace_id=ctx.trace_id,
                    payload={
                        "schema_version": "auris.label-mapping-item-target/1",
                        "definition_sha256": target.definition_sha256,
                        "semantic_sha256": target.semantic_sha256,
                    },
                )
            )
    return mapping, mapping_items, mapping_targets


def _event_summary(
    mapping: LabelMappingVersion,
    compiled: CompiledEdge,
    *,
    item_count: int,
    target_count: int,
) -> dict[str, Any]:
    return {
        "mapping_version_id": mapping.mapping_version_id,
        "mapping_version": mapping.mapping_version,
        "status": mapping.status,
        "resource_version": mapping.resource_version,
        "source_label_version_id": mapping.source_label_version_id,
        "target_label_version_id": mapping.target_label_version_id,
        "source_resource_version": mapping.source_resource_version,
        "target_resource_version": mapping.target_resource_version,
        "content_sha256": mapping.content_sha256,
        "compiler_version": compiled.compiler_version,
        "metric_registry_version": compiled.metric_registry_version,
        "item_count": item_count,
        "target_count": target_count,
        "coverage": compiled.coverage.to_document(),
    }


def _creation_event_ids(mapping: LabelMappingVersion) -> tuple[int, int]:
    audit_id = mapping.payload.get("creation_audit_id")
    outbox_event_id = mapping.payload.get("creation_outbox_event_id")
    if not isinstance(audit_id, int) or not isinstance(outbox_event_id, int):
        raise ApiError(
            "LABEL_MAPPING_CONTENT_DRIFT",
            "映射版本缺少冻结的创建审计引用",
            409,
            details=[{"mapping_version_id": mapping.mapping_version_id}],
        )
    return audit_id, outbox_event_id


def _create_response(
    mapping: LabelMappingVersion,
    compiled: CompiledEdge,
    *,
    deduplicated: bool,
) -> dict[str, Any]:
    audit_id, outbox_event_id = _creation_event_ids(mapping)
    return {
        **_compiled_result(compiled),
        "persisted": True,
        "mapping_version_id": mapping.mapping_version_id,
        "status": mapping.status,
        "resource_version": mapping.resource_version,
        "deduplicated": deduplicated,
        "audit_id": audit_id,
        "outbox_event_id": outbox_event_id,
        "trace_id": mapping.trace_id,
    }


def _save_create_result(
    session: Session,
    ctx: RequestContext,
    *,
    body_hash: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    save_idempotency_result(
        session,
        ctx,
        operation=CREATE_OPERATION,
        body_hash=body_hash,
        status_code=201,
        response_json=response,
    )
    return response


def create_label_mapping_version(
    session: Session,
    ctx: RequestContext,
    request: LabelMappingCreateRequest,
) -> dict[str, Any]:
    """Server-compile and freeze a draft mapping parent/items/targets atomically."""

    request_document = _request_document(request)
    body_hash = _request_hash(CREATE_OPERATION, request_document)
    replay = replay_or_conflict(
        session,
        ctx,
        operation=CREATE_OPERATION,
        body_hash=body_hash,
    )
    if replay is not None:
        return replay

    compiled = _compile_from_request(session, ctx, request, for_update=True)
    same_version = find_mapping_by_pair_and_version(
        session,
        ctx,
        source_label_version_id=compiled.source_label_version_id,
        target_label_version_id=compiled.target_label_version_id,
        mapping_version=compiled.mapping_version,
        for_update=True,
    )
    if same_version is not None:
        if same_version.content_sha256 != compiled.content_sha256:
            raise ApiError(
                "LABEL_MAPPING_VERSION_CONFLICT",
                "同一源目标版本对的 mapping_version 已绑定不同规范内容",
                409,
                details=[
                    {
                        "actual_content_sha256": compiled.content_sha256,
                        "existing_content_sha256": same_version.content_sha256,
                        "existing_mapping_version_id": same_version.mapping_version_id,
                        "mapping_version": compiled.mapping_version,
                    }
                ],
            )
        return _save_create_result(
            session,
            ctx,
            body_hash=body_hash,
            response=_create_response(same_version, compiled, deduplicated=True),
        )

    same_content = find_mapping_by_content_sha256(
        session,
        ctx,
        compiled.content_sha256,
        for_update=True,
    )
    if same_content is not None:
        return _save_create_result(
            session,
            ctx,
            body_hash=body_hash,
            response=_create_response(same_content, compiled, deduplicated=True),
        )

    mapping, mapping_items, mapping_targets = _build_mapping_rows(ctx, request, compiled)
    session.add(mapping)
    session.flush()
    session.add_all(mapping_items)
    session.flush()
    session.add_all(mapping_targets)
    session.flush()

    summary = _event_summary(
        mapping,
        compiled,
        item_count=len(mapping_items),
        target_count=len(mapping_targets),
    )
    audit = record_audit(
        session,
        ctx,
        action="label_mapping_version.created",
        object_type="label_mapping_version",
        object_id=mapping.mapping_version_id,
        after=summary,
    )
    outbox_event = enqueue_event(
        session,
        ctx,
        event_type="label_mapping_version.created",
        aggregate_type="label_mapping_version",
        aggregate_id=mapping.mapping_version_id,
        payload=summary,
    )
    session.flush()
    mapping.payload = {
        **mapping.payload,
        "creation_audit_id": audit.audit_id,
        "creation_outbox_event_id": outbox_event.event_id,
    }
    session.flush()
    return _save_create_result(
        session,
        ctx,
        body_hash=body_hash,
        response=_create_response(mapping, compiled, deduplicated=False),
    )


def _frozen_compile_request(mapping: LabelMappingVersion) -> LabelMappingCreateRequest:
    raw_request = mapping.payload.get("compile_request")
    if not isinstance(raw_request, dict):
        raise ApiError(
            "LABEL_MAPPING_CONTENT_DRIFT",
            "映射版本缺少可重编译的冻结请求",
            409,
            details=[{"mapping_version_id": mapping.mapping_version_id}],
        )
    try:
        request = LabelMappingCreateRequest.model_validate(raw_request)
    except ValidationError as error:
        raise ApiError(
            "LABEL_MAPPING_CONTENT_DRIFT",
            "映射版本冻结请求不再符合强契约",
            409,
            details=[
                {
                    "mapping_version_id": mapping.mapping_version_id,
                    "validation_errors": error.errors(include_url=False),
                }
            ],
        ) from error
    binding = (
        request.mapping_version,
        request.source_label_version_id,
        request.target_label_version_id,
        request.expected_source_resource_version,
        request.expected_target_resource_version,
    )
    expected_binding = (
        mapping.mapping_version,
        mapping.source_label_version_id,
        mapping.target_label_version_id,
        mapping.source_resource_version,
        mapping.target_resource_version,
    )
    if binding != expected_binding:
        raise ApiError(
            "LABEL_MAPPING_CONTENT_DRIFT",
            "映射版本冻结请求与父记录绑定不一致",
            409,
            details=[{"mapping_version_id": mapping.mapping_version_id}],
        )
    return request


def _expected_item_projection(
    ctx: RequestContext,
    mapping: LabelMappingVersion,
    compiled: CompiledEdge,
) -> list[dict[str, Any]]:
    projection: list[dict[str, Any]] = []
    for item in compiled.items:
        mapping_item_id = _mapping_item_id(
            ctx,
            mapping.mapping_version_id,
            item.source_label_id,
        )
        projection.append(
            {
                "mapping_item_id": mapping_item_id,
                "mapping_version_id": mapping.mapping_version_id,
                "source_label_version_id": compiled.source_label_version_id,
                "target_label_version_id": compiled.target_label_version_id,
                "source_label_id": item.source_label_id,
                "relation": item.relation.value,
                "compatibility": item.compatibility.value,
                "comparability_status": item.comparability_status.value,
                "allowed_metric_families": list(item.allowed_metric_families),
                "metric_grain": item.metric_grain,
                "lineage_key": item.lineage_key,
                "reducer": item.reducer,
                "requires_recompute": item.requires_recompute,
                "source_semantic_sha256": item.source_semantic_sha256,
                "target_semantic_sha256": item.target_semantic_sha256,
                "compatibility_evidence_ref": (
                    item.compatibility_evidence.to_document()
                    if item.compatibility_evidence is not None
                    else None
                ),
                "content_sha256": item.content_sha256,
                "payload": {
                    "schema_version": "auris.label-mapping-item/1",
                    "source_definition_sha256": item.source_definition_sha256,
                    "merge_group_sha256": item.merge_group_sha256,
                },
                "targets": [
                    {
                        "mapping_item_target_id": _mapping_target_id(
                            ctx,
                            mapping.mapping_version_id,
                            mapping_item_id,
                            target.target_label_id,
                            target.target_order,
                        ),
                        "mapping_version_id": mapping.mapping_version_id,
                        "mapping_item_id": mapping_item_id,
                        "target_label_version_id": compiled.target_label_version_id,
                        "target_label_id": target.target_label_id,
                        "target_order": target.target_order,
                        "content_sha256": target.content_sha256,
                        "payload": {
                            "schema_version": "auris.label-mapping-item-target/1",
                            "definition_sha256": target.definition_sha256,
                            "semantic_sha256": target.semantic_sha256,
                        },
                    }
                    for target in item.targets
                ],
            }
        )
    return projection


def _actual_item_projection(
    items: list[LabelMappingItem],
    targets: list[LabelMappingItemTarget],
) -> list[dict[str, Any]]:
    targets_by_item: dict[str, list[LabelMappingItemTarget]] = {}
    for target in targets:
        targets_by_item.setdefault(target.mapping_item_id, []).append(target)
    projection: list[dict[str, Any]] = []
    for item in items:
        item_targets = sorted(
            targets_by_item.get(item.mapping_item_id, []),
            key=lambda target: target.target_order,
        )
        projection.append(
            {
                "mapping_item_id": item.mapping_item_id,
                "mapping_version_id": item.mapping_version_id,
                "source_label_version_id": item.source_label_version_id,
                "target_label_version_id": item.target_label_version_id,
                "source_label_id": item.source_label_id,
                "relation": item.relation,
                "compatibility": item.compatibility,
                "comparability_status": item.comparability_status,
                "allowed_metric_families": item.allowed_metric_families,
                "metric_grain": item.metric_grain,
                "lineage_key": item.lineage_key,
                "reducer": item.reducer,
                "requires_recompute": item.requires_recompute,
                "source_semantic_sha256": item.source_semantic_sha256,
                "target_semantic_sha256": item.target_semantic_sha256,
                "compatibility_evidence_ref": item.compatibility_evidence_ref,
                "content_sha256": item.content_sha256,
                "payload": item.payload,
                "targets": [
                    {
                        "mapping_item_target_id": target.mapping_item_target_id,
                        "mapping_version_id": target.mapping_version_id,
                        "mapping_item_id": target.mapping_item_id,
                        "target_label_version_id": target.target_label_version_id,
                        "target_label_id": target.target_label_id,
                        "target_order": target.target_order,
                        "content_sha256": target.content_sha256,
                        "payload": target.payload,
                    }
                    for target in item_targets
                ],
            }
        )
    return projection


def _assert_persisted_content(
    session: Session,
    ctx: RequestContext,
    mapping: LabelMappingVersion,
    compiled: CompiledEdge,
) -> tuple[int, int]:
    if compiled.content_sha256 != mapping.content_sha256:
        raise ApiError(
            "LABEL_MAPPING_CONTENT_DRIFT",
            "服务端重编译结果与冻结父记录不一致",
            409,
            details=[
                {
                    "actual_content_sha256": compiled.content_sha256,
                    "expected_content_sha256": mapping.content_sha256,
                    "mapping_version_id": mapping.mapping_version_id,
                }
            ],
        )
    items = list_mapping_items(
        session,
        ctx,
        mapping.mapping_version_id,
        for_update=True,
    )
    targets = list_mapping_targets(
        session,
        ctx,
        mapping.mapping_version_id,
        for_update=True,
    )
    expected = _expected_item_projection(ctx, mapping, compiled)
    actual = _actual_item_projection(items, targets)
    if expected != actual:
        expected_sources = [item["source_label_id"] for item in expected]
        actual_sources = [item["source_label_id"] for item in actual]
        raise ApiError(
            "LABEL_MAPPING_CONTENT_DRIFT",
            "冻结映射子项或目标与服务端重编译结果不一致",
            409,
            details=[
                {
                    "actual_item_count": len(items),
                    "actual_source_label_ids": actual_sources,
                    "actual_target_count": len(targets),
                    "expected_item_count": len(expected),
                    "expected_source_label_ids": expected_sources,
                    "expected_target_count": sum(len(item["targets"]) for item in expected),
                    "mapping_version_id": mapping.mapping_version_id,
                }
            ],
        )
    return len(items), len(targets)


def _validation_response(
    mapping: LabelMappingVersion,
    compiled: CompiledEdge,
    *,
    audit_id: int,
    outbox_event_id: int,
    already_validated: bool,
) -> dict[str, Any]:
    return {
        "mapping_version_id": mapping.mapping_version_id,
        "mapping_version": mapping.mapping_version,
        "status": mapping.status,
        "resource_version": mapping.resource_version,
        "content_sha256": mapping.content_sha256,
        "compiler_version": compiled.compiler_version,
        "metric_registry_version": compiled.metric_registry_version,
        "coverage": compiled.coverage.to_document(),
        "already_validated": already_validated,
        "audit_id": audit_id,
        "outbox_event_id": outbox_event_id,
        "trace_id": mapping.trace_id,
    }


def _save_validation_result(
    session: Session,
    ctx: RequestContext,
    *,
    body_hash: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    save_idempotency_result(
        session,
        ctx,
        operation=VALIDATE_OPERATION,
        body_hash=body_hash,
        status_code=200,
        response_json=response,
    )
    return response


def validate_label_mapping_version(
    session: Session,
    ctx: RequestContext,
    mapping_version_id: str,
    request: LabelMappingValidationRequest,
) -> dict[str, Any]:
    """Recompile a frozen draft and validate both parent and append-only children."""

    body_hash = _validation_hash(mapping_version_id, request)
    replay = replay_or_conflict(
        session,
        ctx,
        operation=VALIDATE_OPERATION,
        body_hash=body_hash,
    )
    if replay is not None:
        return replay

    mapping = get_mapping_version(
        session,
        ctx,
        mapping_version_id,
        for_update=True,
    )
    if mapping is None:
        raise ApiError(
            "LABEL_MAPPING_VERSION_NOT_FOUND",
            "标签映射版本不存在于当前租户项目范围",
            404,
        )
    if mapping.status in TERMINAL_MAPPING_STATUSES:
        raise ApiError(
            "LABEL_MAPPING_TERMINAL_IMMUTABLE",
            "已发布、被取代或归档的映射版本不可重新验证",
            409,
            details=[
                {
                    "mapping_version_id": mapping.mapping_version_id,
                    "status": mapping.status,
                }
            ],
        )
    if request.expected_resource_version != mapping.resource_version:
        raise ApiError(
            "RESOURCE_VERSION_CONFLICT",
            "标签映射版本 resource_version 已变化",
            409,
            details=[
                {
                    "actual_resource_version": mapping.resource_version,
                    "expected_resource_version": request.expected_resource_version,
                    "mapping_version_id": mapping.mapping_version_id,
                }
            ],
        )

    compile_request = _frozen_compile_request(mapping)
    compiled = _compile_from_request(
        session,
        ctx,
        compile_request,
        for_update=True,
    )
    item_count, target_count = _assert_persisted_content(
        session,
        ctx,
        mapping,
        compiled,
    )

    if mapping.status in READ_ONLY_VALIDATED_STATUSES:
        audit_id = mapping.payload.get("last_validation_audit_id")
        outbox_event_id = mapping.payload.get("last_validation_outbox_event_id")
        if not isinstance(audit_id, int) or not isinstance(outbox_event_id, int):
            raise ApiError(
                "LABEL_MAPPING_CONTENT_DRIFT",
                "已验证映射缺少验证审计引用",
                409,
                details=[{"mapping_version_id": mapping.mapping_version_id}],
            )
        return _save_validation_result(
            session,
            ctx,
            body_hash=body_hash,
            response=_validation_response(
                mapping,
                compiled,
                audit_id=audit_id,
                outbox_event_id=outbox_event_id,
                already_validated=True,
            ),
        )
    if mapping.status != "draft":
        raise ApiError(
            "LABEL_MAPPING_STATUS_INVALID",
            "只有草稿映射可以进入 validated 状态",
            409,
            details=[{"actual_status": mapping.status}],
        )

    before = {
        "status": mapping.status,
        "resource_version": mapping.resource_version,
        "content_sha256": mapping.content_sha256,
    }
    mapping.status = "validated"
    mapping.resource_version += 1
    mapping.trace_id = ctx.trace_id
    summary = _event_summary(
        mapping,
        compiled,
        item_count=item_count,
        target_count=target_count,
    )
    audit = record_audit(
        session,
        ctx,
        action="label_mapping_version.validated",
        object_type="label_mapping_version",
        object_id=mapping.mapping_version_id,
        before=before,
        after=summary,
    )
    outbox_event = enqueue_event(
        session,
        ctx,
        event_type="label_mapping_version.validated",
        aggregate_type="label_mapping_version",
        aggregate_id=mapping.mapping_version_id,
        payload=summary,
    )
    session.flush()
    mapping.payload = {
        **mapping.payload,
        "last_validation_audit_id": audit.audit_id,
        "last_validation_outbox_event_id": outbox_event.event_id,
    }
    session.flush()
    return _save_validation_result(
        session,
        ctx,
        body_hash=body_hash,
        response=_validation_response(
            mapping,
            compiled,
            audit_id=audit.audit_id,
            outbox_event_id=outbox_event.event_id,
            already_validated=False,
        ),
    )


def _require_human_project_admin(ctx: RequestContext) -> None:
    if (
        ctx.actor_kind != "human"
        or ctx.user_id == "system"
        or "system" in ctx.roles
        or "project_admin" not in ctx.roles
    ):
        raise ApiError(
            "LABEL_MAPPING_HUMAN_APPROVAL_REQUIRED",
            "映射审批与 Bundle 发布只能由自然人项目管理员执行",
            403,
        )


def _approval_body_hash(
    ctx: RequestContext,
    mapping_version_id: str,
    request: LabelMappingApprovalRequest,
) -> str:
    return _request_hash(
        APPROVE_OPERATION,
        {
            "actor_id": ctx.user_id,
            "mapping_version_id": mapping_version_id,
            "request": request.model_dump(mode="json", exclude_none=False),
        },
    )


def _approval_id(
    ctx: RequestContext,
    mapping: LabelMappingVersion,
    approval_body_hash: str,
) -> str:
    return _deterministic_id(
        "lma_",
        {
            "approval_body_hash": approval_body_hash,
            "approved_by": ctx.user_id,
            "content_sha256": mapping.content_sha256,
            "mapping_version_id": mapping.mapping_version_id,
            "project_id": ctx.project_id,
            "tenant_id": ctx.tenant_id,
        },
    )


def _approval_event_ids(mapping: LabelMappingVersion) -> tuple[int, int]:
    audit_id = mapping.payload.get("approval_audit_id")
    outbox_event_id = mapping.payload.get("approval_outbox_event_id")
    if not isinstance(audit_id, int) or not isinstance(outbox_event_id, int):
        raise ApiError(
            "LABEL_MAPPING_CONTENT_DRIFT",
            "已审批映射缺少冻结的审批审计引用",
            409,
            details=[{"mapping_version_id": mapping.mapping_version_id}],
        )
    return audit_id, outbox_event_id


def _approval_response(
    mapping: LabelMappingVersion,
    *,
    deduplicated: bool,
) -> dict[str, Any]:
    audit_id, outbox_event_id = _approval_event_ids(mapping)
    if mapping.approval_id is None or mapping.approved_by is None or mapping.approved_at is None:
        raise ApiError(
            "LABEL_MAPPING_CONTENT_DRIFT",
            "approved 映射缺少强审批绑定",
            409,
            details=[{"mapping_version_id": mapping.mapping_version_id}],
        )
    return {
        "mapping_version_id": mapping.mapping_version_id,
        "status": mapping.status,
        "resource_version": mapping.resource_version,
        "content_sha256": mapping.content_sha256,
        "approval_id": mapping.approval_id,
        "approved_by": mapping.approved_by,
        "approved_at": _utc_iso(mapping.approved_at),
        "deduplicated": deduplicated,
        "audit_id": audit_id,
        "outbox_event_id": outbox_event_id,
        "trace_id": mapping.trace_id,
    }


def _save_approval_result(
    session: Session,
    ctx: RequestContext,
    *,
    body_hash: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    save_idempotency_result(
        session,
        ctx,
        operation=APPROVE_OPERATION,
        body_hash=body_hash,
        status_code=200,
        response_json=response,
    )
    return response


def approve_label_mapping_version(
    session: Session,
    ctx: RequestContext,
    mapping_version_id: str,
    request: LabelMappingApprovalRequest,
) -> dict[str, Any]:
    """Approve a fully recompiled edge as a natural human project administrator."""

    _require_human_project_admin(ctx)
    body_hash = _approval_body_hash(ctx, mapping_version_id, request)
    replay = replay_or_conflict(
        session,
        ctx,
        operation=APPROVE_OPERATION,
        body_hash=body_hash,
    )
    if replay is not None:
        return replay

    mapping = get_mapping_version(
        session,
        ctx,
        mapping_version_id,
        for_update=True,
    )
    if mapping is None:
        raise ApiError(
            "LABEL_MAPPING_VERSION_NOT_FOUND",
            "标签映射版本不存在于当前租户项目范围",
            404,
        )
    if mapping.status == "approved":
        if (
            mapping.approved_by == ctx.user_id
            and mapping.payload.get("approval_body_sha256") == body_hash
            and mapping.payload.get("approval_actor_kind") == "human"
        ):
            return _save_approval_result(
                session,
                ctx,
                body_hash=body_hash,
                response=_approval_response(mapping, deduplicated=True),
            )
        raise ApiError(
            "LABEL_MAPPING_ALREADY_APPROVED",
            "映射版本已绑定另一份自然人审批",
            409,
            details=[{"mapping_version_id": mapping.mapping_version_id}],
        )
    if mapping.status in TERMINAL_MAPPING_STATUSES:
        raise ApiError(
            "LABEL_MAPPING_TERMINAL_IMMUTABLE",
            "终态映射版本不可重新审批",
            409,
        )
    if mapping.status != "validated":
        raise ApiError(
            "LABEL_MAPPING_STATUS_INVALID",
            "只有 validated 映射版本可以审批",
            409,
            details=[{"actual_status": mapping.status}],
        )
    if mapping.resource_version != request.expected_resource_version:
        raise ApiError(
            "RESOURCE_VERSION_CONFLICT",
            "标签映射版本 resource_version 已变化",
            409,
            details=[
                {
                    "actual_resource_version": mapping.resource_version,
                    "expected_resource_version": request.expected_resource_version,
                    "mapping_version_id": mapping.mapping_version_id,
                }
            ],
        )

    compile_request = _frozen_compile_request(mapping)
    compiled = _compile_from_request(
        session,
        ctx,
        compile_request,
        for_update=True,
    )
    item_count, target_count = _assert_persisted_content(
        session,
        ctx,
        mapping,
        compiled,
    )
    before = {
        "status": mapping.status,
        "resource_version": mapping.resource_version,
        "content_sha256": mapping.content_sha256,
    }
    approved_at = datetime.now(UTC)
    mapping.status = "approved"
    mapping.resource_version += 1
    mapping.approval_id = _approval_id(ctx, mapping, body_hash)
    mapping.approved_by = ctx.user_id
    mapping.approved_at = approved_at
    mapping.trace_id = ctx.trace_id
    summary = {
        "mapping_version_id": mapping.mapping_version_id,
        "status": mapping.status,
        "resource_version": mapping.resource_version,
        "content_sha256": mapping.content_sha256,
        "source_label_version_id": mapping.source_label_version_id,
        "target_label_version_id": mapping.target_label_version_id,
        "approval_id": mapping.approval_id,
        "approved_by": mapping.approved_by,
        "approved_at": approved_at.isoformat(),
        "reason_sha256": sha256_document({"reason": request.reason}),
        "item_count": item_count,
        "target_count": target_count,
    }
    audit = record_audit(
        session,
        ctx,
        action="label_mapping_version.approved",
        object_type="label_mapping_version",
        object_id=mapping.mapping_version_id,
        before=before,
        after=summary,
    )
    outbox_event = enqueue_event(
        session,
        ctx,
        event_type="label_mapping_version.approved",
        aggregate_type="label_mapping_version",
        aggregate_id=mapping.mapping_version_id,
        payload=summary,
    )
    session.flush()
    mapping.payload = {
        **mapping.payload,
        "approval_actor_kind": "human",
        "approval_roles": sorted(ctx.roles),
        "approval_body_sha256": body_hash,
        "approval_reason_sha256": summary["reason_sha256"],
        "approval_audit_id": audit.audit_id,
        "approval_outbox_event_id": outbox_event.event_id,
    }
    session.flush()
    return _save_approval_result(
        session,
        ctx,
        body_hash=body_hash,
        response=_approval_response(mapping, deduplicated=False),
    )


def _canonical_bundle_publish_request(
    request: LabelMappingBundlePublishRequest,
) -> dict[str, Any]:
    mapping_version_ids = sorted(request.mapping_version_ids)
    source_label_version_ids = sorted(request.source_label_version_ids)
    return {
        "mapping_version_ids": mapping_version_ids,
        "expected_mapping_resource_versions": {
            mapping_version_id: request.expected_mapping_resource_versions[mapping_version_id]
            for mapping_version_id in mapping_version_ids
        },
        "source_label_version_ids": source_label_version_ids,
        "expected_source_resource_versions": {
            source_label_version_id: request.expected_source_resource_versions[
                source_label_version_id
            ]
            for source_label_version_id in source_label_version_ids
        },
        "target_label_version_id": request.target_label_version_id,
        "expected_target_resource_version": request.expected_target_resource_version,
    }


def _bundle_publish_body_hash(
    ctx: RequestContext,
    request: LabelMappingBundlePublishRequest,
) -> str:
    return _request_hash(
        PUBLISH_BUNDLE_OPERATION,
        {
            "actor_id": ctx.user_id,
            "request": _canonical_bundle_publish_request(request),
        },
    )


def _bundle_compile_error(error: BundleCompileError) -> ApiError:
    return ApiError(
        error.code,
        str(error),
        409,
        details=[dict(error.details)] if error.details else [],
    )


def _approved_mapping_edges(
    session: Session,
    ctx: RequestContext,
    request: LabelMappingBundlePublishRequest,
) -> tuple[
    dict[str, LabelMappingVersion],
    tuple[BundleEdgeSnapshot, ...],
]:
    mappings = get_mapping_versions(
        session,
        ctx,
        request.mapping_version_ids,
        for_update=True,
    )
    missing_ids = sorted(set(request.mapping_version_ids) - set(mappings))
    if missing_ids:
        raise ApiError(
            "LABEL_MAPPING_VERSION_NOT_FOUND",
            "Bundle 引用的映射版本不存在于当前租户项目范围",
            404,
            details=[{"missing_mapping_version_ids": missing_ids}],
        )

    edges: list[BundleEdgeSnapshot] = []
    for mapping_version_id in sorted(request.mapping_version_ids):
        mapping = mappings[mapping_version_id]
        expected_resource_version = request.expected_mapping_resource_versions[mapping_version_id]
        if mapping.resource_version != expected_resource_version:
            raise ApiError(
                "RESOURCE_VERSION_CONFLICT",
                "Bundle 引用的映射版本 resource_version 已变化",
                409,
                details=[
                    {
                        "actual_resource_version": mapping.resource_version,
                        "expected_resource_version": expected_resource_version,
                        "mapping_version_id": mapping_version_id,
                    }
                ],
            )
        if mapping.status != "approved":
            raise ApiError(
                "LABEL_MAPPING_EDGE_NOT_APPROVED",
                "Bundle 的每条映射边都必须先经自然人审批",
                409,
                details=[
                    {
                        "actual_status": mapping.status,
                        "mapping_version_id": mapping_version_id,
                    }
                ],
            )
        approval_roles = mapping.payload.get("approval_roles")
        approval_body_sha256 = mapping.payload.get("approval_body_sha256")
        expected_approval_id = (
            _deterministic_id(
                "lma_",
                {
                    "approval_body_hash": approval_body_sha256,
                    "approved_by": mapping.approved_by,
                    "content_sha256": mapping.content_sha256,
                    "mapping_version_id": mapping.mapping_version_id,
                    "project_id": ctx.project_id,
                    "tenant_id": ctx.tenant_id,
                },
            )
            if isinstance(approval_body_sha256, str) and isinstance(mapping.approved_by, str)
            else None
        )
        if (
            mapping.approval_id is None
            or mapping.approved_by is None
            or mapping.approved_at is None
            or mapping.payload.get("approval_actor_kind") != "human"
            or not isinstance(approval_roles, list)
            or "project_admin" not in approval_roles
            or mapping.approval_id != expected_approval_id
        ):
            raise ApiError(
                "LABEL_MAPPING_EDGE_APPROVAL_INVALID",
                "approved 映射边缺少自然人项目管理员强审批绑定",
                409,
                details=[{"mapping_version_id": mapping_version_id}],
            )
        compile_request = _frozen_compile_request(mapping)
        compiled = _compile_from_request(
            session,
            ctx,
            compile_request,
            for_update=True,
        )
        _assert_persisted_content(session, ctx, mapping, compiled)
        edges.append(
            BundleEdgeSnapshot(
                mapping_version_id=mapping.mapping_version_id,
                mapping_resource_version=mapping.resource_version,
                compiled_edge=compiled,
            )
        )
    return mappings, tuple(edges)


def _bundle_version_snapshots(
    session: Session,
    ctx: RequestContext,
    request: LabelMappingBundlePublishRequest,
    mappings: dict[str, LabelMappingVersion],
) -> tuple[
    str,
    tuple[LabelVersionSnapshot, ...],
    LabelVersionSnapshot,
]:
    graph_version_ids = {
        request.target_label_version_id,
        *request.source_label_version_ids,
        *(mapping.source_label_version_id for mapping in mappings.values()),
        *(mapping.target_label_version_id for mapping in mappings.values()),
    }
    versions = get_label_versions(
        session,
        ctx,
        graph_version_ids,
        for_update=True,
    )
    missing_ids = sorted(graph_version_ids - set(versions))
    if missing_ids:
        raise ApiError(
            "LABEL_VERSION_NOT_FOUND",
            "Bundle DAG 引用的标签版本不存在于当前租户项目范围",
            404,
            details=[{"missing_label_version_ids": missing_ids}],
        )
    taxonomy_ids: set[str] = set()
    for version_id in sorted(graph_version_ids):
        version = versions[version_id]
        _require_strong_version(version, role="bundle")
        assert version.taxonomy_id is not None
        taxonomy_ids.add(version.taxonomy_id)
        if version.artifact_status != "published" or version.status != "published":
            raise ApiError(
                "LABEL_MAPPING_BUNDLE_VERSION_NOT_PUBLISHED",
                "Bundle DAG 的全部源、中间和目标标签版本必须已发布",
                409,
                details=[
                    {
                        "artifact_status": version.artifact_status,
                        "label_version_id": version.label_version_id,
                        "status": version.status,
                    }
                ],
            )
    if len(taxonomy_ids) != 1:
        raise ApiError(
            "LABEL_MAPPING_TAXONOMY_MISMATCH",
            "Bundle DAG 的全部标签版本必须属于同一 taxonomy",
            409,
            details=[{"taxonomy_ids": sorted(taxonomy_ids)}],
        )
    taxonomy_id = next(iter(taxonomy_ids))

    for source_version_id in request.source_label_version_ids:
        actual = versions[source_version_id].resource_version
        expected = request.expected_source_resource_versions[source_version_id]
        if actual != expected:
            raise ApiError(
                "RESOURCE_VERSION_CONFLICT",
                "Bundle source label version resource_version 已变化",
                409,
                details=[
                    {
                        "actual_resource_version": actual,
                        "expected_resource_version": expected,
                        "label_version_id": source_version_id,
                    }
                ],
            )
    target = versions[request.target_label_version_id]
    if target.resource_version != request.expected_target_resource_version:
        raise ApiError(
            "RESOURCE_VERSION_CONFLICT",
            "Bundle target label version resource_version 已变化",
            409,
            details=[
                {
                    "actual_resource_version": target.resource_version,
                    "expected_resource_version": request.expected_target_resource_version,
                    "label_version_id": target.label_version_id,
                }
            ],
        )

    snapshots: dict[str, LabelVersionSnapshot] = {}
    for version_id in sorted({request.target_label_version_id, *request.source_label_version_ids}):
        version = versions[version_id]
        items = list_label_version_items(
            session,
            ctx,
            version_id,
            for_update=True,
        )
        snapshots[version_id] = _version_snapshot(
            version,
            items,
            role="bundle",
        )
    return (
        taxonomy_id,
        tuple(snapshots[source_id] for source_id in request.source_label_version_ids),
        snapshots[request.target_label_version_id],
    )


def _compile_publish_bundle(
    session: Session,
    ctx: RequestContext,
    request: LabelMappingBundlePublishRequest,
) -> tuple[
    CompiledBundle,
    dict[str, LabelMappingVersion],
]:
    mappings, edges = _approved_mapping_edges(session, ctx, request)
    taxonomy_id, source_versions, target_version = _bundle_version_snapshots(
        session,
        ctx,
        request,
        mappings,
    )
    try:
        compiled = compile_bundle(
            BundleCompileInput(
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                taxonomy_id=taxonomy_id,
                source_versions=source_versions,
                target_version=target_version,
                edges=edges,
            )
        )
    except BundleCompileError as error:
        raise _bundle_compile_error(error) from error
    return compiled, mappings


def _bundle_id(ctx: RequestContext, compiled: CompiledBundle) -> str:
    return _deterministic_id(
        "lmb_",
        {
            "canonical_manifest_sha256": compiled.canonical_manifest_sha256,
            "project_id": ctx.project_id,
            "tenant_id": ctx.tenant_id,
        },
    )


def _bundle_approval_id(
    ctx: RequestContext,
    compiled: CompiledBundle,
) -> str:
    return _deterministic_id(
        "lmba_",
        {
            "approved_by": ctx.user_id,
            "canonical_manifest_sha256": compiled.canonical_manifest_sha256,
            "project_id": ctx.project_id,
            "tenant_id": ctx.tenant_id,
        },
    )


def _bundle_source_id(
    ctx: RequestContext,
    mapping_bundle_id: str,
    source_label_version_id: str,
) -> str:
    return _deterministic_id(
        "lmbs_",
        {
            "mapping_bundle_id": mapping_bundle_id,
            "project_id": ctx.project_id,
            "source_label_version_id": source_label_version_id,
            "tenant_id": ctx.tenant_id,
        },
    )


def _bundle_member_id(
    ctx: RequestContext,
    mapping_bundle_id: str,
    mapping_version_id: str,
) -> str:
    return _deterministic_id(
        "lmbm_",
        {
            "mapping_bundle_id": mapping_bundle_id,
            "mapping_version_id": mapping_version_id,
            "project_id": ctx.project_id,
            "tenant_id": ctx.tenant_id,
        },
    )


def _bundle_path_id(
    ctx: RequestContext,
    mapping_bundle_id: str,
    path_sha256: str,
) -> str:
    return _deterministic_id(
        "lmbp_",
        {
            "mapping_bundle_id": mapping_bundle_id,
            "path_sha256": path_sha256,
            "project_id": ctx.project_id,
            "tenant_id": ctx.tenant_id,
        },
    )


def _build_bundle_rows(
    ctx: RequestContext,
    request: LabelMappingBundlePublishRequest,
    compiled: CompiledBundle,
    mappings: dict[str, LabelMappingVersion],
) -> tuple[
    LabelMappingBundle,
    list[LabelMappingBundleSource],
    list[LabelMappingBundleMember],
    list[LabelMappingBundlePath],
]:
    mapping_bundle_id = _bundle_id(ctx, compiled)
    approved_at = datetime.now(UTC)
    bundle = LabelMappingBundle(
        mapping_bundle_id=mapping_bundle_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        target_label_version_id=compiled.target_label_version_id,
        source_label_version_ids=list(compiled.source_label_version_ids),
        source_manifest_sha256=compiled.source_manifest_sha256,
        compiler_version=compiled.compiler_version,
        status="approved",
        resource_version=1,
        canonical_manifest_sha256=compiled.canonical_manifest_sha256,
        approval_id=_bundle_approval_id(ctx, compiled),
        approved_by=ctx.user_id,
        approved_at=approved_at,
        published_at=None,
        root_trace_id=ctx.trace_id,
        trace_id=ctx.trace_id,
        payload={
            "schema_version": "auris.label-mapping-bundle/1",
            "canonical_manifest": compiled.canonical_manifest,
            "metric_registry_version": compiled.metric_registry_version,
            "publish_request": _canonical_bundle_publish_request(request),
            "publication_actor_kind": "human",
            "publication_roles": sorted(ctx.roles),
        },
    )
    sources = [
        LabelMappingBundleSource(
            bundle_source_id=_bundle_source_id(
                ctx,
                mapping_bundle_id,
                source.source_label_version_id,
            ),
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            mapping_bundle_id=mapping_bundle_id,
            source_label_version_id=source.source_label_version_id,
            source_resource_version=source.source_resource_version,
            source_order=source.source_order,
            content_sha256=source.content_sha256,
            trace_id=ctx.trace_id,
            payload={
                "schema_version": "auris.label-mapping-bundle-source/1",
                "version_content_sha256": source.version_content_sha256,
            },
        )
        for source in compiled.sources
    ]
    members = [
        LabelMappingBundleMember(
            bundle_member_id=_bundle_member_id(
                ctx,
                mapping_bundle_id,
                member.mapping_version_id,
            ),
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            mapping_bundle_id=mapping_bundle_id,
            mapping_version_id=member.mapping_version_id,
            source_label_version_id=member.source_label_version_id,
            target_label_version_id=member.target_label_version_id,
            edge_order=member.edge_order,
            edge_content_sha256=member.edge_content_sha256,
            trace_id=ctx.trace_id,
            payload={
                "schema_version": "auris.label-mapping-bundle-member/1",
                "mapping_resource_version": member.mapping_resource_version,
                "mapping_approval_id": mappings[member.mapping_version_id].approval_id,
            },
        )
        for member in compiled.members
    ]
    paths = [
        LabelMappingBundlePath(
            bundle_path_id=_bundle_path_id(
                ctx,
                mapping_bundle_id,
                path.path_sha256,
            ),
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            mapping_bundle_id=mapping_bundle_id,
            source_label_version_id=path.source_label_version_id,
            target_label_version_id=path.target_label_version_id,
            source_label_id=path.source_label_id,
            target_label_id=path.target_label_id,
            metric_family=path.metric_family,
            relation_path=list(path.relation_path),
            mapping_version_ids=list(path.mapping_version_ids),
            metric_grain=path.metric_grain,
            lineage_key=path.lineage_key,
            reducer=path.reducer,
            comparability_status=path.comparability_status,
            requires_recompute=path.requires_recompute,
            path_sha256=path.path_sha256,
            trace_id=ctx.trace_id,
            payload={
                "schema_version": "auris.label-mapping-bundle-path/1",
                "coverage_gap": path.coverage_gap,
                "compiler_version": compiled.compiler_version,
            },
        )
        for path in compiled.paths
    ]
    return bundle, sources, members, paths


def _bundle_child_projection(
    sources: list[LabelMappingBundleSource],
    members: list[LabelMappingBundleMember],
    paths: list[LabelMappingBundlePath],
) -> dict[str, Any]:
    return {
        "sources": [
            {
                "bundle_source_id": source.bundle_source_id,
                "source_label_version_id": source.source_label_version_id,
                "source_resource_version": source.source_resource_version,
                "source_order": source.source_order,
                "content_sha256": source.content_sha256,
                "payload": source.payload,
            }
            for source in sources
        ],
        "members": [
            {
                "bundle_member_id": member.bundle_member_id,
                "mapping_version_id": member.mapping_version_id,
                "source_label_version_id": member.source_label_version_id,
                "target_label_version_id": member.target_label_version_id,
                "edge_order": member.edge_order,
                "edge_content_sha256": member.edge_content_sha256,
                "payload": member.payload,
            }
            for member in members
        ],
        "paths": [
            {
                "bundle_path_id": path.bundle_path_id,
                "source_label_version_id": path.source_label_version_id,
                "target_label_version_id": path.target_label_version_id,
                "source_label_id": path.source_label_id,
                "target_label_id": path.target_label_id,
                "metric_family": path.metric_family,
                "relation_path": path.relation_path,
                "mapping_version_ids": path.mapping_version_ids,
                "metric_grain": path.metric_grain,
                "lineage_key": path.lineage_key,
                "reducer": path.reducer,
                "comparability_status": path.comparability_status,
                "requires_recompute": path.requires_recompute,
                "path_sha256": path.path_sha256,
                "payload": path.payload,
            }
            for path in paths
        ],
    }


def _assert_bundle_persisted_content(
    session: Session,
    ctx: RequestContext,
    bundle: LabelMappingBundle,
    request: LabelMappingBundlePublishRequest,
    compiled: CompiledBundle,
    mappings: dict[str, LabelMappingVersion],
) -> None:
    expected_payload_keys = {
        "schema_version",
        "canonical_manifest",
        "metric_registry_version",
        "publish_request",
        "publication_actor_kind",
        "publication_roles",
        "publication_audit_id",
        "publication_outbox_event_id",
        "path_manifest_sha256",
    }
    expected_path_manifest_sha256 = sha256_document(
        {"path_sha256s": [path.path_sha256 for path in compiled.paths]}
    )
    if (
        bundle.status != "published"
        or bundle.target_label_version_id != compiled.target_label_version_id
        or bundle.source_label_version_ids != list(compiled.source_label_version_ids)
        or bundle.source_manifest_sha256 != compiled.source_manifest_sha256
        or bundle.compiler_version != compiled.compiler_version
        or bundle.canonical_manifest_sha256 != compiled.canonical_manifest_sha256
        or bundle.payload.get("canonical_manifest") != compiled.canonical_manifest
        or set(bundle.payload) != expected_payload_keys
        or bundle.payload.get("schema_version") != "auris.label-mapping-bundle/1"
        or bundle.payload.get("metric_registry_version") != compiled.metric_registry_version
        or bundle.payload.get("publish_request") != _canonical_bundle_publish_request(request)
        or bundle.payload.get("path_manifest_sha256") != expected_path_manifest_sha256
        or bundle.payload.get("publication_actor_kind") != "human"
        or not isinstance(bundle.payload.get("publication_roles"), list)
        or "project_admin" not in bundle.payload.get("publication_roles", [])
        or bundle.approved_by is None
        or bundle.approved_at is None
        or bundle.published_at is None
        or bundle.approval_id
        != _deterministic_id(
            "lmba_",
            {
                "approved_by": bundle.approved_by,
                "canonical_manifest_sha256": compiled.canonical_manifest_sha256,
                "project_id": ctx.project_id,
                "tenant_id": ctx.tenant_id,
            },
        )
    ):
        raise ApiError(
            "LABEL_MAPPING_BUNDLE_CONTENT_DRIFT",
            "已发布 Bundle 父记录与服务端规范内容不一致",
            409,
            details=[{"mapping_bundle_id": bundle.mapping_bundle_id}],
        )
    _, expected_sources, expected_members, expected_paths = _build_bundle_rows(
        ctx,
        request,
        compiled,
        mappings,
    )
    actual_sources = list_bundle_sources(session, ctx, bundle.mapping_bundle_id)
    actual_members = list_bundle_members(session, ctx, bundle.mapping_bundle_id)
    actual_paths = list_bundle_paths(session, ctx, bundle.mapping_bundle_id)
    if _bundle_child_projection(
        actual_sources,
        actual_members,
        actual_paths,
    ) != _bundle_child_projection(
        expected_sources,
        expected_members,
        expected_paths,
    ):
        raise ApiError(
            "LABEL_MAPPING_BUNDLE_CONTENT_DRIFT",
            "已发布 Bundle 的 source/member/path 子记录发生漂移",
            409,
            details=[
                {
                    "actual_member_count": len(actual_members),
                    "actual_path_count": len(actual_paths),
                    "actual_source_count": len(actual_sources),
                    "expected_member_count": len(expected_members),
                    "expected_path_count": len(expected_paths),
                    "expected_source_count": len(expected_sources),
                    "mapping_bundle_id": bundle.mapping_bundle_id,
                }
            ],
        )


def _bundle_event_ids(bundle: LabelMappingBundle) -> tuple[int, int]:
    audit_id = bundle.payload.get("publication_audit_id")
    outbox_event_id = bundle.payload.get("publication_outbox_event_id")
    if not isinstance(audit_id, int) or not isinstance(outbox_event_id, int):
        raise ApiError(
            "LABEL_MAPPING_BUNDLE_CONTENT_DRIFT",
            "已发布 Bundle 缺少冻结的审计事件引用",
            409,
            details=[{"mapping_bundle_id": bundle.mapping_bundle_id}],
        )
    return audit_id, outbox_event_id


def _bundle_response(
    bundle: LabelMappingBundle,
    compiled: CompiledBundle,
    *,
    deduplicated: bool,
) -> dict[str, Any]:
    audit_id, outbox_event_id = _bundle_event_ids(bundle)
    return {
        "mapping_bundle_id": bundle.mapping_bundle_id,
        "status": bundle.status,
        "resource_version": bundle.resource_version,
        "source_label_version_ids": list(compiled.source_label_version_ids),
        "target_label_version_id": compiled.target_label_version_id,
        "mapping_version_ids": [member.mapping_version_id for member in compiled.members],
        "source_manifest_sha256": compiled.source_manifest_sha256,
        "canonical_manifest_sha256": compiled.canonical_manifest_sha256,
        "compiler_version": compiled.compiler_version,
        "metric_registry_version": compiled.metric_registry_version,
        "member_count": len(compiled.members),
        "path_count": len(compiled.paths),
        "canonical_manifest": compiled.canonical_manifest,
        "approval_id": bundle.approval_id,
        "approved_by": bundle.approved_by,
        "deduplicated": deduplicated,
        "audit_id": audit_id,
        "outbox_event_id": outbox_event_id,
        "trace_id": bundle.trace_id,
    }


def _save_bundle_result(
    session: Session,
    ctx: RequestContext,
    *,
    body_hash: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    save_idempotency_result(
        session,
        ctx,
        operation=PUBLISH_BUNDLE_OPERATION,
        body_hash=body_hash,
        status_code=201,
        response_json=response,
    )
    return response


def publish_label_mapping_bundle(
    session: Session,
    ctx: RequestContext,
    request: LabelMappingBundlePublishRequest,
) -> dict[str, Any]:
    """Compile and atomically publish one immutable multi-edge mapping bundle."""

    _require_human_project_admin(ctx)
    body_hash = _bundle_publish_body_hash(ctx, request)
    replay = replay_or_conflict(
        session,
        ctx,
        operation=PUBLISH_BUNDLE_OPERATION,
        body_hash=body_hash,
    )
    if replay is not None:
        return replay

    compiled, mappings = _compile_publish_bundle(session, ctx, request)
    existing = find_bundle_by_manifest_sha256(
        session,
        ctx,
        compiled.canonical_manifest_sha256,
        for_update=True,
    )
    if existing is not None:
        _assert_bundle_persisted_content(
            session,
            ctx,
            existing,
            request,
            compiled,
            mappings,
        )
        return _save_bundle_result(
            session,
            ctx,
            body_hash=body_hash,
            response=_bundle_response(existing, compiled, deduplicated=True),
        )

    bundle, sources, members, paths = _build_bundle_rows(
        ctx,
        request,
        compiled,
        mappings,
    )
    session.add(bundle)
    session.flush()
    session.add_all(sources)
    session.flush()
    session.add_all(members)
    session.flush()
    session.add_all(paths)
    session.flush()

    path_manifest_sha256 = sha256_document(
        {"path_sha256s": [path.path_sha256 for path in compiled.paths]}
    )
    published_at = datetime.now(UTC)
    summary = {
        "mapping_bundle_id": bundle.mapping_bundle_id,
        "status": "published",
        "resource_version": bundle.resource_version,
        "source_label_version_ids": list(compiled.source_label_version_ids),
        "target_label_version_id": compiled.target_label_version_id,
        "mapping_version_ids": [member.mapping_version_id for member in compiled.members],
        "source_manifest_sha256": compiled.source_manifest_sha256,
        "canonical_manifest_sha256": compiled.canonical_manifest_sha256,
        "path_manifest_sha256": path_manifest_sha256,
        "compiler_version": compiled.compiler_version,
        "metric_registry_version": compiled.metric_registry_version,
        "member_count": len(compiled.members),
        "path_count": len(compiled.paths),
        "approval_id": bundle.approval_id,
        "approved_by": bundle.approved_by,
        "published_at": published_at.isoformat(),
    }
    audit = record_audit(
        session,
        ctx,
        action="label_mapping_bundle.published",
        object_type="label_mapping_bundle",
        object_id=bundle.mapping_bundle_id,
        after=summary,
    )
    outbox_event = enqueue_event(
        session,
        ctx,
        event_type="label_mapping_bundle.published",
        aggregate_type="label_mapping_bundle",
        aggregate_id=bundle.mapping_bundle_id,
        payload=summary,
    )
    session.flush()
    bundle.payload = {
        **bundle.payload,
        "publication_audit_id": audit.audit_id,
        "publication_outbox_event_id": outbox_event.event_id,
        "path_manifest_sha256": path_manifest_sha256,
    }
    session.flush()
    bundle.status = "published"
    bundle.published_at = published_at
    session.flush()
    return _save_bundle_result(
        session,
        ctx,
        body_hash=body_hash,
        response=_bundle_response(bundle, compiled, deduplicated=False),
    )
