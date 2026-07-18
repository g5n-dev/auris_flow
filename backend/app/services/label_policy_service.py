from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import (
    RequestContext,
    project_member_roles,
    require_context_membership,
)
from app.core.errors import ApiError
from app.domain.label_policy import PolicyCompileError, compile_policy, evaluate_policy
from app.domain.label_policy.compiler import COMPILER_VERSION
from app.domain.label_policy.defaults import default_release_policy
from app.models import (
    HumanReviewTask,
    JsonResource,
    LabelCandidate,
    LabelConflict,
    LabelPolicyEvaluation,
    LabelPolicyVersion,
    LabelVersion,
    Project,
    RunRecord,
)
from app.repositories.label_policies import (
    find_policy_artifact,
    find_policy_evaluation,
    get_candidate_for_update,
    get_label_version_for_update,
    get_policy_version,
)
from app.schemas.label_policy import (
    ActorFacts,
    CandidateFacts,
    ConflictFacts,
    EvaluationFacts,
    EvidenceArtifactFacts,
    EvidenceFacts,
    ImpactFacts,
    LabelCandidateEvaluationRequest,
    LabelPolicyDSL,
    LabelPolicyFacts,
    LabelPolicyValidationRequest,
    LabelVersionPublishRequest,
    PolicyFactProvenance,
    ReleaseFacts,
    RequestFacts,
    ReviewFacts,
    TargetFacts,
)
from app.services.audit_service import record_audit
from app.services.label_lifecycle_compat_service import (
    LabelLifecycleDriftError,
    apply_label_version_lifecycle_fields,
    transition_label_version_artifact,
)
from app.services.resource_service import upsert_resource

CandidateSource = Literal[
    "human_confirmed",
    "verified_business_document",
    "deterministic_rule",
    "model_candidate",
    "llm_candidate",
    "low_confidence_inference",
]
RiskLevel = Literal["low", "medium", "high", "critical"]

RELEASE_METRIC_SCHEMA_VERSION = "label-eval-metrics/1"
MIN_RELEASE_PROCESSED_COUNT = 200
MIN_RELEASE_EFFECTIVE_COVERAGE_PPM = 950_000
PENDING_OR_BLOCKING_REVIEW_STATUSES = frozenset(
    {"draft", "pending", "blocked", "rejected", "escalate", "escalated"}
)
REJECTING_REVIEW_STATUSES = frozenset({"blocked", "rejected"})


@dataclass(frozen=True)
class _ReleaseEvalSnapshot:
    eval_run: JsonResource
    dataset: JsonResource
    metric_data: dict[str, Any]
    optimization_run_id: str
    dataset_version: str
    eligible_count: int
    processed_count: int
    skipped_count: int
    invalid_count: int
    abstain_count: int
    duplicate_count: int
    effective_count: int
    effective_coverage_ppm: int
    confusion_matrix: dict[str, int | dict[str, int]]


def _scoped_artifact_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:24]}"


def _policy_data(policy: LabelPolicyVersion) -> dict[str, Any]:
    return {
        "policy_version_id": policy.policy_version_id,
        "label_version_id": policy.label_version_id,
        "policy_key": policy.policy_key,
        "revision": policy.revision,
        "dsl_version": policy.dsl_version,
        "policy_kind": policy.policy_kind,
        "status": policy.status,
        "source_sha256": policy.source_sha256,
        "canonical_sha256": policy.canonical_sha256,
        "compiler_version": policy.compiler_version,
        "ast_nodes": policy.canonical_json.get("_compiler", {}).get("ast_nodes"),
        "max_depth": policy.canonical_json.get("_compiler", {}).get("max_depth"),
        "trace_id": policy.trace_id,
    }


def _evaluation_data(evaluation: LabelPolicyEvaluation) -> dict[str, Any]:
    return {
        "evaluation_id": evaluation.evaluation_id,
        "target_type": evaluation.target_type,
        "target_id": evaluation.target_id,
        "candidate_id": evaluation.candidate_id,
        "policy_version_id": evaluation.policy_version_id,
        "status": evaluation.status,
        "verdict": evaluation.verdict,
        "policy_sha256": evaluation.policy_sha256,
        "facts_sha256": evaluation.facts_sha256,
        "decision_sha256": evaluation.decision_sha256,
        "decision": evaluation.decision_json,
        "trace_id": evaluation.trace_id,
    }


def get_policy_data(
    session: Session,
    ctx: RequestContext,
    policy_version_id: str,
) -> dict[str, Any]:
    policy = get_policy_version(session, ctx, policy_version_id)
    if policy is None:
        raise ApiError("LABEL_POLICY_NOT_FOUND", "标签策略版本不存在", 404)
    return {**_policy_data(policy), "policy": policy.source_json}


def get_evaluation_data(
    session: Session,
    ctx: RequestContext,
    evaluation_id: str,
) -> dict[str, Any]:
    evaluation = session.scalar(
        select(LabelPolicyEvaluation).where(
            LabelPolicyEvaluation.evaluation_id == evaluation_id,
            LabelPolicyEvaluation.tenant_id == ctx.tenant_id,
            LabelPolicyEvaluation.project_id == ctx.project_id,
        )
    )
    if evaluation is None:
        raise ApiError("LABEL_POLICY_EVALUATION_NOT_FOUND", "标签策略评估不存在", 404)
    return {**_evaluation_data(evaluation), "facts": evaluation.facts_json}


def validate_label_policy(
    session: Session,
    ctx: RequestContext,
    *,
    label_version_id: str,
    request_body: LabelPolicyValidationRequest,
) -> dict[str, Any]:
    label_version = get_label_version_for_update(session, ctx, label_version_id)
    if label_version is None:
        raise ApiError("LABEL_VERSION_NOT_FOUND", "标签版本不存在", 404)
    if request_body.expected_label_resource_version is not None and (
        label_version.resource_version != request_body.expected_label_resource_version
    ):
        raise ApiError(
            "LABEL_VERSION_CONFLICT",
            "标签版本已被其他操作更新，请刷新后重试",
            409,
            details=[
                {
                    "expected_resource_version": request_body.expected_label_resource_version,
                    "actual_resource_version": label_version.resource_version,
                }
            ],
        )
    if request_body.activate and label_version.status == "published":
        raise ApiError(
            "PUBLISHED_LABEL_VERSION_IMMUTABLE",
            "已发布标签版本不能替换策略，请创建候选版本",
            409,
        )
    try:
        compiled = compile_policy(request_body.policy)
    except PolicyCompileError as exc:
        raise ApiError(
            exc.code,
            str(exc),
            422,
            details=[{"field": exc.path, "message": str(exc), "code": exc.code}],
        ) from exc

    existing = find_policy_artifact(
        session,
        ctx,
        label_version_id=label_version_id,
        canonical_sha256=compiled.canonical_sha256,
    )
    if existing is None:
        policy_version_id = _scoped_artifact_id(
            "lpv",
            ctx.tenant_id,
            ctx.project_id,
            label_version_id,
            compiled.canonical_sha256,
        )
        canonical_json = {
            **compiled.canonical_ast,
            "_compiler": {
                "ast_nodes": compiled.ast_nodes,
                "max_depth": compiled.max_depth,
                "execution_step_budget": compiled.execution_step_budget,
            },
        }
        existing = LabelPolicyVersion(
            policy_version_id=policy_version_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            label_version_id=label_version_id,
            policy_key=request_body.policy.policy_key,
            revision=request_body.policy.revision,
            dsl_version=request_body.policy.dsl_version,
            policy_kind=request_body.policy.policy_kind,
            status="validated",
            source_sha256=compiled.source_sha256,
            canonical_sha256=compiled.canonical_sha256,
            compiler_version=compiled.compiler_version,
            source_json=request_body.policy.model_dump(mode="json", exclude_none=True),
            canonical_json=canonical_json,
            trace_id=ctx.trace_id,
        )
        session.add(existing)
        record_audit(
            session,
            ctx,
            action="label_policy.validated",
            object_type="label_policy_version",
            object_id=policy_version_id,
            after={
                "label_version_id": label_version_id,
                "source_sha256": compiled.source_sha256,
                "canonical_sha256": compiled.canonical_sha256,
                "compiler_version": compiled.compiler_version,
            },
        )

    binding_field = (
        "policy_version_id"
        if request_body.policy.policy_kind == "label-candidate"
        else "release_gate_id"
    )
    active_policy_version_id = getattr(label_version, binding_field)
    if request_body.activate and active_policy_version_id != existing.policy_version_id:
        previous_policy_version_id = active_policy_version_id
        if previous_policy_version_id:
            previous_policy = get_policy_version(session, ctx, previous_policy_version_id)
            if previous_policy is not None:
                previous_policy.status = "validated"
        setattr(label_version, binding_field, existing.policy_version_id)
        existing.status = "active"
        label_version.resource_version += 1
        label_version.trace_id = ctx.trace_id
        label_version.payload = {
            **label_version.payload,
            binding_field: existing.policy_version_id,
            f"{binding_field}_sha256": existing.canonical_sha256,
            "resource_version": label_version.resource_version,
            "trace_id": ctx.trace_id,
        }
        try:
            apply_label_version_lifecycle_fields(
                label_version,
                label_version.payload,
                conflict_policy="raise",
            )
        except LabelLifecycleDriftError as exc:
            raise ApiError("LABEL_VERSION_STRONG_FIELD_DRIFT", str(exc), 409) from exc
        _update_label_version_projection(
            session,
            ctx,
            label_version_id=label_version_id,
            payload=label_version.payload,
        )
        record_audit(
            session,
            ctx,
            action="label_policy.activated",
            object_type="label_version",
            object_id=label_version_id,
            before={"policy_version_id": previous_policy_version_id},
            after={
                binding_field: existing.policy_version_id,
                "resource_version": label_version.resource_version,
            },
        )

    return {
        **_policy_data(existing),
        "valid": True,
        "active": getattr(label_version, binding_field) == existing.policy_version_id,
        "binding_field": binding_field,
        "label_resource_version": label_version.resource_version,
        "next_actions": [
            {"key": "evaluate_candidate", "label": "评估候选标签"},
            {"key": "view_trace", "label": "查看 Trace", "route": f"traces/{ctx.trace_id}"},
        ],
    }


