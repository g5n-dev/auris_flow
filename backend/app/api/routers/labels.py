from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.deps import ContextDep, PaginationDep, SessionDep
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.request_identifiers import public_id_from_hex
from app.core.response import collection_envelope, envelope
from app.models import (
    EvalDatasetVersion,
    LabelAggregationPolicyVersion,
    LabelVersion,
    PromptVersion,
    RunRecord,
)
from app.schemas import LabelOptimizationRunRequest, LabelVersionRequest, parse_payload
from app.schemas.label_closed_loop import LabelVersionEvaluationLockRequest
from app.schemas.label_policy import (
    MAX_EVALUATION_SOURCE_BYTES,
    MAX_POLICY_SOURCE_BYTES,
    LabelCandidateEvaluationRequest,
    LabelPolicyValidationRequest,
    LabelVersionPublishRequest,
    parse_strict_json_request,
)
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.label_lifecycle_compat_service import compatible_label_version_resources
from app.services.label_lifecycle_service import (
    enrich_label_version_lifecycle_views,
    list_label_version_item_views,
)
from app.services.label_policy_service import (
    evaluate_label_candidate,
    evaluate_label_version_release,
    get_evaluation_data,
    get_policy_data,
    validate_label_policy,
)
from app.services.prompt_release_service import lock_label_version_for_evaluation
from app.services.resource_service import (
    create_idempotent_json_resource,
    get_resource,
    list_resource_data,
    list_resource_page,
    patch_idempotent_json_resource,
)
from app.services.run_service import create_run, get_run

router = APIRouter(tags=["labels"])


