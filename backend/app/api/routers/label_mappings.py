from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterable
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from app.api.deps import ContextDep, SessionDep
from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.response import envelope
from app.schemas.common import ApiErrorEnvelope, ApiMeta
from app.schemas.label_mapping import (
    LabelMappingApprovalRequest,
    LabelMappingBundlePublishRequest,
    LabelMappingCreateRequest,
    LabelMappingValidationRequest,
)
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    require_idempotency,
    save_idempotency_result,
)
from app.services.label_mapping_service import (
    approve_label_mapping_version,
    create_label_mapping_version,
    dry_run_label_mapping_edge,
    publish_label_mapping_bundle,
    validate_label_mapping_version,
)


class MappingContractRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_route_handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                return await original_route_handler(request)
            except RequestValidationError as exc:
                raise ApiError(
                    "VALIDATION_ERROR",
                    "请求参数校验失败",
                    422,
                    details=[
                        {
                            "field": ".".join(str(part) for part in error["loc"]),
                            "message": str(error["msg"]),
                            "code": str(error["type"]),
                        }
                        for error in exc.errors()
                    ],
                ) from exc

        return route_handler


class StrictMappingResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LabelMappingScope(StrictMappingResponseModel):
    project_id: str
    tenant_id: str


class LabelMappingLabelVersionRef(StrictMappingResponseModel):
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    label_version_id: str
    resource_version: int = Field(ge=1)
    taxonomy_id: str


class LabelMappingCoverageResult(StrictMappingResponseModel):
    active_source_item_count: int = Field(ge=0)
    coverage_gap_source_label_ids: list[str]
    disposition_count: int = Field(ge=0)
    exact_count: int = Field(ge=0)
    metric_dependent_count: int = Field(ge=0)
    normalizable_count: int = Field(ge=0)
    recompute_required_source_label_ids: list[str]
    structural_break_count: int = Field(ge=0)
    unmapped_source_label_ids: list[str]


class LabelMappingCompatibilityEvidenceResult(StrictMappingResponseModel):
    evidence_type: str
    evidence_id: str
    resource_version: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LabelMappingCompiledTargetResult(StrictMappingResponseModel):
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_label_id: str
    target_order: int = Field(ge=0)


class LabelMappingCompiledItemResult(StrictMappingResponseModel):
    allowed_metric_families: list[str]
    comparability_status: Literal[
        "comparable",
        "partial",
        "structural-break",
        "not-applicable",
    ]
    compatibility: Literal[
        "exact",
        "metric-dependent",
        "structural-break",
        "not-applicable",
    ]
    compatibility_evidence: LabelMappingCompatibilityEvidenceResult | None
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lineage_key: str | None
    merge_group_sha256: str | None
    metric_grain: str | None
    reducer: str | None
    relation: Literal["identity", "rename", "replace", "merge", "retire", "split-recompute"]
    requires_recompute: bool
    source_definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_label_id: str
    source_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_semantic_sha256: str | None
    targets: list[LabelMappingCompiledTargetResult]


class LabelMappingEdgeCanonicalManifest(StrictMappingResponseModel):
    compiler_version: str
    coverage: LabelMappingCoverageResult
    items: list[LabelMappingCompiledItemResult]
    mapping_version: str
    metric_registry_version: str
    schema_version: Literal["auris.label-mapping-edge/1"]
    scope: LabelMappingScope
    source_label_version: LabelMappingLabelVersionRef
    target_label_version: LabelMappingLabelVersionRef


class LabelMappingCompiledEdgeResult(StrictMappingResponseModel):
    mapping_version: str
    compiler_version: str
    metric_registry_version: str
    source_label_version_id: str
    target_label_version_id: str
    source_resource_version: int = Field(ge=1)
    target_resource_version: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage: LabelMappingCoverageResult
    items: list[LabelMappingCompiledItemResult]
    canonical_manifest: LabelMappingEdgeCanonicalManifest


class LabelMappingDryRunResult(LabelMappingCompiledEdgeResult):
    persisted: Literal[False]


class LabelMappingVersionCreateResult(LabelMappingCompiledEdgeResult):
    persisted: Literal[True]
    mapping_version_id: str
    status: str
    resource_version: int = Field(ge=1)
    deduplicated: bool
    audit_id: int = Field(ge=1)
    outbox_event_id: int = Field(ge=1)
    trace_id: str


class LabelMappingValidationResult(StrictMappingResponseModel):
    mapping_version_id: str
    mapping_version: str
    status: str
    resource_version: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiler_version: str
    metric_registry_version: str
    coverage: LabelMappingCoverageResult
    already_validated: bool
    audit_id: int = Field(ge=1)
    outbox_event_id: int = Field(ge=1)
    trace_id: str


class LabelMappingApprovalResult(StrictMappingResponseModel):
    mapping_version_id: str
    status: Literal["approved"]
    resource_version: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_id: str
    approved_by: str
    approved_at: str
    deduplicated: bool
    audit_id: int = Field(ge=1)
    outbox_event_id: int = Field(ge=1)
    trace_id: str