def evaluate_label_candidate(
    session: Session,
    ctx: RequestContext,
    *,
    request_body: LabelCandidateEvaluationRequest,
) -> dict[str, Any]:
    candidate = get_candidate_for_update(session, ctx, request_body.candidate_id)
    if candidate is None:
        raise ApiError("LABEL_CANDIDATE_NOT_FOUND", "标签候选不存在", 404)
    if request_body.expected_candidate_resource_version is not None and (
        candidate.resource_version != request_body.expected_candidate_resource_version
    ):
        raise ApiError(
            "LABEL_CANDIDATE_CONFLICT",
            "标签候选已被其他操作更新，请刷新后重试",
            409,
        )
    candidate_label_version_id = candidate.payload.get("label_version_id")
    if not isinstance(candidate_label_version_id, str) or not candidate_label_version_id:
        raise ApiError(
            "LABEL_CANDIDATE_VERSION_REQUIRED",
            "标签候选必须绑定标签版本后才能评估",
            409,
        )
    label_version = get_label_version_for_update(
        session,
        ctx,
        candidate_label_version_id,
    )
    if label_version is None:
        raise ApiError("LABEL_VERSION_NOT_FOUND", "候选绑定的标签版本不存在", 404)
    active_policy_version_id = label_version.policy_version_id
    if active_policy_version_id is None:
        raise ApiError(
            "LABEL_POLICY_NOT_ACTIVE",
            "标签版本尚未激活候选策略",
            409,
        )
    if (
        request_body.policy_version_id is not None
        and request_body.policy_version_id != active_policy_version_id
    ):
        raise ApiError(
            "LABEL_POLICY_NOT_ACTIVE",
            "只能使用标签版本当前激活的候选策略",
            409,
        )
    policy_record = get_policy_version(session, ctx, active_policy_version_id)
    if policy_record is None:
        raise ApiError("LABEL_POLICY_NOT_FOUND", "标签策略版本不存在", 404)
    if policy_record.policy_kind != "label-candidate":
        raise ApiError(
            "LABEL_POLICY_KIND_MISMATCH",
            "候选评估只能使用 label-candidate 策略",
            422,
        )
    if candidate_label_version_id != policy_record.label_version_id:
        raise ApiError(
            "LABEL_POLICY_BINDING_MISMATCH",
            "策略未绑定到候选标签版本",
            409,
        )
    if policy_record.status != "active":
        raise ApiError(
            "LABEL_POLICY_NOT_ACTIVE",
            "候选策略不是激活状态",
            409,
        )

    if policy_record.compiler_version != COMPILER_VERSION:
        raise ApiError(
            "LABEL_POLICY_ENGINE_VERSION_UNSUPPORTED",
            "标签策略编译器版本不受当前执行引擎支持",
            409,
        )
    policy = LabelPolicyDSL.model_validate(policy_record.source_json)
    try:
        compiled = compile_policy(policy)
    except PolicyCompileError as exc:
        raise ApiError(exc.code, str(exc), 409) from exc
    if (
        compiled.canonical_sha256 != policy_record.canonical_sha256
        or compiled.source_sha256 != policy_record.source_sha256
    ):
        raise ApiError(
            "LABEL_POLICY_ARTIFACT_TAMPERED",
            "标签策略产物哈希校验失败",
            409,
        )

    facts = _authoritative_candidate_facts(
        session,
        ctx,
        candidate.payload,
        candidate_id=candidate.candidate_id,
        candidate_status=candidate.status,
        label_version_id=candidate_label_version_id,
        candidate_resource_version=candidate.resource_version,
        label_resource_version=label_version.resource_version,
    )
    decision = evaluate_policy(compiled, facts)
    decision_json = decision.to_dict()
    existing = find_policy_evaluation(
        session,
        ctx,
        target_type="label_candidate",
        target_id=candidate.candidate_id,
        policy_version_id=policy_record.policy_version_id,
        facts_sha256=decision.facts_sha256,
    )
    if existing is not None:
        replay_conflict_id = _scoped_artifact_id(
            "lcf", ctx.tenant_id, ctx.project_id, existing.evaluation_id
        )
        replay_review_task_id = _scoped_artifact_id(
            "hrt", ctx.tenant_id, ctx.project_id, existing.evaluation_id
        )
        conflict_exists = session.get(LabelConflict, replay_conflict_id) is not None
        review_exists = session.scalar(
            select(HumanReviewTask.review_task_id).where(
                HumanReviewTask.review_task_id == replay_review_task_id,
                HumanReviewTask.tenant_id == ctx.tenant_id,
                HumanReviewTask.project_id == ctx.project_id,
            )
        )
        return {
            **_evaluation_data(existing),
            "candidate_status": candidate.status,
            "candidate_resource_version": candidate.resource_version,
            "conflict_id": replay_conflict_id if conflict_exists else None,
            "review_task_id": replay_review_task_id if review_exists else None,
            "replayed": True,
        }

    evaluation_id = _scoped_artifact_id(
        "lpe",
        ctx.tenant_id,
        ctx.project_id,
        candidate.candidate_id,
        policy_record.policy_version_id,
        decision.facts_sha256,
    )
    evaluation = LabelPolicyEvaluation(
        evaluation_id=evaluation_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        target_type="label_candidate",
        target_id=candidate.candidate_id,
        candidate_id=candidate.candidate_id,
        policy_version_id=policy_record.policy_version_id,
        status="evaluated",
        verdict=decision.verdict,
        policy_sha256=decision.policy_sha256,
        facts_sha256=decision.facts_sha256,
        decision_sha256=decision_json["decision_sha256"],
        facts_json=facts.model_dump(mode="json", exclude_none=False),
        decision_json=decision_json,
        trace_id=ctx.trace_id,
    )
    session.add(evaluation)

    previous_candidate = {
        "status": candidate.status,
        "resource_version": candidate.resource_version,
        "policy_evaluation_id": candidate.payload.get("policy_evaluation_id"),
    }
    candidate.status = {
        "pass": "evaluated",
        "gray_only": "pending_review",
        "require_review": "pending_review",
        "block": "blocked",
    }[decision.verdict]
    candidate.resource_version += 1
    candidate.trace_id = ctx.trace_id
    candidate.payload = {
        **candidate.payload,
        "status": candidate.status,
        "policy_version_id": policy_record.policy_version_id,
        "policy_evaluation_id": evaluation_id,
        "policy_verdict": decision.verdict,
        "policy_decision_sha256": decision_json["decision_sha256"],
        "policy_input_status": previous_candidate["status"],
        "policy_input_resource_version": previous_candidate["resource_version"],
        "policy_output_resource_version": candidate.resource_version,
        "resource_version": candidate.resource_version,
        "trace_id": ctx.trace_id,
    }
    _update_candidate_projection(
        session,
        ctx,
        candidate_id=candidate.candidate_id,
        payload=candidate.payload,
        status=candidate.status,
    )

    conflict_id: str | None = None
    review_task_id: str | None = None
    if decision.verdict in {"require_review", "block"}:
        conflict_id = _create_conflict(
            session,
            ctx,
            evaluation=evaluation,
            decision=decision_json,
            evidence_pack_id=candidate.payload.get("evidence_pack_id"),
        )
        if request_body.create_human_review:
            review_task_id = _create_review_task(
                session,
                ctx,
                evaluation=evaluation,
                conflict_id=conflict_id,
                evidence_pack_id=candidate.payload.get("evidence_pack_id"),
            )

    record_audit(
        session,
        ctx,
        action="label_candidate.policy_evaluated",
        object_type="label_candidate",
        object_id=candidate.candidate_id,
        before=previous_candidate,
        after={
            "status": candidate.status,
            "resource_version": candidate.resource_version,
            "policy_evaluation_id": evaluation_id,
            "decision_sha256": decision_json["decision_sha256"],
            "verdict": decision.verdict,
            "conflict_id": conflict_id,
            "review_task_id": review_task_id,
        },
    )
    return {
        **_evaluation_data(evaluation),
        "candidate_status": candidate.status,
        "candidate_resource_version": candidate.resource_version,
        "conflict_id": conflict_id,
        "review_task_id": review_task_id,
        "replayed": False,
        "next_actions": [
            {
                "key": "human_review" if review_task_id else "view_candidate",
                "label": "进入人工复核" if review_task_id else "查看候选",
                "id": review_task_id or candidate.candidate_id,
            },
            {"key": "view_trace", "label": "查看 Trace", "route": f"traces/{ctx.trace_id}"},
        ],
    }