@router.get("/labels")
def get_labels(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    taxonomies = list_resource_data(session, ctx, "taxonomies", limit=int(page["limit"] or 50))
    candidates = list_resource_data(session, ctx, "label_candidates", limit=200)
    data = [{"taxonomy": taxonomy, "labels": candidates} for taxonomy in taxonomies]
    return collection_envelope(data, ctx)


@router.get("/label-versions")
def get_label_versions(
    session: SessionDep, ctx: ContextDep, page: PaginationDep, status: str | None = None
):
    resource_page = list_resource_page(session, ctx, "label_versions", page, status=status)
    compatible = compatible_label_version_resources(session, ctx, resource_page.items)
    return collection_envelope(
        enrich_label_version_lifecycle_views(session, ctx, compatible),
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
    )


@router.post("/label-versions", status_code=201)
async def post_label_versions(request: Request, session: SessionDep, ctx: ContextDep):
    return await create_idempotent_json_resource(
        session,
        ctx,
        request,
        "label_versions",
        key_prefix="label_version",
        status="draft",
        body_model=LabelVersionRequest,
        reject_existing=True,
    )


@router.get("/label-versions/{id}")
def get_label_versions_by_id(id: str, session: SessionDep, ctx: ContextDep):
    resource = get_resource(session, ctx, "label_versions", id)
    data = compatible_label_version_resources(session, ctx, [resource.data])[0]
    enriched = enrich_label_version_lifecycle_views(
        session,
        ctx,
        [data],
        include_timeline=True,
    )[0]
    return envelope(enriched, ctx)


@router.get("/label-versions/{id}/items")
def get_label_version_items(
    id: str,
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
    status: str | None = None,
):
    items, total, next_cursor = list_label_version_item_views(
        session,
        ctx,
        label_version_id=id,
        status=status,
        cursor=cast(str | None, page["cursor"]),
        limit=int(page["limit"] or 50),
    )
    return collection_envelope(
        items,
        ctx,
        total=total,
        limit=int(page["limit"] or 50),
        next_cursor=next_cursor,
    )


@router.patch("/label-versions/{id}")
async def patch_label_versions_by_id(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    body = await request.json()
    protected_fields = {
        "status",
        "policy_version_id",
        "release_gate_id",
        "release_policy_evaluation_id",
        "release_policy_verdict",
        "release_policy_decision_sha256",
        "resource_version",
        "published_at",
        "taxonomy_id",
        "semantic_version",
        "base_label_version_id",
        "artifact_status",
        "artifact_published_at",
        "artifact_deprecated_at",
        "deprecation_reason",
        "replacement_label_version_id",
        "mapping_bundle_id",
        "content_sha256",
    }
    attempted = sorted(protected_fields.intersection(body))
    if attempted:
        raise ApiError(
            "LABEL_VERSION_SERVER_FIELDS_IMMUTABLE",
            "标签版本状态、门禁和策略绑定只能通过受控动作更新",
            422,
            details=[{"fields": attempted}],
        )
    version = session.scalar(
        select(LabelVersion).where(
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
            LabelVersion.label_version_id == id,
        )
    )
    if version is not None:
        artifact_status = version.artifact_status or version.status
        if artifact_status in {"published", "deprecated", "archived"}:
            if artifact_status == "published":
                code = "PUBLISHED_LABEL_VERSION_IMMUTABLE"
                message = "已发布标签版本不可原地修改，请创建候选版本"
            else:
                code = "TERMINAL_LABEL_VERSION_IMMUTABLE"
                message = "已废弃或归档的标签版本不可原地修改"
            raise ApiError(code, message, 409)
    return await patch_idempotent_json_resource(session, ctx, request, "label_versions", id)


@router.post("/label-versions/{id}/evaluation-lock")
async def post_label_version_evaluation_lock(
    id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    require_any_role(
        ctx,
        ("project_admin", "model_engineer"),
        "label_versions.lock_for_evaluation",
    )
    body_hash = await request_hash(request)
    operation = f"label_versions.evaluation_lock:{id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    body = parse_payload(LabelVersionEvaluationLockRequest, await request.json())
    response = envelope(lock_label_version_for_evaluation(session, ctx, id, body), ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        response_json=response,
    )
    session.commit()
    return response


@router.post("/label-versions/{id}/policy/validate", status_code=201)
async def post_label_version_policy_validation(
    id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    require_any_role(
        ctx,
        ("project_admin", "model_engineer", "review_arbitrator"),
        "label_policy.validate",
    )
    body_hash = await request_hash(request)
    operation = f"label_policy.validate:{id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    body = cast(
        LabelPolicyValidationRequest,
        parse_strict_json_request(
            await request.body(),
            LabelPolicyValidationRequest,
            max_bytes=MAX_POLICY_SOURCE_BYTES,
        ),
    )
    if body.activate and "system" in ctx.roles:
        raise ApiError(
            "AGENT_POLICY_ACTIVATION_FORBIDDEN",
            "系统或智能体身份不能激活标签策略",
            403,
        )
    response = envelope(
        validate_label_policy(
            session,
            ctx,
            label_version_id=id,
            request_body=body,
        ),
        ctx,
    )
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        response_json=response,
    )
    session.commit()
    return response


@router.get("/label-policy-versions/{id}")
def get_label_policy_version(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_policy_data(session, ctx, id), ctx)


@router.post("/label-candidates/evaluate", status_code=201)
async def post_label_candidate_evaluation(
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    require_any_role(
        ctx,
        ("project_admin", "model_engineer", "review_arbitrator"),
        "label_candidate.evaluate",
    )
    body_hash = await request_hash(request)
    operation = "label_candidate.policy_evaluate"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    body = cast(
        LabelCandidateEvaluationRequest,
        parse_strict_json_request(
            await request.body(),
            LabelCandidateEvaluationRequest,
            max_bytes=MAX_EVALUATION_SOURCE_BYTES,
        ),
    )
    response = envelope(evaluate_label_candidate(session, ctx, request_body=body), ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        response_json=response,
    )
    session.commit()
    return response


@router.get("/label-policy-evaluations/{id}")
def get_label_policy_evaluation(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_evaluation_data(session, ctx, id), ctx)


@router.post("/label-versions/{id}/publish", status_code=202)
async def post_label_versions_by_id_publish(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    require_any_role(ctx, ("project_admin", "model_engineer"), "label_versions.publish")
    if "system" in ctx.roles:
        raise ApiError(
            "AGENT_LABEL_PUBLISH_FORBIDDEN",
            "系统或智能体身份不能批准标签版本发布",
            403,
        )
    body = cast(
        LabelVersionPublishRequest,
        parse_strict_json_request(
            await request.body(),
            LabelVersionPublishRequest,
            max_bytes=MAX_EVALUATION_SOURCE_BYTES,
        ),
    )
    gate = evaluate_label_version_release(
        session,
        ctx,
        label_version_id=id,
        request_body=body,
    )
    run_status = "pending" if gate["verdict"] in {"pass", "gray_only"} else "blocked"
    return await create_run(
        session,
        ctx,
        request,
        run_type="label_publish",
        event_type="label_version.publish_requested",
        payload={
            **body.model_dump(mode="json", exclude_none=True),
            "label_version_id": id,
            "release_policy_version_id": gate["policy_version_id"],
            "release_policy_evaluation_id": gate["evaluation_id"],
            "release_policy_verdict": gate["verdict"],
            "release_policy_decision_sha256": gate["decision_sha256"],
            "release_policy_facts_sha256": gate["facts_sha256"],
            "release_label_resource_version": gate["label_resource_version"],
            "approved_by": ctx.user_id,
            "approval_roles": list(ctx.roles),
        },
        status=run_status,
    )


@router.post("/label-optimization-runs", status_code=202)
async def post_label_optimization_runs(request: Request, session: SessionDep, ctx: ContextDep):
    body_hash = await request_hash(request)
    replay = replay_or_conflict(
        session,
        ctx,
        operation="create:label_optimization",
        body_hash=body_hash,
    )
    if replay is not None:
        return replay
    request_body = parse_payload(LabelOptimizationRunRequest, await request.json())
    body = request_body.model_dump(mode="json", exclude_none=True)
    if "system" in ctx.roles and request_body.trigger_reason.kind == "manual":
        raise ApiError(
            "SYSTEM_MANUAL_OPTIMIZATION_FORBIDDEN",
            "系统身份不能伪造人工手动优化触发",
            403,
        )
    label_version = session.scalar(
        select(LabelVersion).where(
            LabelVersion.label_version_id == request_body.label_version_id,
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
        )
    )
    prompt_version = session.scalar(
        select(PromptVersion).where(
            PromptVersion.prompt_version_id == request_body.prompt_version_id,
            PromptVersion.tenant_id == ctx.tenant_id,
            PromptVersion.project_id == ctx.project_id,
        )
    )
    aggregation_policy = session.scalar(
        select(LabelAggregationPolicyVersion).where(
            LabelAggregationPolicyVersion.policy_version_id
            == request_body.aggregation_policy_version_id,
            LabelAggregationPolicyVersion.tenant_id == ctx.tenant_id,
            LabelAggregationPolicyVersion.project_id == ctx.project_id,
        )
    )
    eval_dataset = session.scalar(
        select(EvalDatasetVersion).where(
            EvalDatasetVersion.eval_dataset_id == request_body.eval_dataset_version_id,
            EvalDatasetVersion.tenant_id == ctx.tenant_id,
            EvalDatasetVersion.project_id == ctx.project_id,
        )
    )
    missing = [
        name
        for name, value in (
            ("label_version_id", label_version),
            ("prompt_version_id", prompt_version),
            ("aggregation_policy_version_id", aggregation_policy),
            ("eval_dataset_version_id", eval_dataset),
        )
        if value is None
    ]
    if missing:
        raise ApiError(
            "LABEL_OPTIMIZATION_LOCKED_VERSION_NOT_FOUND",
            "优化运行的锁定版本在当前租户项目中不存在",
            404,
            details=[{"fields": missing}],
        )
    assert label_version is not None
    assert prompt_version is not None
    assert aggregation_policy is not None
    assert eval_dataset is not None
    if prompt_version.label_version_id != request_body.label_version_id:
        raise ApiError(
            "LABEL_OPTIMIZATION_PROMPT_BINDING_MISMATCH",
            "PromptVersion 未绑定锁定的 LabelVersion",
            409,
        )
    if (
        aggregation_policy.label_version_id != request_body.label_version_id
        or aggregation_policy.status != "active"
    ):
        raise ApiError(
            "LABEL_OPTIMIZATION_POLICY_BINDING_INVALID",
            "聚合策略必须绑定锁定标签版本且处于 active",
            409,
        )
    if eval_dataset.status != "locked":
        raise ApiError(
            "LABEL_OPTIMIZATION_DATASET_NOT_LOCKED",
            "优化运行必须绑定已锁定评测集版本",
            409,
        )

    trigger_document = {
        "tenant_id": ctx.tenant_id,
        "project_id": ctx.project_id,
        "label_version_id": request_body.label_version_id,
        "prompt_version_id": request_body.prompt_version_id,
        "aggregation_policy_version_id": request_body.aggregation_policy_version_id,
        "eval_dataset_version_id": request_body.eval_dataset_version_id,
        "trigger_reason": request_body.trigger_reason.model_dump(mode="json"),
    }
    trigger_hash = hashlib.sha256(
        json.dumps(
            trigger_document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    scoped_runs = list(
        session.scalars(
            select(RunRecord).where(
                RunRecord.tenant_id == ctx.tenant_id,
                RunRecord.project_id == ctx.project_id,
                RunRecord.run_type == "label_optimization",
            )
        )
    )
    scoped_runs = [
        run
        for run in scoped_runs
        if run.payload.get("label_version_id") == request_body.label_version_id
    ]
    active = [
        run
        for run in scoped_runs
        if run.status
        not in {"blocked", "cancelled", "completed", "failed", "rolled-back", "success"}
    ]
    if active:
        raise ApiError(
            "LABEL_OPTIMIZATION_ACTIVE_RUN_EXISTS",
            "同一租户、项目、标签版本仅允许一个活跃优化运行",
            409,
            details=[{"run_id": active[0].run_id, "status": active[0].status}],
        )
    now = datetime.now(UTC)
    cooldown_cutoff = now - timedelta(hours=24)
    for previous in scoped_runs:
        created_at = previous.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if created_at >= cooldown_cutoff:
            raise ApiError(
                "LABEL_OPTIMIZATION_COOLDOWN_ACTIVE",
                "同一标签版本的优化运行处于 24 小时冷却期",
                409,
                details=[{"run_id": previous.run_id, "created_at": created_at.isoformat()}],
            )

    run_id = request_body.optimization_run_id or public_id_from_hex(
        "label_optimization",
        trigger_hash,
        suffix_length=24,
    )
    locked_versions = {
        "label_version_id": request_body.label_version_id,
        "prompt_version_id": request_body.prompt_version_id,
        "model_version": request_body.model_version,
        "aggregation_policy_version_id": request_body.aggregation_policy_version_id,
        "eval_dataset_version_id": request_body.eval_dataset_version_id,
    }
    return await create_run(
        session,
        ctx,
        request,
        run_type="label_optimization",
        event_type="agent_run.requested",
        payload={
            **body,
            "run_id": run_id,
            "stage": "queued",
            "locked_versions": locked_versions,
            "trigger_hash": trigger_hash,
            "blocked_reasons": [],
            "next_action": {
                "code": "generate-prompt-candidates",
                "label": "生成 2–5 个 P-CODE Prompt 候选",
            },
            "affected_objects": [
                {"type": "label_version", "id": request_body.label_version_id},
                {"type": "prompt_version", "id": request_body.prompt_version_id},
                {
                    "type": "aggregation_policy_version",
                    "id": request_body.aggregation_policy_version_id,
                },
                {"type": "eval_dataset", "id": request_body.eval_dataset_version_id},
            ],
        },
        status="queued",
    )


@router.get("/label-optimization-runs/{id}")
def get_label_optimization_runs_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_run(session, ctx, id), ctx)