class LabelMappingBundleSourceResult(StrictMappingResponseModel):
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_label_version_id: str
    source_order: int = Field(ge=0)
    source_resource_version: int = Field(ge=1)
    version_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LabelMappingBundleMemberResult(StrictMappingResponseModel):
    edge_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    edge_order: int = Field(ge=0)
    mapping_resource_version: int = Field(ge=1)
    mapping_version_id: str
    source_label_version_id: str
    target_label_version_id: str


class LabelMappingBundleRelationStepResult(StrictMappingResponseModel):
    comparability_status: Literal[
        "comparable",
        "partial",
        "structural-break",
        "not-applicable",
    ]
    compatibility: Literal[
        "exact",
        "metric-dependent",
        "structural-break",
        "not-applicable",
    ]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mapping_version_id: str
    relation: Literal["identity", "rename", "replace", "merge", "retire", "split-recompute"]
    source_label_id: str
    source_label_version_id: str
    target_label_ids: list[str]
    target_label_version_id: str
    path_outcome: Literal["metric-family-not-approved"] | None = None


class LabelMappingBundlePathResult(StrictMappingResponseModel):
    comparability_status: Literal[
        "comparable",
        "partial",
        "structural-break",
        "not-applicable",
    ]
    coverage_gap: bool
    lineage_key: str | None
    mapping_version_ids: list[str]
    metric_family: str
    metric_grain: str | None
    path_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reducer: str | None
    relation_path: list[LabelMappingBundleRelationStepResult]
    requires_recompute: bool
    source_label_id: str
    source_label_version_id: str
    target_label_id: str | None
    target_label_version_id: str


class LabelMappingBundleCanonicalManifest(StrictMappingResponseModel):
    compiler_version: str
    members: list[LabelMappingBundleMemberResult]
    metric_registry_version: str
    paths: list[LabelMappingBundlePathResult]
    schema_version: Literal["auris.label-mapping-bundle/1"]
    scope: LabelMappingScope
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sources: list[LabelMappingBundleSourceResult]
    target_label_version: LabelMappingLabelVersionRef
    taxonomy_id: str


class LabelMappingBundlePublishResult(StrictMappingResponseModel):
    mapping_bundle_id: str
    status: Literal["published"]
    resource_version: int = Field(ge=1)
    source_label_version_ids: list[str]
    target_label_version_id: str
    mapping_version_ids: list[str]
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiler_version: str
    metric_registry_version: str
    member_count: int = Field(ge=1)
    path_count: int = Field(ge=0)
    canonical_manifest: LabelMappingBundleCanonicalManifest
    approval_id: str
    approved_by: str
    deduplicated: bool
    audit_id: int = Field(ge=1)
    outbox_event_id: int = Field(ge=1)
    trace_id: str


class StrictMappingEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meta: ApiMeta


class LabelMappingDryRunResponse(StrictMappingEnvelope):
    data: LabelMappingDryRunResult


class LabelMappingVersionCreateResponse(StrictMappingEnvelope):
    data: LabelMappingVersionCreateResult


class LabelMappingValidationResponse(StrictMappingEnvelope):
    data: LabelMappingValidationResult


class LabelMappingApprovalResponse(StrictMappingEnvelope):
    data: LabelMappingApprovalResult


class LabelMappingBundlePublishResponse(StrictMappingEnvelope):
    data: LabelMappingBundlePublishResult


router = APIRouter(tags=["label-mappings"], route_class=MappingContractRoute)

MAPPING_AUTHOR_ROLES = ("project_admin", "model_engineer")
MAPPING_APPROVER_ROLES = ("project_admin",)
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ApiErrorEnvelope, "description": "上下文或幂等键无效"},
    401: {"model": ApiErrorEnvelope, "description": "身份认证失败"},
    403: {"model": ApiErrorEnvelope, "description": "角色或自然人审批权限不足"},
    404: {"model": ApiErrorEnvelope, "description": "标签版本或映射版本不存在"},
    409: {"model": ApiErrorEnvelope, "description": "幂等、状态、版本或冻结内容冲突"},
    422: {"model": ApiErrorEnvelope, "description": "请求或映射语义校验失败"},
    429: {"model": ApiErrorEnvelope, "description": "请求频率超过限额"},
    503: {"model": ApiErrorEnvelope, "description": "依赖暂时不可用"},
}
_DRY_RUN_HTTP_OPERATION = "http.label_mapping_versions.dry_run"
_CREATE_HTTP_OPERATION = "http.label_mapping_versions.create"
_VALIDATE_HTTP_OPERATION = "http.label_mapping_versions.validate"
_APPROVE_HTTP_OPERATION = "http.label_mapping_versions.approve"
_PUBLISH_BUNDLE_HTTP_OPERATION = "http.label_mapping_bundles.publish"


def _authorize_write(
    ctx: RequestContext,
    *,
    roles: Iterable[str],
    action: str,
) -> None:
    require_idempotency(ctx)
    require_any_role(ctx, roles, action=action)


async def _begin_http_operation(
    request: Request,
    session: SessionDep,
    ctx: RequestContext,
    *,
    operation: str,
) -> tuple[str, dict[str, Any] | None]:
    body_hash = await request_hash(request)
    replay = replay_or_conflict(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
    )
    if replay is not None:
        session.commit()
    return body_hash, replay