def evaluate_label_version_release(
    session: Session,
    ctx: RequestContext,
    *,
    label_version_id: str,
    request_body: LabelVersionPublishRequest,
) -> dict[str, Any]:
    label_version = get_label_version_for_update(session, ctx, label_version_id)
    if label_version is None:
        raise ApiError("LABEL_VERSION_NOT_FOUND", "标签版本不存在", 404)
    if label_version.status == "published":
        raise ApiError(
            "PUBLISHED_LABEL_VERSION_IMMUTABLE",
            "已发布标签版本不能重复发布",
            409,
        )
    if request_body.expected_label_resource_version is not None and (
        label_version.resource_version != request_body.expected_label_resource_version
    ):
        raise ApiError(
            "LABEL_VERSION_CONFLICT",
            "标签版本已被其他操作更新，请刷新后重试",
            409,
        )

    if label_version.release_gate_id is None:
        validate_label_policy(
            session,
            ctx,
            label_version_id=label_version_id,
            request_body=LabelPolicyValidationRequest(
                policy=default_release_policy(),
                activate=True,
                expected_label_resource_version=label_version.resource_version,
            ),
        )
        session.flush()
    policy_version_id = label_version.release_gate_id
    if policy_version_id is None:
        raise ApiError("LABEL_RELEASE_POLICY_NOT_ACTIVE", "标签发布策略未激活", 409)
    policy_record = get_policy_version(session, ctx, policy_version_id)
    if (
        policy_record is None
        or policy_record.status != "active"
        or policy_record.policy_kind != "label-version-release"
        or policy_record.label_version_id != label_version_id
    ):
        raise ApiError(
            "LABEL_RELEASE_POLICY_NOT_ACTIVE",
            "标签版本绑定的发布策略无效",
            409,
        )

    if policy_record.compiler_version != COMPILER_VERSION:
        raise ApiError(
            "LABEL_POLICY_ENGINE_VERSION_UNSUPPORTED",
            "标签发布策略编译器版本不受当前执行引擎支持",
            409,
        )
    policy = LabelPolicyDSL.model_validate(policy_record.source_json)
    try:
        compiled = compile_policy(policy)
    except PolicyCompileError as exc:
        raise ApiError(exc.code, str(exc), 409) from exc
    if (
        compiled.canonical_sha256 != policy_record.canonical_sha256
        or compiled.source_sha256 != policy_record.source_sha256
    ):
        raise ApiError(
            "LABEL_POLICY_ARTIFACT_TAMPERED",
            "标签发布策略产物哈希校验失败",
            409,
        )

    facts = _authoritative_release_facts(
        session,
        ctx,
        label_version,
        request_body=request_body,
    )
    decision = evaluate_policy(compiled, facts)
    decision_json = decision.to_dict()
    existing = find_policy_evaluation(
        session,
        ctx,
        target_type="label_version",
        target_id=label_version_id,
        policy_version_id=policy_version_id,
        facts_sha256=decision.facts_sha256,
    )
    if existing is not None:
        return {
            **_evaluation_data(existing),
            "label_resource_version": label_version.resource_version,
            "replayed": True,
        }

    evaluation_id = _scoped_artifact_id(
        "lpe",
        ctx.tenant_id,
        ctx.project_id,
        label_version_id,
        policy_version_id,
        decision.facts_sha256,
    )
    evaluation = LabelPolicyEvaluation(
        evaluation_id=evaluation_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        target_type="label_version",
        target_id=label_version_id,
        candidate_id=None,
        policy_version_id=policy_version_id,
        status="evaluated",
        verdict=decision.verdict,
        policy_sha256=decision.policy_sha256,
        facts_sha256=decision.facts_sha256,
        decision_sha256=decision_json["decision_sha256"],
        facts_json=facts.model_dump(mode="json", exclude_none=False),
        decision_json=decision_json,
        trace_id=ctx.trace_id,
    )
    session.add(evaluation)
    record_audit(
        session,
        ctx,
        action="label_version.release_policy_evaluated",
        object_type="label_version",
        object_id=label_version_id,
        before={"resource_version": label_version.resource_version},
        after={
            "resource_version": label_version.resource_version,
            "policy_version_id": policy_version_id,
            "policy_evaluation_id": evaluation_id,
            "verdict": decision.verdict,
            "decision_sha256": decision_json["decision_sha256"],
        },
    )
    return {
        **_evaluation_data(evaluation),
        "label_resource_version": label_version.resource_version,
        "replayed": False,
    }


