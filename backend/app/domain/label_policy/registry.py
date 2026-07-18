from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.schemas.label_policy import LabelPolicyFacts, ScalarValue

ValueKind = Literal["bool", "int", "str"]
ThresholdDimension = Literal["ratio_ppm", "count", "u64", "duration_ms", "timestamp_ms"]


@dataclass(frozen=True)
class PathSpec:
    kind: ValueKind
    get: Callable[[LabelPolicyFacts], ScalarValue]
    threshold_dimension: ThresholdDimension | None = None


PATH_REGISTRY_V1: dict[str, PathSpec] = {
    "request.action": PathSpec("str", lambda facts: facts.request.action),
    "request.automation_level": PathSpec("str", lambda facts: facts.request.automation_level),
    "actor.kind": PathSpec("str", lambda facts: facts.actor.kind),
    "target.status": PathSpec("str", lambda facts: facts.target.status),
    "target.risk_level": PathSpec("str", lambda facts: facts.target.risk_level),
    "target.resource_version": PathSpec("int", lambda facts: facts.target.resource_version, "u64"),
    "target.same_scope": PathSpec("bool", lambda facts: facts.target.same_scope),
    "candidate.source_type": PathSpec("str", lambda facts: facts.candidate.source_type),
    "candidate.confidence_ppm": PathSpec(
        "int", lambda facts: facts.candidate.confidence_ppm, "ratio_ppm"
    ),
    "candidate.version_matches": PathSpec("bool", lambda facts: facts.candidate.version_matches),
    "candidate.overwrites_human": PathSpec("bool", lambda facts: facts.candidate.overwrites_human),
    "candidate.business_document_conflict": PathSpec(
        "bool", lambda facts: facts.candidate.business_document_conflict
    ),
    "evidence.total_count": PathSpec("int", lambda facts: facts.evidence.total_count, "count"),
    "evidence.valid_count": PathSpec("int", lambda facts: facts.evidence.valid_count, "count"),
    "evidence.pending_count": PathSpec("int", lambda facts: facts.evidence.pending_count, "count"),
    "evidence.cross_scope_count": PathSpec(
        "int", lambda facts: facts.evidence.cross_scope_count, "count"
    ),
    "evidence.stale_count": PathSpec("int", lambda facts: facts.evidence.stale_count, "count"),
    "evidence.invalid_window_count": PathSpec(
        "int", lambda facts: facts.evidence.invalid_window_count, "count"
    ),
    "evidence.missing_checksum_count": PathSpec(
        "int", lambda facts: facts.evidence.missing_checksum_count, "count"
    ),
    "conflicts.open_count": PathSpec("int", lambda facts: facts.conflicts.open_count, "count"),
    "conflicts.high_risk_open_count": PathSpec(
        "int", lambda facts: facts.conflicts.high_risk_open_count, "count"
    ),
    "conflicts.human_disagreement_count": PathSpec(
        "int", lambda facts: facts.conflicts.human_disagreement_count, "count"
    ),
    "conflicts.equal_precedence_count": PathSpec(
        "int", lambda facts: facts.conflicts.equal_precedence_count, "count"
    ),
    "reviews.pending_count": PathSpec("int", lambda facts: facts.reviews.pending_count, "count"),
    "reviews.rejected_count": PathSpec("int", lambda facts: facts.reviews.rejected_count, "count"),
    "reviews.distinct_human_approver_count": PathSpec(
        "int", lambda facts: facts.reviews.distinct_human_approver_count, "count"
    ),
    "evaluation.status": PathSpec("str", lambda facts: facts.evaluation.status),
    "evaluation.same_optimization_run": PathSpec(
        "bool", lambda facts: facts.evaluation.same_optimization_run
    ),
    "evaluation.dataset_locked": PathSpec("bool", lambda facts: facts.evaluation.dataset_locked),
    "evaluation.sample_count": PathSpec(
        "int", lambda facts: facts.evaluation.sample_count, "count"
    ),
    "evaluation.labeling_f1_ppm": PathSpec(
        "int", lambda facts: facts.evaluation.labeling_f1_ppm, "ratio_ppm"
    ),
    "evaluation.conflict_rate_ppm": PathSpec(
        "int", lambda facts: facts.evaluation.conflict_rate_ppm, "ratio_ppm"
    ),
    "evaluation.json_validity_ppm": PathSpec(
        "int", lambda facts: facts.evaluation.json_validity_ppm, "ratio_ppm"
    ),
    "evaluation.blocking_regression_count": PathSpec(
        "int", lambda facts: facts.evaluation.blocking_regression_count, "count"
    ),
    "evaluation.blocking_badcase_count": PathSpec(
        "int", lambda facts: facts.evaluation.blocking_badcase_count, "count"
    ),
    "impact.assets_confirmed": PathSpec("bool", lambda facts: facts.impact.assets_confirmed),
    "impact.downstream_incompatible_count": PathSpec(
        "int", lambda facts: facts.impact.downstream_incompatible_count, "count"
    ),
    "release.rollback_available": PathSpec("bool", lambda facts: facts.release.rollback_available),
    "release.gray_traffic_ppm": PathSpec(
        "int", lambda facts: facts.release.gray_traffic_ppm, "ratio_ppm"
    ),
}
