from __future__ import annotations

import hashlib
import secrets
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.project_membership import project_member_user_ids
from app.core.request_identifiers import server_generated_public_id
from app.domain.calibration import (
    RUBRIC_PROFILES,
    calculate_calibration_metrics,
    canonical_json_bytes,
    get_calibration_rubric,
)
from app.domain.calibration import canonical_value_sha256 as annotation_sha256
from app.models import (
    CalibrationAdjudication,
    CalibrationAssignment,
    CalibrationItem,
    CalibrationRound,
    CalibrationSubmission,
    GoldAnnotation,
    GoldSetSeries,
    GoldSetVersion,
    HumanReviewTask,
    JsonResource,
    Project,
    User,
)
from app.schemas.calibration import (
    CalibrationAdjudicationClaimRequest,
    CalibrationAdjudicationRequest,
    CalibrationAssignmentDTO,
    CalibrationConflictDTO,
    CalibrationConflictSubmissionDTO,
    CalibrationGoldReleaseRequest,
    CalibrationItemDTO,
    CalibrationRoundCreateRequest,
    CalibrationRoundDTO,
    CalibrationSubmissionRequest,
)
from app.services.audit_service import record_audit
from app.services.outbox_service import enqueue_event
from app.services.resource_service import upsert_resource

ROUND_STATUSES = frozenset({"in_review", "ready", "published"})
MANAGER_ROLES = frozenset({"project_admin", "system"})
REVIEWER_ELIGIBLE_ROLES = frozenset({"annotator", "review_arbitrator", "project_admin"})
ADJUDICATOR_ELIGIBLE_ROLES = frozenset({"review_arbitrator", "project_admin", "system"})
SUPPORTED_BINARY_RUBRICS = frozenset(RUBRIC_PROFILES)
MIN_GOLD_COVERAGE_PPM = 800_000