def revalidate_label_version_release_dispatch(
    session: Session,
    run: RunRecord,
) -> dict[str, Any]:
    payload = run.payload
    label_version_id = payload.get("label_version_id")
    evaluation_id = payload.get("release_policy_evaluation_id")
    policy_version_id = payload.get("release_policy_version_id")
    approved_by = payload.get("approved_by")
    raw_roles = payload.get("approval_roles")
    approval_roles = (
        tuple(role for role in raw_roles if isinstance(role, str) and role)
        if isinstance(raw_roles, list)
        else ()
    )
    if (
        not isinstance(label_version_id, str)
        or not label_version_id
        or not isinstance(evaluation_id, str)
        or not evaluation_id
        or not isinstance(policy_version_id, str)
        or not policy_version_id
        or not isinstance(approved_by, str)
        or not approved_by
        or not approval_roles
        or "system" in approval_roles
    ):
        return {"allowed": False, "reason_code": "RELEASE_APPROVAL_CONTEXT_INVALID"}

    approval_ctx = RequestContext(
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        user_id=approved_by,
        roles=approval_roles,
        request_id=f"release-dispatch:{run.run_id}",
        trace_id=run.trace_id,
        idempotency_key=str(payload.get("request_idempotency_key") or "") or None,
    )
    try:
        require_context_membership(session, approval_ctx)
    except ApiError as exc:
        if exc.code == "TOKEN_PROJECT_ROLE_MISMATCH":
            project = session.get(Project, run.project_id)
            current_roles = (
                project_member_roles(project, approved_by) if project is not None else ()
            )
            return {
                "allowed": False,
                "reason_code": "RELEASE_APPROVER_ROLE_REVOKED",
                "current_roles": list(current_roles),
            }
        return {
            "allowed": False,
            "reason_code": "RELEASE_APPROVER_NO_LONGER_AUTHORIZED",
            "authorization_error": exc.code,
        }
    project = session.get(Project, run.project_id)
    current_roles = project_member_roles(project, approved_by) if project is not None else ()
    if not {"project_admin", "model_engineer"}.intersection(current_roles):
        return {
            "allowed": False,
            "reason_code": "RELEASE_APPROVER_ROLE_REVOKED",
            "current_roles": list(current_roles),
        }
    approval_ctx = RequestContext(
        tenant_id=approval_ctx.tenant_id,
        project_id=approval_ctx.project_id,
        user_id=approval_ctx.user_id,
        roles=current_roles,
        request_id=approval_ctx.request_id,
        trace_id=approval_ctx.trace_id,
        idempotency_key=approval_ctx.idempotency_key,
    )

    label_version = get_label_version_for_update(
        session,
        approval_ctx,
        label_version_id,
    )
    expected_resource_version = _nonnegative_int(payload.get("release_label_resource_version"))
    if label_version is None:
        return {"allowed": False, "reason_code": "LABEL_VERSION_NOT_FOUND"}
    if expected_resource_version != label_version.resource_version:
        return {
            "allowed": False,
            "reason_code": "RELEASE_LABEL_VERSION_CHANGED",
            "expected_resource_version": expected_resource_version,
            "current_resource_version": label_version.resource_version,
        }
    if label_version.release_gate_id != policy_version_id:
        return {"allowed": False, "reason_code": "RELEASE_POLICY_BINDING_CHANGED"}

    evaluation = session.scalar(
        select(LabelPolicyEvaluation).where(
            LabelPolicyEvaluation.evaluation_id == evaluation_id,
            LabelPolicyEvaluation.tenant_id == run.tenant_id,
            LabelPolicyEvaluation.project_id == run.project_id,
            LabelPolicyEvaluation.target_type == "label_version",
            LabelPolicyEvaluation.target_id == label_version_id,
            LabelPolicyEvaluation.policy_version_id == policy_version_id,
        )
    )
    if evaluation is None or evaluation.verdict not in {"pass", "gray_only"}:
        return {"allowed": False, "reason_code": "RELEASE_EVALUATION_NOT_APPROVED"}
    if (
        payload.get("release_policy_facts_sha256") != evaluation.facts_sha256
        or payload.get("release_policy_decision_sha256") != evaluation.decision_sha256
        or payload.get("release_policy_verdict") != evaluation.verdict
    ):
        return {"allowed": False, "reason_code": "RELEASE_EVALUATION_POINTER_TAMPERED"}

    policy_record = get_policy_version(session, approval_ctx, policy_version_id)
    if (
        policy_record is None
        or policy_record.status != "active"
        or policy_record.policy_kind != "label-version-release"
        or policy_record.label_version_id != label_version_id
        or policy_record.compiler_version != COMPILER_VERSION
    ):
        return {"allowed": False, "reason_code": "RELEASE_POLICY_NOT_EXECUTABLE"}
    policy = LabelPolicyDSL.model_validate(policy_record.source_json)
    try:
        compiled = compile_policy(policy)
    except PolicyCompileError as exc:
        return {
            "allowed": False,
            "reason_code": "RELEASE_POLICY_COMPILE_FAILED",
            "policy_error": exc.code,
        }
    if (
        compiled.canonical_sha256 != policy_record.canonical_sha256
        or compiled.source_sha256 != policy_record.source_sha256
    ):
        return {"allowed": False, "reason_code": "RELEASE_POLICY_ARTIFACT_TAMPERED"}

    eval_run_id = payload.get("eval_run_id")
    gray_traffic_ppm = payload.get("gray_traffic_ppm", 100_000)
    if (
        not isinstance(eval_run_id, str)
        or not eval_run_id
        or not isinstance(gray_traffic_ppm, int)
        or isinstance(gray_traffic_ppm, bool)
    ):
        return {"allowed": False, "reason_code": "RELEASE_EVAL_BINDING_INVALID"}
    try:
        request_body = LabelVersionPublishRequest(
            eval_run_id=eval_run_id,
            gray_traffic_ppm=gray_traffic_ppm,
        )
    except ValueError:
        return {"allowed": False, "reason_code": "RELEASE_EVAL_BINDING_INVALID"}
    try:
        facts = _authoritative_release_facts(
            session,
            approval_ctx,
            label_version,
            request_body=request_body,
        )
    except ApiError as exc:
        return {
            "allowed": False,
            "reason_code": "RELEASE_FACTS_INVALID",
            "validation_error_code": exc.code,
        }
    decision = evaluate_policy(compiled, facts)
    current_decision = decision.to_dict()
    if (
        decision.facts_sha256 != evaluation.facts_sha256
        or current_decision["decision_sha256"] != evaluation.decision_sha256
        or decision.verdict != evaluation.verdict
    ):
        return {
            "allowed": False,
            "reason_code": "RELEASE_FACTS_CHANGED",
            "approved_facts_sha256": evaluation.facts_sha256,
            "current_facts_sha256": decision.facts_sha256,
            "current_verdict": decision.verdict,
        }
    return {
        "allowed": True,
        "reason_code": "RELEASE_FACTS_REVALIDATED",
        "label_version_id": label_version_id,
        "policy_version_id": policy_version_id,
        "evaluation_id": evaluation_id,
        "verdict": decision.verdict,
        "facts_sha256": decision.facts_sha256,
        "decision_sha256": current_decision["decision_sha256"],
        "approval_ctx": approval_ctx,
        "label_version": label_version,
    }