def _complete_http_operation(
    session: SessionDep,
    ctx: RequestContext,
    *,
    operation: str,
    body_hash: str,
    status_code: int,
    data: dict[str, Any],
) -> dict[str, Any]:
    response = envelope(data, ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=status_code,
        response_json=response,
    )
    session.commit()
    return response


@router.post(
    "/label-mapping-versions/dry-run",
    operation_id="postLabelMappingVersionsDryRun",
    response_model=LabelMappingDryRunResponse,
    response_model_exclude_unset=True,
    responses=ERROR_RESPONSES,
)
async def post_label_mapping_dry_run(
    body: LabelMappingCreateRequest,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    _authorize_write(
        ctx,
        roles=MAPPING_AUTHOR_ROLES,
        action="label_mapping_versions.dry_run",
    )
    body_hash, replay = await _begin_http_operation(
        request,
        session,
        ctx,
        operation=_DRY_RUN_HTTP_OPERATION,
    )
    if replay is not None:
        return replay
    data = dry_run_label_mapping_edge(session, ctx, body)
    return _complete_http_operation(
        session,
        ctx,
        operation=_DRY_RUN_HTTP_OPERATION,
        body_hash=body_hash,
        status_code=200,
        data=data,
    )


@router.post(
    "/label-mapping-versions",
    operation_id="postLabelMappingVersions",
    status_code=201,
    response_model=LabelMappingVersionCreateResponse,
    response_model_exclude_unset=True,
    responses=ERROR_RESPONSES,
)
async def post_label_mapping_version(
    body: LabelMappingCreateRequest,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    _authorize_write(
        ctx,
        roles=MAPPING_AUTHOR_ROLES,
        action="label_mapping_versions.create",
    )
    body_hash, replay = await _begin_http_operation(
        request,
        session,
        ctx,
        operation=_CREATE_HTTP_OPERATION,
    )
    if replay is not None:
        return replay
    data = create_label_mapping_version(session, ctx, body)
    return _complete_http_operation(
        session,
        ctx,
        operation=_CREATE_HTTP_OPERATION,
        body_hash=body_hash,
        status_code=201,
        data=data,
    )


@router.post(
    "/label-mapping-versions/{id}/validate",
    operation_id="postLabelMappingVersionsByIdValidate",
    response_model=LabelMappingValidationResponse,
    response_model_exclude_unset=True,
    responses=ERROR_RESPONSES,
)
async def post_label_mapping_version_validation(
    id: str,
    body: LabelMappingValidationRequest,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    _authorize_write(
        ctx,
        roles=MAPPING_AUTHOR_ROLES,
        action="label_mapping_versions.validate",
    )
    body_hash, replay = await _begin_http_operation(
        request,
        session,
        ctx,
        operation=_VALIDATE_HTTP_OPERATION,
    )
    if replay is not None:
        return replay
    data = validate_label_mapping_version(session, ctx, id, body)
    return _complete_http_operation(
        session,
        ctx,
        operation=_VALIDATE_HTTP_OPERATION,
        body_hash=body_hash,
        status_code=200,
        data=data,
    )


@router.post(
    "/label-mapping-versions/{id}/approve",
    operation_id="postLabelMappingVersionsByIdApprove",
    response_model=LabelMappingApprovalResponse,
    response_model_exclude_unset=True,
    responses=ERROR_RESPONSES,
)
async def post_label_mapping_version_approval(
    id: str,
    body: LabelMappingApprovalRequest,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    _authorize_write(
        ctx,
        roles=MAPPING_APPROVER_ROLES,
        action="label_mapping_versions.approve",
    )
    body_hash, replay = await _begin_http_operation(
        request,
        session,
        ctx,
        operation=_APPROVE_HTTP_OPERATION,
    )
    if replay is not None:
        return replay
    data = approve_label_mapping_version(session, ctx, id, body)
    return _complete_http_operation(
        session,
        ctx,
        operation=_APPROVE_HTTP_OPERATION,
        body_hash=body_hash,
        status_code=200,
        data=data,
    )


@router.post(
    "/label-mapping-bundles/publish",
    operation_id="postLabelMappingBundlesPublish",
    status_code=201,
    response_model=LabelMappingBundlePublishResponse,
    response_model_exclude_unset=True,
    responses=ERROR_RESPONSES,
)
async def post_label_mapping_bundle(
    body: LabelMappingBundlePublishRequest,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    _authorize_write(
        ctx,
        roles=MAPPING_APPROVER_ROLES,
        action="label_mapping_bundles.publish",
    )
    body_hash, replay = await _begin_http_operation(
        request,
        session,
        ctx,
        operation=_PUBLISH_BUNDLE_HTTP_OPERATION,
    )
    if replay is not None:
        return replay
    data = publish_label_mapping_bundle(session, ctx, body)
    return _complete_http_operation(
        session,
        ctx,
        operation=_PUBLISH_BUNDLE_HTTP_OPERATION,
        body_hash=body_hash,
        status_code=201,
        data=data,
    )
