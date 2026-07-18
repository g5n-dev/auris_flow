from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.models import LabelCandidate, LabelPolicyEvaluation, LabelPolicyVersion, LabelVersion


def get_label_version_for_update(
    session: Session,
    ctx: RequestContext,
    label_version_id: str,
) -> LabelVersion | None:
    return session.scalar(
        select(LabelVersion)
        .where(
            LabelVersion.label_version_id == label_version_id,
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
        )
        .with_for_update()
    )


def get_candidate_for_update(
    session: Session,
    ctx: RequestContext,
    candidate_id: str,
) -> LabelCandidate | None:
    return session.scalar(
        select(LabelCandidate)
        .where(
            LabelCandidate.candidate_id == candidate_id,
            LabelCandidate.tenant_id == ctx.tenant_id,
            LabelCandidate.project_id == ctx.project_id,
        )
        .with_for_update()
    )


def get_policy_version(
    session: Session,
    ctx: RequestContext,
    policy_version_id: str,
) -> LabelPolicyVersion | None:
    return session.scalar(
        select(LabelPolicyVersion).where(
            LabelPolicyVersion.policy_version_id == policy_version_id,
            LabelPolicyVersion.tenant_id == ctx.tenant_id,
            LabelPolicyVersion.project_id == ctx.project_id,
        )
    )


def find_policy_artifact(
    session: Session,
    ctx: RequestContext,
    *,
    label_version_id: str,
    canonical_sha256: str,
) -> LabelPolicyVersion | None:
    return session.scalar(
        select(LabelPolicyVersion).where(
            LabelPolicyVersion.tenant_id == ctx.tenant_id,
            LabelPolicyVersion.project_id == ctx.project_id,
            LabelPolicyVersion.label_version_id == label_version_id,
            LabelPolicyVersion.canonical_sha256 == canonical_sha256,
        )
    )


def find_policy_evaluation(
    session: Session,
    ctx: RequestContext,
    *,
    target_type: str,
    target_id: str,
    policy_version_id: str,
    facts_sha256: str,
) -> LabelPolicyEvaluation | None:
    return session.scalar(
        select(LabelPolicyEvaluation).where(
            LabelPolicyEvaluation.tenant_id == ctx.tenant_id,
            LabelPolicyEvaluation.project_id == ctx.project_id,
            LabelPolicyEvaluation.target_type == target_type,
            LabelPolicyEvaluation.target_id == target_id,
            LabelPolicyEvaluation.policy_version_id == policy_version_id,
            LabelPolicyEvaluation.facts_sha256 == facts_sha256,
        )
    )