def materialize_label_version_release(
    session: Session,
    run: RunRecord,
) -> dict[str, Any]:
    gate = revalidate_label_version_release_dispatch(session, run)
    if gate.get("allowed") is not True:
        return gate
    approval_ctx = cast(RequestContext, gate["approval_ctx"])
    label_version = cast(LabelVersion, gate["label_version"])
    target_status = "gray_releasing" if gate["verdict"] == "gray_only" else "published"
    if (
        label_version.status == target_status
        and label_version.payload.get("release_run_id") == run.run_id
    ):
        try:
            transition_label_version_artifact(label_version, "published")
        except LabelLifecycleDriftError as exc:
            raise ApiError("LABEL_VERSION_STRONG_FIELD_DRIFT", str(exc), 409) from exc
        return {**gate, "materialized": False, "status": target_status}

    before = {
        "status": label_version.status,
        "resource_version": label_version.resource_version,
    }
    published_at = datetime.now(UTC)
    now = published_at.isoformat()
    try:
        transition_label_version_artifact(
            label_version,
            "published",
            occurred_at=published_at,
        )
    except LabelLifecycleDriftError as exc:
        raise ApiError("LABEL_VERSION_STRONG_FIELD_DRIFT", str(exc), 409) from exc
    label_version.status = target_status
    label_version.resource_version += 1
    label_version.trace_id = run.trace_id
    release_fields = {
        "release_run_id": run.run_id,
        "release_policy_version_id": gate["policy_version_id"],
        "release_policy_evaluation_id": gate["evaluation_id"],
        "release_policy_verdict": gate["verdict"],
        "release_policy_facts_sha256": gate["facts_sha256"],
        "release_policy_decision_sha256": gate["decision_sha256"],
        "gray_traffic_ppm": run.payload.get("gray_traffic_ppm"),
        "published_by": approval_ctx.user_id,
        "gray_started_at" if target_status == "gray_releasing" else "published_at": now,
    }
    label_version.payload = {
        **label_version.payload,
        **release_fields,
        "status": target_status,
        "artifact_status": "published",
        "artifact_published_at": now,
        "resource_version": label_version.resource_version,
        "trace_id": run.trace_id,
    }
    _update_label_version_projection(
        session,
        approval_ctx,
        label_version_id=label_version.label_version_id,
        payload=label_version.payload,
    )
    record_audit(
        session,
        approval_ctx,
        action=(
            "label_version.gray_released"
            if target_status == "gray_releasing"
            else "label_version.published"
        ),
        object_type="label_version",
        object_id=label_version.label_version_id,
        before=before,
        after={
            "status": target_status,
            "resource_version": label_version.resource_version,
            **release_fields,
        },
    )
    return {**gate, "materialized": True, "status": target_status}


def _ratio_to_ppm(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if number < 0:
        return None
    if number <= 1:
        result = int(number * 1_000_000)
    elif number <= 100:
        result = int(number * 10_000)
    else:
        return None
    return result if result <= 1_000_000 else None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _risk_level(value: object) -> RiskLevel | None:
    if value in {"low", "medium", "high", "critical"}:
        return cast(RiskLevel, value)
    return None


def _normalize_confusion_matrix(
    value: object,
) -> tuple[dict[str, int | dict[str, int]], int] | None:
    if not isinstance(value, dict) or not value:
        return None
    normalized: dict[str, int | dict[str, int]] = {}
    total = 0
    for key in sorted(value):
        if not isinstance(key, str) or not key:
            return None
        cell = value[key]
        count = _nonnegative_int(cell)
        if count is not None:
            normalized[key] = count
            total += count
            continue
        if not isinstance(cell, dict) or not cell:
            return None
        row: dict[str, int] = {}
        for column in sorted(cell):
            if not isinstance(column, str) or not column:
                return None
            nested_count = _nonnegative_int(cell[column])
            if nested_count is None:
                return None
            row[column] = nested_count
            total += nested_count
        normalized[key] = row
    return normalized, total


def _release_eval_count(
    metric_data: dict[str, Any],
    key: str,
    *,
    eval_run_id: str,
) -> int:
    value = _nonnegative_int(metric_data.get(key))
    if value is None:
        raise ApiError(
            "LABEL_RELEASE_EVAL_COUNTS_INVALID",
            "评测运行缺少完整、非负的实际计数",
            409,
            details=[{"eval_run_id": eval_run_id, "field": f"metrics.{key}"}],
        )
    return value


def _load_release_eval_snapshot(
    session: Session,
    ctx: RequestContext,
    label_version: LabelVersion,
    *,
    eval_run_id: str,
) -> _ReleaseEvalSnapshot:
    eval_run = session.scalar(
        select(JsonResource).where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "eval_runs",
            JsonResource.resource_key == eval_run_id,
        )
    )
    if eval_run is None:
        raise ApiError(
            "LABEL_RELEASE_EVAL_RUN_NOT_FOUND",
            "当前租户和项目下不存在指定的评测运行",
            409,
            details=[{"eval_run_id": eval_run_id}],
        )

    eval_data = eval_run.data
    if eval_data.get("eval_run_id") != eval_run_id:
        raise ApiError(
            "LABEL_RELEASE_EVAL_RUN_ID_MISMATCH",
            "评测运行投影 ID 与请求不一致",
            409,
            details=[
                {
                    "requested_eval_run_id": eval_run_id,
                    "projected_eval_run_id": eval_data.get("eval_run_id"),
                }
            ],
        )
    if eval_run.status != "success" or eval_data.get("status") != "success":
        raise ApiError(
            "LABEL_RELEASE_EVAL_RUN_NOT_SUCCESSFUL",
            "标签发布只接受状态一致且为 success 的评测运行",
            409,
            details=[
                {
                    "eval_run_id": eval_run_id,
                    "projection_status": eval_run.status,
                    "payload_status": eval_data.get("status"),
                }
            ],
        )

    projected_label_version_id = eval_data.get("label_version_id")
    if projected_label_version_id != label_version.label_version_id:
        raise ApiError(
            "LABEL_RELEASE_LABEL_VERSION_MISMATCH",
            "评测运行不属于当前待发布标签版本",
            409,
            details=[
                {
                    "eval_run_id": eval_run_id,
                    "expected_label_version_id": label_version.label_version_id,
                    "actual_label_version_id": projected_label_version_id,
                }
            ],
        )

    expected_optimization_run_id = label_version.payload.get("optimization_run_id")
    optimization_run_id = eval_data.get("optimization_run_id")
    if (
        not isinstance(expected_optimization_run_id, str)
        or not expected_optimization_run_id
        or not isinstance(optimization_run_id, str)
        or not optimization_run_id
        or optimization_run_id != expected_optimization_run_id
    ):
        raise ApiError(
            "LABEL_RELEASE_OPTIMIZATION_RUN_MISMATCH",
            "评测运行与标签版本的优化运行不一致",
            409,
            details=[
                {
                    "eval_run_id": eval_run_id,
                    "expected_optimization_run_id": expected_optimization_run_id,
                    "actual_optimization_run_id": optimization_run_id,
                }
            ],
        )

    dataset_id = eval_data.get("dataset_id")
    dataset_version = eval_data.get("dataset_version")
    if (
        not isinstance(dataset_id, str)
        or not dataset_id
        or not isinstance(dataset_version, str)
        or not dataset_version
    ):
        raise ApiError(
            "LABEL_RELEASE_EVAL_DATASET_BINDING_INVALID",
            "评测运行必须显式绑定评测集 ID 和版本",
            409,
            details=[{"eval_run_id": eval_run_id}],
        )
    dataset = session.scalar(
        select(JsonResource).where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "eval_datasets",
            JsonResource.resource_key == dataset_id,
        )
    )
    if dataset is None or dataset.data.get("dataset_id") != dataset_id:
        raise ApiError(
            "LABEL_RELEASE_EVAL_DATASET_NOT_FOUND",
            "当前租户和项目下不存在评测运行绑定的数据集版本",
            409,
            details=[{"eval_run_id": eval_run_id, "dataset_id": dataset_id}],
        )
    if dataset.data.get("dataset_version") != dataset_version:
        raise ApiError(
            "LABEL_RELEASE_EVAL_DATASET_VERSION_MISMATCH",
            "评测运行绑定的数据集版本已不是当前锁定版本",
            409,
            details=[
                {
                    "eval_run_id": eval_run_id,
                    "dataset_id": dataset_id,
                    "eval_dataset_version": dataset_version,
                    "stored_dataset_version": dataset.data.get("dataset_version"),
                }
            ],
        )
    if dataset.status != "locked" or dataset.data.get("status") != "locked":
        raise ApiError(
            "LABEL_RELEASE_EVAL_DATASET_NOT_LOCKED",
            "标签发布只接受状态一致且为 locked 的评测集版本",
            409,
            details=[
                {
                    "eval_run_id": eval_run_id,
                    "dataset_id": dataset_id,
                    "projection_status": dataset.status,
                    "payload_status": dataset.data.get("status"),
                }
            ],
        )

    metrics = eval_data.get("metrics")
    if not isinstance(metrics, dict):
        raise ApiError(
            "LABEL_RELEASE_EVAL_METRICS_INVALID",
            "评测运行缺少结构化指标事实",
            409,
            details=[{"eval_run_id": eval_run_id}],
        )
    metric_data = cast(dict[str, Any], metrics)
    if metric_data.get("metric_schema_version") != RELEASE_METRIC_SCHEMA_VERSION:
        raise ApiError(
            "LABEL_RELEASE_METRIC_SCHEMA_UNSUPPORTED",
            "评测指标口径版本不受发布门禁支持",
            409,
            details=[
                {
                    "eval_run_id": eval_run_id,
                    "expected_metric_schema_version": RELEASE_METRIC_SCHEMA_VERSION,
                    "actual_metric_schema_version": metric_data.get("metric_schema_version"),
                }
            ],
        )

    eligible_count = _release_eval_count(metric_data, "eligible_count", eval_run_id=eval_run_id)
    processed_count = _release_eval_count(metric_data, "processed_count", eval_run_id=eval_run_id)
    skipped_count = _release_eval_count(metric_data, "skipped_count", eval_run_id=eval_run_id)
    invalid_count = _release_eval_count(metric_data, "invalid_count", eval_run_id=eval_run_id)
    abstain_count = _release_eval_count(metric_data, "abstain_count", eval_run_id=eval_run_id)
    duplicate_count = _release_eval_count(metric_data, "duplicate_count", eval_run_id=eval_run_id)
    normalized_confusion = _normalize_confusion_matrix(metric_data.get("confusion_matrix"))
    if normalized_confusion is None:
        raise ApiError(
            "LABEL_RELEASE_CONFUSION_MATRIX_INVALID",
            "评测运行缺少可守恒校验的混淆矩阵",
            409,
            details=[{"eval_run_id": eval_run_id}],
        )
    confusion_matrix, effective_count = normalized_confusion
    expected_eligible_count = processed_count + skipped_count + invalid_count + duplicate_count
    expected_processed_count = effective_count + abstain_count
    if eligible_count != expected_eligible_count or processed_count != expected_processed_count:
        raise ApiError(
            "LABEL_RELEASE_EVAL_COUNTS_INCONSISTENT",
            "评测运行实际计数与混淆矩阵不守恒",
            409,
            details=[
                {
                    "eval_run_id": eval_run_id,
                    "eligible_count": eligible_count,
                    "expected_eligible_count": expected_eligible_count,
                    "processed_count": processed_count,
                    "expected_processed_count": expected_processed_count,
                }
            ],
        )
    if processed_count < MIN_RELEASE_PROCESSED_COUNT:
        raise ApiError(
            "LABEL_RELEASE_PROCESSED_COUNT_TOO_SMALL",
            "评测运行实际处理样本数不足，不能进入灰度或发布",
            409,
            details=[
                {
                    "eval_run_id": eval_run_id,
                    "processed_count": processed_count,
                    "minimum_processed_count": MIN_RELEASE_PROCESSED_COUNT,
                }
            ],
        )
    effective_coverage_ppm = (
        effective_count * 1_000_000 // eligible_count if eligible_count > 0 else 0
    )
    if effective_coverage_ppm < MIN_RELEASE_EFFECTIVE_COVERAGE_PPM:
        raise ApiError(
            "LABEL_RELEASE_EFFECTIVE_COVERAGE_TOO_LOW",
            "评测运行有效覆盖率不足，不能进入灰度或发布",
            409,
            details=[
                {
                    "eval_run_id": eval_run_id,
                    "effective_count": effective_count,
                    "eligible_count": eligible_count,
                    "effective_coverage_ppm": effective_coverage_ppm,
                    "minimum_effective_coverage_ppm": MIN_RELEASE_EFFECTIVE_COVERAGE_PPM,
                }
            ],
        )

    return _ReleaseEvalSnapshot(
        eval_run=eval_run,
        dataset=dataset,
        metric_data=metric_data,
        optimization_run_id=optimization_run_id,
        dataset_version=dataset_version,
        eligible_count=eligible_count,
        processed_count=processed_count,
        skipped_count=skipped_count,
        invalid_count=invalid_count,
        abstain_count=abstain_count,
        duplicate_count=duplicate_count,
        effective_count=effective_count,
        effective_coverage_ppm=effective_coverage_ppm,
        confusion_matrix=confusion_matrix,
    )