def _new_id(prefix: str) -> str:
    return server_generated_public_id(prefix, suffix_length=20)


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _manifest_sha256(samples: list[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_json_bytes(samples)).hexdigest()


def _round_role(round_record: CalibrationRound, user_id: str) -> str:
    if user_id == round_record.reviewer_a_id:
        return "reviewer_a"
    if user_id == round_record.reviewer_b_id:
        return "reviewer_b"
    if user_id == round_record.adjudicator_id:
        return "adjudicator"
    return "observer"


def _is_manager(ctx: RequestContext) -> bool:
    return bool(MANAGER_ROLES.intersection(ctx.roles))


def _assert_round_visible(round_record: CalibrationRound, ctx: RequestContext) -> None:
    if _is_manager(ctx) or _round_role(round_record, ctx.user_id) != "observer":
        return
    raise ApiError(
        "CALIBRATION_ROUND_FORBIDDEN",
        "当前用户不是该校准轮次的参与者",
        403,
    )


def _assert_adjudicator(round_record: CalibrationRound, ctx: RequestContext) -> None:
    if ctx.user_id in {round_record.reviewer_a_id, round_record.reviewer_b_id}:
        raise ApiError(
            "CALIBRATION_REVIEWER_ADJUDICATION_FORBIDDEN",
            "A/B reviewer 不能领取、裁决或发布自己的盲审校准轮次",
            403,
        )
    if ctx.user_id != round_record.adjudicator_id:
        raise ApiError(
            "CALIBRATION_ADJUDICATOR_REQUIRED",
            "仅该轮次指定的 adjudicator 可以执行此操作",
            403,
        )


def _assert_expected_version(
    *,
    expected: int | None,
    current: int,
    code: str,
    object_name: str,
) -> None:
    if expected is None or expected == current:
        return
    raise ApiError(
        code,
        f"{object_name}已被其他请求更新，请刷新后重试",
        409,
        details=[
            {
                "expected_resource_version": expected,
                "current_resource_version": current,
            }
        ],
    )


def _get_round(
    session: Session,
    ctx: RequestContext,
    round_id: str,
    *,
    for_update: bool = False,
) -> CalibrationRound:
    statement = select(CalibrationRound).where(
        CalibrationRound.round_id == round_id,
        CalibrationRound.tenant_id == ctx.tenant_id,
        CalibrationRound.project_id == ctx.project_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    round_record = session.scalar(statement)
    if round_record is None:
        raise ApiError(
            "CALIBRATION_ROUND_NOT_FOUND",
            f"校准轮次不存在：{round_id}",
            404,
        )
    return round_record


def _get_item_and_round_for_update(
    session: Session,
    ctx: RequestContext,
    item_id: str,
) -> tuple[CalibrationItem, CalibrationRound]:
    item = session.scalar(
        select(CalibrationItem)
        .where(
            CalibrationItem.item_id == item_id,
            CalibrationItem.tenant_id == ctx.tenant_id,
            CalibrationItem.project_id == ctx.project_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise ApiError(
            "CALIBRATION_ITEM_NOT_FOUND",
            f"校准样本不存在：{item_id}",
            404,
        )
    return item, _get_round(session, ctx, item.round_id, for_update=True)


def _validate_participants(
    session: Session,
    ctx: RequestContext,
    participant_ids: list[str],
) -> None:
    project = session.get(Project, ctx.project_id)
    if project is None or project.tenant_id != ctx.tenant_id:
        raise ApiError("PROJECT_NOT_FOUND", "当前项目不存在", 404)
    member_ids = set(project_member_user_ids(project))
    users = session.scalars(
        select(User).where(
            User.user_id.in_(participant_ids),
            User.tenant_id == ctx.tenant_id,
        )
    ).all()
    resolved_ids = {user.user_id for user in users}
    missing = sorted(set(participant_ids) - resolved_ids)
    non_members = sorted(resolved_ids - member_ids)
    if missing or non_members:
        raise ApiError(
            "CALIBRATION_PARTICIPANT_INVALID",
            "校准参与者必须是当前租户和项目的有效成员",
            422,
            details=[{"missing_user_ids": missing, "non_member_user_ids": non_members}],
        )
    users_by_id = {user.user_id: user for user in users}
    reviewer_ids = participant_ids[:2]
    ineligible_reviewers = sorted(
        reviewer_id
        for reviewer_id in reviewer_ids
        if not REVIEWER_ELIGIBLE_ROLES.intersection(users_by_id[reviewer_id].roles)
    )
    adjudicator_id = participant_ids[2]
    adjudicator_roles = users_by_id[adjudicator_id].roles
    if ineligible_reviewers or not ADJUDICATOR_ELIGIBLE_ROLES.intersection(adjudicator_roles):
        raise ApiError(
            "CALIBRATION_PARTICIPANT_ROLE_INVALID",
            "校准参与者角色不满足 reviewer / adjudicator 资格",
            422,
            details=[
                {
                    "ineligible_reviewer_ids": ineligible_reviewers,
                    "adjudicator_eligible": bool(
                        ADJUDICATOR_ELIGIBLE_ROLES.intersection(adjudicator_roles)
                    ),
                }
            ],
        )


def _item_data(item: CalibrationItem) -> dict[str, Any]:
    return CalibrationItemDTO(
        item_id=item.item_id,
        ordinal=item.ordinal,
        source_case_id=item.source_case_id,
        evidence_ref=item.evidence_ref,
        status=item.status,
        review_outcome=item.review_outcome,
        resource_version=item.resource_version,
        trace_id=item.trace_id,
    ).model_dump(mode="json")


def calibration_round_data(
    session: Session,
    ctx: RequestContext,
    round_record: CalibrationRound,
    *,
    include_items: bool = False,
) -> dict[str, Any]:
    _assert_round_visible(round_record, ctx)
    role = _round_role(round_record, ctx.user_id)
    sealed_for_reviewer = role in {"reviewer_a", "reviewer_b"}
    include_outcome_metrics = not sealed_for_reviewer
    items: list[dict[str, Any]] | None = None
    if include_items and not sealed_for_reviewer:
        item_records = session.scalars(
            select(CalibrationItem)
            .where(
                CalibrationItem.tenant_id == ctx.tenant_id,
                CalibrationItem.project_id == ctx.project_id,
                CalibrationItem.round_id == round_record.round_id,
            )
            .order_by(CalibrationItem.ordinal)
        ).all()
        items = [_item_data(item) for item in item_records]
    dto = CalibrationRoundDTO(
        id=round_record.round_id,
        round_id=round_record.round_id,
        dataset_id=round_record.dataset_id,
        dataset_version=round_record.dataset_version,
        label_version=round_record.label_version,
        rubric_version=round_record.rubric_version,
        sample_manifest_sha256=round_record.sample_manifest_sha256,
        status=round_record.status,
        sealed=sealed_for_reviewer,
        my_role=role,
        resource_version=round_record.resource_version,
        sample_count=round_record.sample_count,
        paired_submission_count=(
            round_record.paired_submission_count if include_outcome_metrics else None
        ),
        agreed_count=round_record.agreed_count if include_outcome_metrics else None,
        conflict_count=round_record.conflict_count if include_outcome_metrics else None,
        adjudication_count=(round_record.adjudication_count if include_outcome_metrics else None),
        excluded_count=round_record.excluded_count if include_outcome_metrics else None,
        observed_agreement_ppm=(
            round_record.observed_agreement_ppm if include_outcome_metrics else None
        ),
        cohen_kappa_micros=(round_record.cohen_kappa_micros if include_outcome_metrics else None),
        cohen_kappa_defined=(round_record.cohen_kappa_defined if include_outcome_metrics else None),
        root_trace_id=round_record.root_trace_id,
        current_trace_id=round_record.current_trace_id,
        items=cast(list[CalibrationItemDTO] | None, items),
        created_at=_isoformat(round_record.created_at),
        updated_at=_isoformat(round_record.updated_at),
        published_at=_isoformat(round_record.published_at),
    )
    return dto.model_dump(mode="json", exclude_none=True)


def create_calibration_round(
    session: Session,
    ctx: RequestContext,
    request_body: CalibrationRoundCreateRequest,
) -> dict[str, Any]:
    if request_body.rubric_version not in SUPPORTED_BINARY_RUBRICS:
        raise ApiError(
            "CALIBRATION_RUBRIC_UNSUPPORTED",
            "当前版本只允许已注册的二元校准 rubric",
            422,
            details=[{"rubric_version": request_body.rubric_version}],
        )
    if ctx.user_id in request_body.reviewer_ids:
        raise ApiError(
            "CALIBRATION_CREATOR_REVIEWER_FORBIDDEN",
            "严格盲审轮次的创建者不能同时作为 A/B reviewer",
            422,
        )
    # Treat reviewer_ids as an unordered reviewer set. Slot assignment happens
    # server-side so the creator cannot map anonymous A/B answers by request order.
    reviewer_a_id, reviewer_b_id = secrets.SystemRandom().sample(request_body.reviewer_ids, k=2)
    _validate_participants(
        session,
        ctx,
        [reviewer_a_id, reviewer_b_id, request_body.adjudicator_id],
    )
    manifest_entries = [
        {
            "ordinal": ordinal,
            "source_case_id": sample.source_case_id,
            "evidence_ref": sample.evidence_ref,
        }
        for ordinal, sample in enumerate(request_body.samples)
    ]
    manifest_sha256 = _manifest_sha256(manifest_entries)
    if (
        request_body.sample_manifest_sha256 is not None
        and request_body.sample_manifest_sha256 != manifest_sha256
    ):
        raise ApiError(
            "CALIBRATION_MANIFEST_MISMATCH",
            "sample_manifest_sha256 与规范化 samples 不一致",
            422,
            details=[
                {
                    "expected_sample_manifest_sha256": request_body.sample_manifest_sha256,
                    "actual_sample_manifest_sha256": manifest_sha256,
                }
            ],
        )

    round_id = _new_id("calr")
    round_record = CalibrationRound(
        round_id=round_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        dataset_id=request_body.dataset_id,
        dataset_version=request_body.dataset_version,
        label_version=request_body.label_version,
        rubric_version=request_body.rubric_version,
        sample_manifest_sha256=manifest_sha256,
        reviewer_a_id=reviewer_a_id,
        reviewer_b_id=reviewer_b_id,
        adjudicator_id=request_body.adjudicator_id,
        status="in_review",
        resource_version=1,
        sample_count=len(request_body.samples),
        paired_submission_count=0,
        agreed_count=0,
        conflict_count=0,
        adjudication_count=0,
        excluded_count=0,
        observed_agreement_ppm=0,
        cohen_kappa_micros=0,
        cohen_kappa_defined=False,
        root_trace_id=ctx.trace_id,
        current_trace_id=ctx.trace_id,
        published_at=None,
    )
    session.add(round_record)
    session.flush()

    item_specs: list[tuple[CalibrationItem, int]] = []
    for ordinal, sample in enumerate(request_body.samples):
        item_id = _new_id("cali")
        item = CalibrationItem(
            item_id=item_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            round_id=round_id,
            ordinal=ordinal,
            evidence_ref=sample.evidence_ref,
            source_case_id=sample.source_case_id,
            status="pending",
            review_outcome="pending",
            final_value_json=None,
            final_value_sha256=None,
            adjudication_claimed_by=None,
            adjudication_claimed_at=None,
            resource_version=1,
            trace_id=ctx.trace_id,
        )
        session.add(item)
        item_specs.append((item, ordinal))
    # Composite foreign keys are deliberately flushed in dependency order. This
    # also keeps SQLite's FK enforcement equivalent to MySQL during tests.
    session.flush()

    assignments: list[CalibrationAssignment] = []
    for item, ordinal in item_specs:
        for slot, reviewer_id in (("A", reviewer_a_id), ("B", reviewer_b_id)):
            assignment_id = _new_id("cala")
            review_task_id = _new_id("hrt_cal")
            task_payload = {
                "id": review_task_id,
                "review_task_id": review_task_id,
                "queue": "blind_calibration",
                "status": "pending",
                "assignment_id": assignment_id,
                "assignee_id": reviewer_id,
                "round_id": round_id,
                "item_id": item.item_id,
                "slot": slot,
                "ordinal": ordinal,
                "resource_version": 1,
                "root_trace_id": ctx.trace_id,
                "current_trace_id": ctx.trace_id,
                "trace_id": ctx.trace_id,
            }
            upsert_resource(
                session,
                ctx,
                "human_review_tasks",
                review_task_id,
                task_payload,
                status="pending",
                trace_id=ctx.trace_id,
            )
            assignments.append(
                CalibrationAssignment(
                    assignment_id=assignment_id,
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    round_id=round_id,
                    item_id=item.item_id,
                    slot=slot,
                    reviewer_id=reviewer_id,
                    review_task_id=review_task_id,
                    status="pending",
                    resource_version=1,
                    submitted_at=None,
                    trace_id=ctx.trace_id,
                )
            )
    session.flush()
    session.add_all(assignments)
    session.flush()
    created_data = calibration_round_data(session, ctx, round_record, include_items=True)
    record_audit(
        session,
        ctx,
        action="calibration.round.created",
        object_type="calibration_round",
        object_id=round_id,
        after={
            "round_id": round_id,
            "dataset_id": round_record.dataset_id,
            "dataset_version": round_record.dataset_version,
            "label_version": round_record.label_version,
            "rubric_version": round_record.rubric_version,
            "sample_manifest_sha256": manifest_sha256,
            "sample_count": round_record.sample_count,
            "status": round_record.status,
        },
    )
    enqueue_event(
        session,
        ctx,
        event_type="calibration.round.created",
        aggregate_type="calibration_round",
        aggregate_id=round_id,
        payload={
            "round_id": round_id,
            "dataset_id": round_record.dataset_id,
            "dataset_version": round_record.dataset_version,
            "label_version": round_record.label_version,
            "rubric_version": round_record.rubric_version,
            "sample_manifest_sha256": manifest_sha256,
            "sample_count": round_record.sample_count,
            "status": round_record.status,
        },
    )
    return created_data


def list_calibration_rounds(
    session: Session,
    ctx: RequestContext,
    *,
    status: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if status is not None and status not in ROUND_STATUSES:
        raise ApiError("CALIBRATION_ROUND_STATUS_INVALID", "校准轮次状态筛选值无效", 422)
    statement = select(CalibrationRound).where(
        CalibrationRound.tenant_id == ctx.tenant_id,
        CalibrationRound.project_id == ctx.project_id,
    )
    if status is not None:
        statement = statement.where(CalibrationRound.status == status)
    if not _is_manager(ctx):
        statement = statement.where(
            or_(
                CalibrationRound.reviewer_a_id == ctx.user_id,
                CalibrationRound.reviewer_b_id == ctx.user_id,
                CalibrationRound.adjudicator_id == ctx.user_id,
            )
        )
    statement = statement.order_by(
        CalibrationRound.created_at.desc(),
        CalibrationRound.round_id.desc(),
    ).limit(limit)
    rounds = session.scalars(statement).all()
    return [calibration_round_data(session, ctx, round_record) for round_record in rounds]


def get_calibration_round_detail(
    session: Session,
    ctx: RequestContext,
    round_id: str,
) -> dict[str, Any]:
    round_record = _get_round(session, ctx, round_id)
    return calibration_round_data(session, ctx, round_record, include_items=True)


def list_my_calibration_assignments(
    session: Session,
    ctx: RequestContext,
    *,
    round_id: str,
    mine: bool,
    limit: int,
) -> list[dict[str, Any]]:
    if not mine:
        raise ApiError(
            "CALIBRATION_ASSIGNMENTS_MINE_REQUIRED",
            "盲审 assignment 只能使用 mine=true 查询",
            422,
        )
    round_record = _get_round(session, ctx, round_id)
    if ctx.user_id not in {round_record.reviewer_a_id, round_record.reviewer_b_id}:
        raise ApiError(
            "CALIBRATION_REVIEWER_REQUIRED",
            "仅该轮次 A/B reviewer 可以查询自己的 assignment",
            403,
        )
    rows = session.execute(
        select(CalibrationAssignment, CalibrationItem)
        .join(
            CalibrationItem,
            and_(
                CalibrationItem.tenant_id == CalibrationAssignment.tenant_id,
                CalibrationItem.project_id == CalibrationAssignment.project_id,
                CalibrationItem.round_id == CalibrationAssignment.round_id,
                CalibrationItem.item_id == CalibrationAssignment.item_id,
            ),
        )
        .where(
            CalibrationAssignment.tenant_id == ctx.tenant_id,
            CalibrationAssignment.project_id == ctx.project_id,
            CalibrationAssignment.round_id == round_id,
            CalibrationAssignment.reviewer_id == ctx.user_id,
        )
        .order_by(CalibrationItem.ordinal)
        .limit(limit)
    ).all()
    return [
        CalibrationAssignmentDTO(
            assignment_id=assignment.assignment_id,
            round_id=assignment.round_id,
            item_id=assignment.item_id,
            review_task_id=assignment.review_task_id,
            slot=cast(Literal["A", "B"], assignment.slot),
            ordinal=item.ordinal,
            source_case_id=item.source_case_id,
            evidence_ref=item.evidence_ref,
            status=assignment.status,
            resource_version=assignment.resource_version,
            trace_id=assignment.trace_id,
        ).model_dump(mode="json")
        for assignment, item in rows
    ]


def _sync_assignment_review_task(
    session: Session,
    ctx: RequestContext,
    assignment: CalibrationAssignment,
) -> None:
    task = session.scalar(
        select(HumanReviewTask)
        .where(
            HumanReviewTask.review_task_id == assignment.review_task_id,
            HumanReviewTask.tenant_id == ctx.tenant_id,
            HumanReviewTask.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    resource = session.scalar(
        select(JsonResource)
        .where(
            JsonResource.collection == "human_review_tasks",
            JsonResource.resource_key == assignment.review_task_id,
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if task is None or resource is None:
        raise ApiError(
            "CALIBRATION_REVIEW_TASK_MISSING",
            "盲审 assignment 的 HumanReviewTask 投影缺失",
            409,
        )
    task.status = assignment.status
    task.trace_id = ctx.trace_id
    task.payload = {
        **task.payload,
        "status": assignment.status,
        "resource_version": assignment.resource_version,
        "current_trace_id": ctx.trace_id,
        "trace_id": ctx.trace_id,
    }
    resource.status = assignment.status
    resource.trace_id = ctx.trace_id
    resource.data = {
        **resource.data,
        "status": assignment.status,
        "resource_version": assignment.resource_version,
        "current_trace_id": ctx.trace_id,
        "trace_id": ctx.trace_id,
    }


def _item_submissions(
    session: Session,
    ctx: RequestContext,
    item: CalibrationItem,
) -> dict[str, CalibrationSubmission]:
    rows = session.execute(
        select(CalibrationAssignment.slot, CalibrationSubmission)
        .join(
            CalibrationSubmission,
            and_(
                CalibrationSubmission.tenant_id == CalibrationAssignment.tenant_id,
                CalibrationSubmission.project_id == CalibrationAssignment.project_id,
                CalibrationSubmission.assignment_id == CalibrationAssignment.assignment_id,
                CalibrationSubmission.round_id == CalibrationAssignment.round_id,
                CalibrationSubmission.item_id == CalibrationAssignment.item_id,
            ),
        )
        .where(
            CalibrationAssignment.tenant_id == ctx.tenant_id,
            CalibrationAssignment.project_id == ctx.project_id,
            CalibrationAssignment.round_id == item.round_id,
            CalibrationAssignment.item_id == item.item_id,
        )
    ).all()
    return {slot: submission for slot, submission in rows}


def _refresh_round_metrics(
    session: Session,
    ctx: RequestContext,
    round_record: CalibrationRound,
) -> None:
    items = session.scalars(
        select(CalibrationItem)
        .where(
            CalibrationItem.tenant_id == ctx.tenant_id,
            CalibrationItem.project_id == ctx.project_id,
            CalibrationItem.round_id == round_record.round_id,
        )
        .order_by(CalibrationItem.ordinal)
    ).all()
    rows = session.execute(
        select(CalibrationAssignment.item_id, CalibrationAssignment.slot, CalibrationSubmission)
        .join(
            CalibrationSubmission,
            and_(
                CalibrationSubmission.tenant_id == CalibrationAssignment.tenant_id,
                CalibrationSubmission.project_id == CalibrationAssignment.project_id,
                CalibrationSubmission.assignment_id == CalibrationAssignment.assignment_id,
                CalibrationSubmission.round_id == CalibrationAssignment.round_id,
                CalibrationSubmission.item_id == CalibrationAssignment.item_id,
            ),
        )
        .where(
            CalibrationAssignment.tenant_id == ctx.tenant_id,
            CalibrationAssignment.project_id == ctx.project_id,
            CalibrationAssignment.round_id == round_record.round_id,
        )
    ).all()
    by_item: dict[str, dict[str, CalibrationSubmission]] = {}
    for item_id, slot, submission in rows:
        by_item.setdefault(item_id, {})[slot] = submission
    rubric = get_calibration_rubric(round_record.rubric_version)
    reviewer_a: list[str] = []
    reviewer_b: list[str] = []
    for item in items:
        submissions = by_item.get(item.item_id, {})
        if "A" in submissions and "B" in submissions:
            reviewer_a.append(rubric.category_key(submissions["A"].value_json))
            reviewer_b.append(rubric.category_key(submissions["B"].value_json))
    adjudication_count = sum(item.status in {"adjudicated", "excluded"} for item in items)
    excluded_count = sum(item.status == "excluded" for item in items)
    metrics = calculate_calibration_metrics(
        reviewer_a,
        reviewer_b,
        adjudication_count=adjudication_count,
    )
    round_record.paired_submission_count = metrics.paired_submission_count
    round_record.agreed_count = metrics.agreed_count
    round_record.conflict_count = metrics.conflict_count
    round_record.adjudication_count = metrics.adjudication_count
    round_record.excluded_count = excluded_count
    round_record.observed_agreement_ppm = metrics.observed_agreement_ppm
    round_record.cohen_kappa_micros = metrics.cohen_kappa_micros
    round_record.cohen_kappa_defined = metrics.cohen_kappa_defined
    if round_record.status != "published":
        fully_resolved = (
            metrics.paired_submission_count == round_record.sample_count
            and metrics.adjudication_count == metrics.conflict_count
        )
        round_record.status = "ready" if fully_resolved else "in_review"


def submit_calibration_assignment(
    session: Session,
    ctx: RequestContext,
    assignment_id: str,
    request_body: CalibrationSubmissionRequest,
) -> dict[str, Any]:
    assignment = session.scalar(
        select(CalibrationAssignment)
        .where(
            CalibrationAssignment.assignment_id == assignment_id,
            CalibrationAssignment.tenant_id == ctx.tenant_id,
            CalibrationAssignment.project_id == ctx.project_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if assignment is None:
        raise ApiError(
            "CALIBRATION_ASSIGNMENT_NOT_FOUND",
            f"校准 assignment 不存在：{assignment_id}",
            404,
        )
    if assignment.reviewer_id != ctx.user_id:
        raise ApiError(
            "CALIBRATION_ASSIGNMENT_FORBIDDEN",
            "仅该 assignment 指定的 reviewer 可以提交",
            403,
        )
    _assert_expected_version(
        expected=request_body.expected_resource_version,
        current=assignment.resource_version,
        code="CALIBRATION_ASSIGNMENT_VERSION_CONFLICT",
        object_name="校准 assignment",
    )
    if assignment.status != "pending":
        raise ApiError(
            "CALIBRATION_ASSIGNMENT_ALREADY_SUBMITTED",
            "该 reviewer 的提交已密封，不能重复提交或覆盖",
            409,
        )
    round_record = _get_round(session, ctx, assignment.round_id, for_update=True)
    if round_record.status != "in_review":
        raise ApiError(
            "CALIBRATION_ROUND_NOT_ACCEPTING_SUBMISSIONS",
            "该校准轮次已停止接收 reviewer 提交",
            409,
        )
    item = session.scalar(
        select(CalibrationItem)
        .where(
            CalibrationItem.item_id == assignment.item_id,
            CalibrationItem.tenant_id == ctx.tenant_id,
            CalibrationItem.project_id == ctx.project_id,
            CalibrationItem.round_id == assignment.round_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise ApiError("CALIBRATION_ITEM_NOT_FOUND", "assignment 绑定的样本不存在", 409)
    submission_value = request_body.value.model_dump(mode="json")
    rubric = get_calibration_rubric(round_record.rubric_version)
    try:
        rubric.validate_submission(submission_value, evidence_ref=item.evidence_ref)
    except ValueError as exc:
        raise ApiError(
            "CALIBRATION_RUBRIC_VALUE_INVALID",
            str(exc),
            422,
        ) from exc
    try:
        value_sha256 = annotation_sha256(submission_value)
    except ValueError as exc:
        raise ApiError("CALIBRATION_VALUE_INVALID", str(exc), 422) from exc

    submitted_at = datetime.now(UTC)
    submission_id = _new_id("cals")
    submission = CalibrationSubmission(
        submission_id=submission_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        round_id=assignment.round_id,
        item_id=assignment.item_id,
        assignment_id=assignment.assignment_id,
        reviewer_id=ctx.user_id,
        resource_version=1,
        value_json=deepcopy(submission_value),
        canonical_value_sha256=value_sha256,
        trace_id=ctx.trace_id,
        submitted_at=submitted_at,
    )
    session.add(submission)
    assignment.status = "submitted"
    assignment.submitted_at = submitted_at
    assignment.resource_version += 1
    assignment.trace_id = ctx.trace_id
    _sync_assignment_review_task(session, ctx, assignment)
    session.flush()

    submissions = _item_submissions(session, ctx, item)
    if set(submissions) == {"A", "B"}:
        submission_a = submissions["A"]
        submission_b = submissions["B"]
        if rubric.category_key(submission_a.value_json) == rubric.category_key(
            submission_b.value_json
        ):
            resolved_value = rubric.gold_value(submission_a.value_json)
            item.status = "agreed"
            item.review_outcome = "agreed"
            item.final_value_json = deepcopy(resolved_value)
            item.final_value_sha256 = annotation_sha256(resolved_value)
        else:
            item.status = "conflicted"
            item.review_outcome = "conflicted"
            item.final_value_json = None
            item.final_value_sha256 = None
        item.resource_version += 1
        item.trace_id = ctx.trace_id
    round_record.resource_version += 1
    round_record.current_trace_id = ctx.trace_id
    _refresh_round_metrics(session, ctx, round_record)
    result = {
        "submission_id": submission_id,
        "assignment_id": assignment.assignment_id,
        "round_id": assignment.round_id,
        "item_id": assignment.item_id,
        "slot": assignment.slot,
        "status": assignment.status,
        "resource_version": assignment.resource_version,
        "trace_id": ctx.trace_id,
        "submitted_at": submitted_at.isoformat(),
    }
    internal_result = {
        **result,
        "item_status": item.status,
        "round_status": round_record.status,
    }
    record_audit(
        session,
        ctx,
        action="calibration.submission.sealed",
        object_type="calibration_assignment",
        object_id=assignment.assignment_id,
        after=internal_result,
    )
    enqueue_event(
        session,
        ctx,
        event_type="calibration.submission.sealed",
        aggregate_type="calibration_assignment",
        aggregate_id=assignment.assignment_id,
        payload=internal_result,
    )
    return result


def list_calibration_conflicts(
    session: Session,
    ctx: RequestContext,
    round_id: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    round_record = _get_round(session, ctx, round_id)
    _assert_adjudicator(round_record, ctx)
    items = session.scalars(
        select(CalibrationItem)
        .where(
            CalibrationItem.tenant_id == ctx.tenant_id,
            CalibrationItem.project_id == ctx.project_id,
            CalibrationItem.round_id == round_id,
            CalibrationItem.review_outcome == "conflicted",
        )
        .order_by(CalibrationItem.ordinal)
        .limit(limit)
    ).all()
    result: list[dict[str, Any]] = []
    for item in items:
        submissions = _item_submissions(session, ctx, item)
        submission_dtos = [
            CalibrationConflictSubmissionDTO(
                submission_id=submission.submission_id,
                slot=cast(Literal["A", "B"], slot),
                value=submission.value_json,
                submitted_at=_isoformat(submission.submitted_at) or "",
            )
            for slot, submission in sorted(submissions.items())
        ]
        result.append(
            CalibrationConflictDTO(
                item_id=item.item_id,
                round_id=item.round_id,
                ordinal=item.ordinal,
                source_case_id=item.source_case_id,
                evidence_ref=item.evidence_ref,
                status=item.status,
                review_outcome=item.review_outcome,
                resource_version=item.resource_version,
                adjudication_claimed=item.adjudication_claimed_by is not None,
                submissions=submission_dtos,
            ).model_dump(mode="json")
        )
    return result


def claim_calibration_item(
    session: Session,
    ctx: RequestContext,
    item_id: str,
    request_body: CalibrationAdjudicationClaimRequest,
) -> dict[str, Any]:
    item, round_record = _get_item_and_round_for_update(session, ctx, item_id)
    _assert_adjudicator(round_record, ctx)
    if item.status != "conflicted":
        raise ApiError(
            "CALIBRATION_ITEM_NOT_CLAIMABLE",
            "仅 unresolved conflicted 样本可以领取裁决",
            409,
        )
    if item.adjudication_claimed_by == ctx.user_id:
        return {
            "item_id": item.item_id,
            "round_id": item.round_id,
            "status": item.status,
            "adjudication_claimed": True,
            "resource_version": item.resource_version,
            "trace_id": item.trace_id,
            "claimed_at": _isoformat(item.adjudication_claimed_at),
            "replayed": True,
        }
    if item.adjudication_claimed_by is not None:
        raise ApiError(
            "CALIBRATION_ADJUDICATION_ALREADY_CLAIMED",
            "该争议样本已被领取",
            409,
        )
    _assert_expected_version(
        expected=request_body.expected_resource_version,
        current=item.resource_version,
        code="CALIBRATION_ITEM_VERSION_CONFLICT",
        object_name="校准样本",
    )
    claimed_at = datetime.now(UTC)
    item.adjudication_claimed_by = ctx.user_id
    item.adjudication_claimed_at = claimed_at
    item.resource_version += 1
    item.trace_id = ctx.trace_id
    round_record.resource_version += 1
    round_record.current_trace_id = ctx.trace_id
    result = {
        "item_id": item.item_id,
        "round_id": item.round_id,
        "status": item.status,
        "adjudication_claimed": True,
        "resource_version": item.resource_version,
        "trace_id": ctx.trace_id,
        "claimed_at": claimed_at.isoformat(),
    }
    record_audit(
        session,
        ctx,
        action="calibration.adjudication.claimed",
        object_type="calibration_item",
        object_id=item.item_id,
        after=result,
    )
    enqueue_event(
        session,
        ctx,
        event_type="calibration.adjudication.claimed",
        aggregate_type="calibration_item",
        aggregate_id=item.item_id,
        payload=result,
    )
    return result


def adjudicate_calibration_item(
    session: Session,
    ctx: RequestContext,
    item_id: str,
    request_body: CalibrationAdjudicationRequest,
) -> dict[str, Any]:
    item, round_record = _get_item_and_round_for_update(session, ctx, item_id)
    _assert_adjudicator(round_record, ctx)
    _assert_expected_version(
        expected=request_body.expected_resource_version,
        current=item.resource_version,
        code="CALIBRATION_ITEM_VERSION_CONFLICT",
        object_name="校准样本",
    )
    if item.status != "conflicted":
        raise ApiError(
            "CALIBRATION_ITEM_ALREADY_RESOLVED",
            "该争议样本已被其他请求裁决",
            409,
        )
    if item.adjudication_claimed_by != ctx.user_id:
        raise ApiError(
            "CALIBRATION_ADJUDICATION_CLAIM_REQUIRED",
            "adjudicator 必须先成功领取该争议样本",
            409,
        )
    existing = session.scalar(
        select(CalibrationAdjudication)
        .where(
            CalibrationAdjudication.item_id == item.item_id,
            CalibrationAdjudication.tenant_id == ctx.tenant_id,
            CalibrationAdjudication.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if existing is not None:
        raise ApiError(
            "CALIBRATION_ITEM_ALREADY_RESOLVED",
            "该争议样本已存在不可变裁决",
            409,
        )
    submissions = _item_submissions(session, ctx, item)
    if set(submissions) != {"A", "B"}:
        raise ApiError(
            "CALIBRATION_SUBMISSION_PAIR_MISSING",
            "争议样本缺少完整 A/B 密封提交",
            409,
        )

    accepted_submission_id: str | None = None
    resolved_value: Any = None
    resolved_sha256: str | None = None
    rubric = get_calibration_rubric(round_record.rubric_version)
    if request_body.decision in {"accept_a", "accept_b"}:
        slot = "A" if request_body.decision == "accept_a" else "B"
        accepted = submissions[slot]
        accepted_submission_id = accepted.submission_id
        resolved_value = rubric.gold_value(accepted.value_json)
        resolved_sha256 = annotation_sha256(resolved_value)
    elif request_body.decision == "revise":
        assert request_body.value is not None
        revised_value = request_body.value.model_dump(mode="json")
        try:
            rubric.validate_submission(revised_value, evidence_ref=item.evidence_ref)
            resolved_value = rubric.gold_value(revised_value)
            resolved_sha256 = annotation_sha256(resolved_value)
        except ValueError as exc:
            raise ApiError("CALIBRATION_VALUE_INVALID", str(exc), 422) from exc

    adjudication_id = _new_id("caladj")
    session.add(
        CalibrationAdjudication(
            adjudication_id=adjudication_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            round_id=item.round_id,
            item_id=item.item_id,
            adjudicator_id=ctx.user_id,
            decision=request_body.decision,
            reason=request_body.reason,
            resource_version=1,
            accepted_submission_id=accepted_submission_id,
            value_json=(
                None if request_body.decision == "exclude" else cast(dict[str, Any], resolved_value)
            ),
            canonical_value_sha256=resolved_sha256,
            trace_id=ctx.trace_id,
            created_at=datetime.now(UTC),
        )
    )
    if request_body.decision == "exclude":
        item.status = "excluded"
        item.final_value_json = None
        item.final_value_sha256 = None
    else:
        item.status = "adjudicated"
        item.final_value_json = cast(dict[str, Any], resolved_value)
        item.final_value_sha256 = resolved_sha256
    item.adjudication_claimed_by = None
    item.adjudication_claimed_at = None
    item.resource_version += 1
    item.trace_id = ctx.trace_id
    round_record.resource_version += 1
    round_record.current_trace_id = ctx.trace_id
    session.flush()
    _refresh_round_metrics(session, ctx, round_record)
    result = {
        "adjudication_id": adjudication_id,
        "item_id": item.item_id,
        "round_id": item.round_id,
        "decision": request_body.decision,
        "status": item.status,
        "resource_version": item.resource_version,
        "round_status": round_record.status,
        "trace_id": ctx.trace_id,
    }
    record_audit(
        session,
        ctx,
        action="calibration.adjudication.completed",
        object_type="calibration_item",
        object_id=item.item_id,
        after=result,
    )
    enqueue_event(
        session,
        ctx,
        event_type="calibration.adjudication.completed",
        aggregate_type="calibration_item",
        aggregate_id=item.item_id,
        payload=result,
    )
    return result


def _allocate_gold_version(
    session: Session,
    ctx: RequestContext,
    *,
    gold_set_key: str,
) -> int:
    series = session.scalar(
        select(GoldSetSeries)
        .where(
            GoldSetSeries.tenant_id == ctx.tenant_id,
            GoldSetSeries.project_id == ctx.project_id,
            GoldSetSeries.gold_set_key == gold_set_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if series is None:
        series = GoldSetSeries(
            gold_set_series_id=_new_id("gseries"),
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            gold_set_key=gold_set_key,
            next_version=2,
            resource_version=1,
            trace_id=ctx.trace_id,
        )
        session.add(series)
        session.flush()
        return 1
    version_number = series.next_version
    series.next_version += 1
    series.resource_version += 1
    series.trace_id = ctx.trace_id
    session.flush()
    return version_number


def release_calibration_gold(
    session: Session,
    ctx: RequestContext,
    round_id: str,
    request_body: CalibrationGoldReleaseRequest,
) -> dict[str, Any]:
    round_record = _get_round(session, ctx, round_id, for_update=True)
    _assert_adjudicator(round_record, ctx)
    _assert_expected_version(
        expected=request_body.expected_resource_version,
        current=round_record.resource_version,
        code="CALIBRATION_ROUND_VERSION_CONFLICT",
        object_name="校准轮次",
    )
    if round_record.status != "ready":
        raise ApiError(
            "CALIBRATION_ROUND_NOT_READY",
            "仅全部样本完成配对且所有冲突已裁决的轮次可以发布金标",
            409,
        )
    existing = session.scalar(
        select(GoldSetVersion)
        .where(
            GoldSetVersion.tenant_id == ctx.tenant_id,
            GoldSetVersion.project_id == ctx.project_id,
            GoldSetVersion.round_id == round_id,
        )
        .with_for_update()
    )
    if existing is not None:
        raise ApiError(
            "CALIBRATION_GOLD_ALREADY_RELEASED",
            "该校准轮次已经发布过不可变金标版本",
            409,
        )
    items = session.scalars(
        select(CalibrationItem)
        .where(
            CalibrationItem.tenant_id == ctx.tenant_id,
            CalibrationItem.project_id == ctx.project_id,
            CalibrationItem.round_id == round_id,
        )
        .order_by(CalibrationItem.ordinal)
        .with_for_update()
    ).all()
    if len(items) != round_record.sample_count or any(
        item.status not in {"agreed", "adjudicated", "excluded"} for item in items
    ):
        raise ApiError(
            "CALIBRATION_ROUND_RESOLUTION_INCONSISTENT",
            "校准轮次状态与样本 resolution 不一致",
            409,
        )

    annotation_count = round_record.sample_count - round_record.excluded_count
    coverage_ppm = (annotation_count * 1_000_000) // round_record.sample_count
    if annotation_count <= 0 or coverage_ppm < MIN_GOLD_COVERAGE_PPM:
        raise ApiError(
            "CALIBRATION_GOLD_COVERAGE_BLOCKED",
            "有效金标覆盖率未达到发布门禁",
            409,
            details=[
                {
                    "annotation_count": annotation_count,
                    "sample_count": round_record.sample_count,
                    "coverage_ppm": coverage_ppm,
                    "minimum_coverage_ppm": MIN_GOLD_COVERAGE_PPM,
                }
            ],
        )
    version_number = _allocate_gold_version(
        session,
        ctx,
        gold_set_key=request_body.gold_set_key,
    )

    annotation_manifest = [
        {
            "ordinal": item.ordinal,
            "item_id": item.item_id,
            "source_case_id": item.source_case_id,
            "status": item.status,
            "canonical_value_sha256": item.final_value_sha256,
        }
        for item in items
    ]
    annotation_manifest_sha256 = _manifest_sha256(annotation_manifest)
    gold_set_version_id = _new_id("goldv")
    published_at = datetime.now(UTC)
    gold_version = GoldSetVersion(
        gold_set_version_id=gold_set_version_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        round_id=round_id,
        gold_set_key=request_body.gold_set_key,
        version_number=version_number,
        dataset_id=round_record.dataset_id,
        dataset_version=round_record.dataset_version,
        label_version=round_record.label_version,
        rubric_version=round_record.rubric_version,
        sample_manifest_sha256=round_record.sample_manifest_sha256,
        annotation_manifest_sha256=annotation_manifest_sha256,
        status="published",
        resource_version=1,
        sample_count=round_record.sample_count,
        annotation_count=annotation_count,
        excluded_count=round_record.excluded_count,
        observed_agreement_ppm=round_record.observed_agreement_ppm,
        cohen_kappa_micros=round_record.cohen_kappa_micros,
        cohen_kappa_defined=round_record.cohen_kappa_defined,
        conflict_count=round_record.conflict_count,
        adjudication_count=round_record.adjudication_count,
        published_by=ctx.user_id,
        trace_id=ctx.trace_id,
        published_at=published_at,
    )
    session.add(gold_version)
    # GoldAnnotation has a composite FK to the version but no ORM relationship.
    # Flush the parent explicitly so SQLAlchemy cannot batch child inserts first.
    session.flush()
    annotation_ids: list[str] = []
    for item in items:
        if item.status == "excluded":
            continue
        if item.final_value_json is None or item.final_value_sha256 is None:
            raise ApiError(
                "CALIBRATION_GOLD_VALUE_MISSING",
                "非排除样本缺少最终金标值",
                409,
            )
        annotation_id = _new_id("golda")
        annotation_ids.append(annotation_id)
        session.add(
            GoldAnnotation(
                gold_annotation_id=annotation_id,
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                gold_set_version_id=gold_set_version_id,
                round_id=round_id,
                item_id=item.item_id,
                source_case_id=item.source_case_id,
                evidence_ref=item.evidence_ref,
                value_json=deepcopy(item.final_value_json),
                canonical_value_sha256=item.final_value_sha256,
                resolution_source=("agreed" if item.status == "agreed" else "adjudicated"),
                resource_version=1,
                trace_id=ctx.trace_id,
                created_at=published_at,
            )
        )
    session.flush()
    round_record.status = "published"
    round_record.published_at = published_at
    round_record.resource_version += 1
    round_record.current_trace_id = ctx.trace_id
    result = {
        "gold_set_version_id": gold_set_version_id,
        "gold_set_key": request_body.gold_set_key,
        "version_number": version_number,
        "round_id": round_id,
        "status": "published",
        "resource_version": 1,
        "dataset_id": round_record.dataset_id,
        "dataset_version": round_record.dataset_version,
        "label_version": round_record.label_version,
        "rubric_version": round_record.rubric_version,
        "sample_manifest_sha256": round_record.sample_manifest_sha256,
        "annotation_manifest_sha256": annotation_manifest_sha256,
        "sample_count": round_record.sample_count,
        "annotation_count": len(annotation_ids),
        "excluded_count": round_record.excluded_count,
        "observed_agreement_ppm": round_record.observed_agreement_ppm,
        "cohen_kappa_micros": round_record.cohen_kappa_micros,
        "cohen_kappa_defined": round_record.cohen_kappa_defined,
        "coverage_ppm": coverage_ppm,
        "conflict_count": round_record.conflict_count,
        "adjudication_count": round_record.adjudication_count,
        "published_by": ctx.user_id,
        "trace_id": ctx.trace_id,
        "published_at": published_at.isoformat(),
    }
    record_audit(
        session,
        ctx,
        action="calibration.gold_set.published",
        object_type="gold_set_version",
        object_id=gold_set_version_id,
        after=result,
    )
    enqueue_event(
        session,
        ctx,
        event_type="calibration.gold_set.published",
        aggregate_type="gold_set_version",
        aggregate_id=gold_set_version_id,
        payload={**result, "annotation_ids": annotation_ids},
    )
    return result


def count_gold_versions(
    session: Session,
    ctx: RequestContext,
    *,
    gold_set_key: str,
) -> int:
    return int(
        session.scalar(
            select(func.count(GoldSetVersion.gold_set_version_id)).where(
                GoldSetVersion.tenant_id == ctx.tenant_id,
                GoldSetVersion.project_id == ctx.project_id,
                GoldSetVersion.gold_set_key == gold_set_key,
            )
        )
        or 0
    )


def _gold_version_data(
    session: Session,
    ctx: RequestContext,
    version: GoldSetVersion,
    *,
    include_annotations: bool,
) -> dict[str, Any]:
    if version.tenant_id != ctx.tenant_id or version.project_id != ctx.project_id:
        raise ApiError("GOLD_SET_VERSION_NOT_FOUND", "金标版本不存在", 404)
    source_round = session.scalar(
        select(CalibrationRound).where(
            CalibrationRound.round_id == version.round_id,
            CalibrationRound.tenant_id == ctx.tenant_id,
            CalibrationRound.project_id == ctx.project_id,
        )
    )
    if source_round is not None and ctx.user_id in {
        source_round.reviewer_a_id,
        source_round.reviewer_b_id,
    }:
        raise ApiError(
            "CALIBRATION_REVIEWER_GOLD_FORBIDDEN",
            "A/B reviewer 不能读取其参与轮次发布的金标",
            403,
        )
    annotations: list[dict[str, Any]] | None = None
    if include_annotations:
        rows = session.scalars(
            select(GoldAnnotation)
            .where(
                GoldAnnotation.tenant_id == ctx.tenant_id,
                GoldAnnotation.project_id == ctx.project_id,
                GoldAnnotation.gold_set_version_id == version.gold_set_version_id,
            )
            .order_by(GoldAnnotation.source_case_id, GoldAnnotation.gold_annotation_id)
        ).all()
        annotations = [
            {
                "gold_annotation_id": row.gold_annotation_id,
                "item_id": row.item_id,
                "source_case_id": row.source_case_id,
                "evidence_ref": row.evidence_ref,
                "value": deepcopy(row.value_json),
                "canonical_value_sha256": row.canonical_value_sha256,
                "resolution_source": row.resolution_source,
                "trace_id": row.trace_id,
                "created_at": _isoformat(row.created_at),
            }
            for row in rows
        ]
    return {
        "id": version.gold_set_version_id,
        "gold_set_version_id": version.gold_set_version_id,
        "gold_set_key": version.gold_set_key,
        "version_number": version.version_number,
        "round_id": version.round_id,
        "dataset_id": version.dataset_id,
        "dataset_version": version.dataset_version,
        "label_version": version.label_version,
        "rubric_version": version.rubric_version,
        "sample_manifest_sha256": version.sample_manifest_sha256,
        "annotation_manifest_sha256": version.annotation_manifest_sha256,
        "status": version.status,
        "resource_version": version.resource_version,
        "sample_count": version.sample_count,
        "annotation_count": version.annotation_count,
        "excluded_count": version.excluded_count,
        "coverage_ppm": (version.annotation_count * 1_000_000) // version.sample_count,
        "observed_agreement_ppm": version.observed_agreement_ppm,
        "cohen_kappa_micros": version.cohen_kappa_micros,
        "cohen_kappa_defined": version.cohen_kappa_defined,
        "conflict_count": version.conflict_count,
        "adjudication_count": version.adjudication_count,
        "published_by": version.published_by,
        "trace_id": version.trace_id,
        "published_at": _isoformat(version.published_at),
        **({"annotations": annotations} if annotations is not None else {}),
    }


def list_gold_set_versions(
    session: Session,
    ctx: RequestContext,
    *,
    gold_set_key: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    statement = select(GoldSetVersion).where(
        GoldSetVersion.tenant_id == ctx.tenant_id,
        GoldSetVersion.project_id == ctx.project_id,
        GoldSetVersion.round_id.not_in(
            select(CalibrationRound.round_id).where(
                CalibrationRound.tenant_id == ctx.tenant_id,
                CalibrationRound.project_id == ctx.project_id,
                or_(
                    CalibrationRound.reviewer_a_id == ctx.user_id,
                    CalibrationRound.reviewer_b_id == ctx.user_id,
                ),
            )
        ),
    )
    if gold_set_key is not None:
        statement = statement.where(GoldSetVersion.gold_set_key == gold_set_key)
    versions = session.scalars(
        statement.order_by(
            GoldSetVersion.published_at.desc(),
            GoldSetVersion.gold_set_version_id.desc(),
        ).limit(limit)
    ).all()
    return [
        _gold_version_data(session, ctx, version, include_annotations=False) for version in versions
    ]


def get_gold_set_version(
    session: Session,
    ctx: RequestContext,
    gold_set_version_id: str,
) -> dict[str, Any]:
    version = session.scalar(
        select(GoldSetVersion).where(
            GoldSetVersion.gold_set_version_id == gold_set_version_id,
            GoldSetVersion.tenant_id == ctx.tenant_id,
            GoldSetVersion.project_id == ctx.project_id,
        )
    )
    if version is None:
        raise ApiError("GOLD_SET_VERSION_NOT_FOUND", "金标版本不存在", 404)
    return _gold_version_data(session, ctx, version, include_annotations=True)


__all__ = [
    "adjudicate_calibration_item",
    "calibration_round_data",
    "claim_calibration_item",
    "count_gold_versions",
    "create_calibration_round",
    "get_calibration_round_detail",
    "get_gold_set_version",
    "list_gold_set_versions",
    "list_calibration_conflicts",
    "list_calibration_rounds",
    "list_my_calibration_assignments",
    "release_calibration_gold",
    "submit_calibration_assignment",
]