def _authoritative_release_facts(
    session: Session,
    ctx: RequestContext,
    label_version: LabelVersion,
    *,
    request_body: LabelVersionPublishRequest,
) -> LabelPolicyFacts:
    snapshot = _load_release_eval_snapshot(
        session,
        ctx,
        label_version,
        eval_run_id=request_body.eval_run_id,
    )
    eval_run = snapshot.eval_run
    dataset = snapshot.dataset
    metric_data = snapshot.metric_data

    candidates = list(
        session.scalars(
            select(LabelCandidate).where(
                LabelCandidate.tenant_id == ctx.tenant_id,
                LabelCandidate.project_id == ctx.project_id,
            )
        )
    )
    candidate_ids = {
        candidate.candidate_id
        for candidate in candidates
        if candidate.payload.get("label_version_id") == label_version.label_version_id
    }
    conflicts = [
        conflict
        for conflict in session.scalars(
            select(LabelConflict).where(
                LabelConflict.tenant_id == ctx.tenant_id,
                LabelConflict.project_id == ctx.project_id,
                LabelConflict.status.in_(["detected", "reviewing"]),
            )
        )
        if conflict.payload.get("candidate_id") in candidate_ids
    ]
    reviews = [
        review
        for review in session.scalars(
            select(HumanReviewTask).where(
                HumanReviewTask.tenant_id == ctx.tenant_id,
                HumanReviewTask.project_id == ctx.project_id,
            )
        )
        if review.payload.get("candidate_id") in candidate_ids
    ]
    rollback_available = (
        session.scalar(
            select(LabelVersion.label_version_id).where(
                LabelVersion.tenant_id == ctx.tenant_id,
                LabelVersion.project_id == ctx.project_id,
                LabelVersion.status == "published",
                LabelVersion.label_version_id != label_version.label_version_id,
            )
        )
        is not None
    )
    actor_kind: Literal["service", "human"] = "service" if "system" in ctx.roles else "human"
    return LabelPolicyFacts(
        request=RequestFacts(
            action="publish_label_version",
            automation_level="agentic" if actor_kind == "service" else "assisted",
        ),
        actor=ActorFacts(kind=actor_kind),
        target=TargetFacts(
            status=label_version.status,
            risk_level=_risk_level(label_version.payload.get("risk_level")),
            resource_version=label_version.resource_version,
            same_scope=True,
        ),
        conflicts=ConflictFacts(
            open_count=len(conflicts),
            high_risk_open_count=sum(
                1
                for conflict in conflicts
                if conflict.payload.get("severity") in {"high", "critical"}
                or conflict.payload.get("verdict") == "block"
            ),
            human_disagreement_count=sum(
                1
                for conflict in conflicts
                if conflict.payload.get("reason_code") == "HUMAN_LABEL_DISAGREEMENT"
            ),
            equal_precedence_count=sum(
                1
                for conflict in conflicts
                if conflict.payload.get("reason_code") == "EQUAL_PRECEDENCE_CONFLICT"
            ),
        ),
        reviews=ReviewFacts(
            pending_count=sum(
                1 for review in reviews if review.status in PENDING_OR_BLOCKING_REVIEW_STATUSES
            ),
            rejected_count=sum(
                1 for review in reviews if review.status in REJECTING_REVIEW_STATUSES
            ),
            distinct_human_approver_count=len(
                {
                    str(review.payload.get("decided_by"))
                    for review in reviews
                    if review.payload.get("decided_by")
                }
            ),
        ),
        evaluation=EvaluationFacts(
            status="success",
            same_optimization_run=(
                snapshot.optimization_run_id == label_version.payload.get("optimization_run_id")
            ),
            dataset_locked=True,
            metric_schema_version=RELEASE_METRIC_SCHEMA_VERSION,
            eligible_count=snapshot.eligible_count,
            processed_count=snapshot.processed_count,
            skipped_count=snapshot.skipped_count,
            invalid_count=snapshot.invalid_count,
            abstain_count=snapshot.abstain_count,
            duplicate_count=snapshot.duplicate_count,
            effective_count=snapshot.effective_count,
            effective_coverage_ppm=snapshot.effective_coverage_ppm,
            counts_conserved=True,
            confusion_matrix=snapshot.confusion_matrix,
            sample_count=snapshot.processed_count,
            labeling_f1_ppm=_ratio_to_ppm(metric_data.get("labeling_f1")),
            conflict_rate_ppm=_ratio_to_ppm(metric_data.get("conflict_rate")),
            json_validity_ppm=_ratio_to_ppm(metric_data.get("json_validity")),
            blocking_regression_count=_nonnegative_int(
                metric_data.get("blocking_regression_count")
            ),
            blocking_badcase_count=_nonnegative_int(metric_data.get("blocking_badcase_count")),
        ),
        impact=ImpactFacts(
            assets_confirmed=(
                True if label_version.payload.get("impacted_assets_confirmed") is True else None
            ),
            downstream_incompatible_count=_nonnegative_int(
                label_version.payload.get("downstream_incompatible_count")
            ),
        ),
        release=ReleaseFacts(
            rollback_available=rollback_available,
            gray_traffic_ppm=request_body.gray_traffic_ppm,
        ),
        provenance=PolicyFactProvenance(
            target_type="label_version",
            target_id=label_version.label_version_id,
            target_resource_version=label_version.resource_version,
            label_version_id=label_version.label_version_id,
            conflict_ids=sorted(conflict.conflict_id for conflict in conflicts),
            review_task_ids=sorted(review.review_task_id for review in reviews),
            eval_run_id=eval_run.resource_key,
            optimization_run_id=snapshot.optimization_run_id,
            eval_dataset_id=dataset.resource_key,
            eval_dataset_version=snapshot.dataset_version,
        ),
    )


def _authoritative_candidate_facts(
    session: Session,
    ctx: RequestContext,
    candidate_payload: dict[str, Any],
    *,
    candidate_id: str,
    candidate_status: str,
    label_version_id: str,
    candidate_resource_version: int,
    label_resource_version: int,
) -> LabelPolicyFacts:
    prior_evaluation_id = candidate_payload.get("policy_evaluation_id")
    output_resource_version = _nonnegative_int(
        candidate_payload.get("policy_output_resource_version")
    )
    unchanged_since_policy_evaluation = (
        isinstance(prior_evaluation_id, str)
        and bool(prior_evaluation_id)
        and output_resource_version == candidate_resource_version
    )
    input_resource_version = _nonnegative_int(
        candidate_payload.get("policy_input_resource_version")
    )
    input_status = candidate_payload.get("policy_input_status")
    evaluated_target_resource_version = (
        input_resource_version
        if unchanged_since_policy_evaluation and input_resource_version is not None
        else candidate_resource_version
    )
    evaluated_target_status = (
        input_status
        if unchanged_since_policy_evaluation and isinstance(input_status, str) and input_status
        else candidate_status
    )
    evidence_ids: set[str] = set()
    evidence_pack_id = candidate_payload.get("evidence_pack_id")
    if isinstance(evidence_pack_id, str) and evidence_pack_id:
        evidence_ids.add(evidence_pack_id)
    for ref in candidate_payload.get("evidence_refs") or []:
        if isinstance(ref, dict):
            ref_id = ref.get("evidence_pack_id") or ref.get("id")
            if isinstance(ref_id, str) and ref_id:
                evidence_ids.add(ref_id)

    valid_statuses = {
        "accepted",
        "complete",
        "completed",
        "published",
        "ready",
        "success",
        "verified",
    }
    valid_count = 0
    pending_count = 0
    stale_count = 0
    invalid_window_count = 0
    missing_checksum_count = 0
    evidence_artifacts: list[EvidenceArtifactFacts] = []
    for ref_id in sorted(evidence_ids):
        resource = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == ctx.tenant_id,
                JsonResource.project_id == ctx.project_id,
                JsonResource.collection == "evidence_packs",
                JsonResource.resource_key == ref_id,
            )
        )
        if resource is None:
            pending_count += 1
            missing_checksum_count += 1
            invalid_window_count += 1
            evidence_artifacts.append(
                EvidenceArtifactFacts(evidence_pack_id=ref_id, status="missing")
            )
            continue
        data = resource.data
        status = str(resource.status or data.get("status") or "pending")
        checksum_value = (
            data.get("checksum_sha256")
            or data.get("artifact_checksum")
            or data.get("content_sha256")
        )
        checksum = (
            str(checksum_value).lower()
            if isinstance(checksum_value, str)
            and len(checksum_value) == 64
            and all(char in "0123456789abcdefABCDEF" for char in checksum_value)
            else None
        )
        start_ms = data.get("window_start_ms")
        end_ms = data.get("window_end_ms")
        window_valid = (
            isinstance(start_ms, int)
            and not isinstance(start_ms, bool)
            and isinstance(end_ms, int)
            and not isinstance(end_ms, bool)
            and start_ms >= 0
            and end_ms > start_ms
        )
        stale = bool(data.get("stale")) or status in {"expired", "stale"}
        if status not in valid_statuses:
            pending_count += 1
        if stale:
            stale_count += 1
        if not window_valid:
            invalid_window_count += 1
        if checksum is None:
            missing_checksum_count += 1
        if status in valid_statuses and not stale and window_valid and checksum is not None:
            valid_count += 1
        raw_resource_version = data.get("resource_version")
        resource_version = (
            raw_resource_version
            if isinstance(raw_resource_version, int)
            and not isinstance(raw_resource_version, bool)
            and raw_resource_version >= 1
            else resource.id
        )
        evidence_artifacts.append(
            EvidenceArtifactFacts(
                evidence_pack_id=ref_id,
                status=status,
                resource_version=resource_version,
                checksum_sha256=checksum,
                window_start_ms=_nonnegative_int(start_ms),
                window_end_ms=_nonnegative_int(end_ms),
                trace_id=resource.trace_id or None,
            )
        )

    human_state = str(candidate_payload.get("human_state") or "")
    source_type: CandidateSource | None = None
    if human_state in {"accepted", "modified", "confirmed"}:
        source_type = "human_confirmed"
    elif candidate_payload.get("prompt_version"):
        source_type = "llm_candidate"
    elif candidate_payload.get("source_type") in {
        "verified_business_document",
        "deterministic_rule",
        "model_candidate",
        "llm_candidate",
        "low_confidence_inference",
    }:
        source_type = cast(CandidateSource, candidate_payload["source_type"])
    else:
        source_type = "model_candidate"

    confidence_ppm = None
    raw_confidence = candidate_payload.get("confidence_ppm")
    if isinstance(raw_confidence, int) and not isinstance(raw_confidence, bool):
        confidence_ppm = raw_confidence if 0 <= raw_confidence <= 1_000_000 else None
    elif "confidence" in candidate_payload:
        try:
            legacy_confidence = Decimal(str(candidate_payload["confidence"]))
        except (InvalidOperation, ValueError):
            confidence_ppm = None
        else:
            confidence_ppm = (
                int(legacy_confidence * 1_000_000)
                if Decimal("0") <= legacy_confidence <= Decimal("1")
                else None
            )

    business_document_conflict = bool(candidate_payload.get("business_document_conflict")) or (
        "冲突" in str(candidate_payload.get("value_or_action") or "")
    )
    overwrites_human = bool(candidate_payload.get("overwrites_human")) or human_state in {
        "accepted",
        "modified",
        "confirmed",
    }
    competing_source_type: (
        Literal[
            "human_confirmed",
            "verified_business_document",
        ]
        | None
    ) = (
        "human_confirmed"
        if overwrites_human
        else "verified_business_document"
        if business_document_conflict
        else None
    )
    conflict_rows = list(
        session.scalars(
            select(LabelConflict).where(
                LabelConflict.tenant_id == ctx.tenant_id,
                LabelConflict.project_id == ctx.project_id,
                LabelConflict.status.in_(["detected", "reviewing"]),
            )
        )
    )
    conflicts = [
        row
        for row in conflict_rows
        if row.payload.get("candidate_id") == candidate_id
        and not (
            unchanged_since_policy_evaluation
            and row.payload.get("policy_evaluation_id") == prior_evaluation_id
            and row.status in {"detected", "reviewing"}
        )
    ]
    review_rows = list(
        session.scalars(
            select(HumanReviewTask).where(
                HumanReviewTask.tenant_id == ctx.tenant_id,
                HumanReviewTask.project_id == ctx.project_id,
            )
        )
    )
    reviews = [
        row
        for row in review_rows
        if row.payload.get("candidate_id") == candidate_id
        and not (
            unchanged_since_policy_evaluation
            and row.payload.get("policy_evaluation_id") == prior_evaluation_id
            and row.status in {"draft", "pending"}
        )
    ]
    actor_kind: Literal["service", "human"] = "service" if "system" in ctx.roles else "human"
    return LabelPolicyFacts(
        request=RequestFacts(
            action="evaluate_candidate",
            automation_level="agentic" if actor_kind == "service" else "assisted",
        ),
        actor=ActorFacts(kind=actor_kind),
        target=TargetFacts(
            status=evaluated_target_status,
            risk_level=_risk_level(candidate_payload.get("risk_level")),
            resource_version=evaluated_target_resource_version,
            same_scope=True,
        ),
        candidate=CandidateFacts(
            source_type=source_type,
            confidence_ppm=confidence_ppm,
            version_matches=(
                _nonnegative_int(candidate_payload.get("label_resource_version"))
                == label_resource_version
            ),
            overwrites_human=overwrites_human,
            business_document_conflict=business_document_conflict,
            competing_source_type=competing_source_type,
        ),
        evidence=EvidenceFacts(
            total_count=len(evidence_ids),
            valid_count=valid_count,
            pending_count=pending_count,
            cross_scope_count=0,
            stale_count=stale_count,
            invalid_window_count=invalid_window_count,
            missing_checksum_count=missing_checksum_count,
            artifacts=evidence_artifacts,
        ),
        conflicts=ConflictFacts(
            open_count=len(conflicts),
            high_risk_open_count=sum(
                1
                for row in conflicts
                if row.payload.get("severity") in {"high", "critical"}
                or row.payload.get("verdict") == "block"
            ),
            human_disagreement_count=sum(
                1
                for row in conflicts
                if row.payload.get("reason_code") == "HUMAN_LABEL_DISAGREEMENT"
            ),
            equal_precedence_count=sum(
                1
                for row in conflicts
                if row.payload.get("reason_code") == "EQUAL_PRECEDENCE_CONFLICT"
            ),
        ),
        reviews=ReviewFacts(
            pending_count=sum(
                1 for row in reviews if row.status in PENDING_OR_BLOCKING_REVIEW_STATUSES
            ),
            rejected_count=sum(1 for row in reviews if row.status in REJECTING_REVIEW_STATUSES),
            distinct_human_approver_count=len(
                {
                    str(row.payload.get("decided_by"))
                    for row in reviews
                    if row.payload.get("decided_by")
                }
            ),
        ),
        provenance=PolicyFactProvenance(
            target_type="label_candidate",
            target_id=candidate_id,
            target_resource_version=evaluated_target_resource_version,
            label_version_id=label_version_id,
            evidence_pack_ids=sorted(evidence_ids),
            conflict_ids=sorted(row.conflict_id for row in conflicts),
            review_task_ids=sorted(row.review_task_id for row in reviews),
        ),
    )


def _create_conflict(
    session: Session,
    ctx: RequestContext,
    *,
    evaluation: LabelPolicyEvaluation,
    decision: dict[str, Any],
    evidence_pack_id: object,
) -> str:
    conflict_id = _scoped_artifact_id(
        "lcf", ctx.tenant_id, ctx.project_id, evaluation.evaluation_id
    )
    if session.get(LabelConflict, conflict_id) is None:
        session.add(
            LabelConflict(
                conflict_id=conflict_id,
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                status="detected",
                trace_id=ctx.trace_id,
                payload={
                    "candidate_id": evaluation.candidate_id,
                    "policy_version_id": evaluation.policy_version_id,
                    "policy_evaluation_id": evaluation.evaluation_id,
                    "reason_code": decision["primary_reason_code"],
                    "verdict": decision["verdict"],
                    "evidence_pack_id": evidence_pack_id,
                    "decision_sha256": decision["decision_sha256"],
                    "trace_id": ctx.trace_id,
                },
            )
        )
    return conflict_id


def _create_review_task(
    session: Session,
    ctx: RequestContext,
    *,
    evaluation: LabelPolicyEvaluation,
    conflict_id: str,
    evidence_pack_id: object,
) -> str:
    review_task_id = _scoped_artifact_id(
        "hrt", ctx.tenant_id, ctx.project_id, evaluation.evaluation_id
    )
    existing = session.scalar(
        select(JsonResource).where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "human_review_tasks",
            JsonResource.resource_key == review_task_id,
        )
    )
    if existing is None:
        upsert_resource(
            session,
            ctx,
            "human_review_tasks",
            review_task_id,
            {
                "id": review_task_id,
                "review_task_id": review_task_id,
                "queue": "label_policy_conflict",
                "title": "标签策略冲突待复核",
                "priority": "high" if evaluation.verdict == "block" else "medium",
                "status": "pending",
                "policy_evaluation_id": evaluation.evaluation_id,
                "label_conflict_id": conflict_id,
                "candidate_id": evaluation.candidate_id,
                "evidence_pack_id": evidence_pack_id,
                "target_refs": [
                    {"type": "label_candidate", "id": evaluation.candidate_id},
                    {"type": "label_conflict", "id": conflict_id},
                ],
                "trace_id": ctx.trace_id,
            },
            status="pending",
            trace_id=ctx.trace_id,
            audit_action="label_policy.human_review_created",
        )
    return review_task_id


def _update_candidate_projection(
    session: Session,
    ctx: RequestContext,
    *,
    candidate_id: str,
    payload: dict[str, Any],
    status: str,
) -> None:
    projection = session.scalar(
        select(JsonResource).where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "label_candidates",
            JsonResource.resource_key == candidate_id,
        )
    )
    if projection is not None:
        projection.data = payload
        projection.status = status
        projection.trace_id = ctx.trace_id


def _update_label_version_projection(
    session: Session,
    ctx: RequestContext,
    *,
    label_version_id: str,
    payload: dict[str, Any],
) -> None:
    projection = session.scalar(
        select(JsonResource).where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "label_versions",
            JsonResource.resource_key == label_version_id,
        )
    )
    if projection is not None:
        projection.data = payload
        projection.status = str(payload.get("status") or projection.status or "draft")
        projection.trace_id = ctx.trace_id
