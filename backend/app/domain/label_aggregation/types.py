from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class LabelKind(StrEnum):
    BOOLEAN = "boolean"
    MULTI = "multi"
    CATEGORICAL = "categorical"
    NUMERIC = "numeric"
    TEMPORAL = "temporal"
    HIERARCHY = "hierarchy"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SourceType(StrEnum):
    HUMAN_CONFIRMED = "human_confirmed"
    VERIFIED_BUSINESS_DOCUMENT = "verified_business_document"
    DETERMINISTIC_RULE = "deterministic_rule"
    MODEL = "model"
    LLM = "llm"
    INFERRED = "inferred"


class AggregationMode(StrEnum):
    L1 = "l1"
    L2 = "l2"


class AggregateDecision(StrEnum):
    AUTO_ACCEPT = "auto_accept"
    REQUIRE_REVIEW = "require_review"
    ABSTAIN = "abstain"


@dataclass(frozen=True, slots=True)
class TimeSpan:
    start: float
    end: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.start) or not math.isfinite(self.end):
            raise ValueError("time span bounds must be finite")
        if self.start > self.end:
            raise ValueError("time span start must not exceed end")

    def to_dict(self) -> dict[str, float]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True, slots=True)
class SourceWeight:
    source_family: str
    weight: float

    def __post_init__(self) -> None:
        if not self.source_family.strip():
            raise ValueError("source_family must not be empty")
        if not math.isfinite(self.weight):
            raise ValueError("source weight must be finite")
        if self.weight <= 0:
            raise ValueError("source weight must be positive")


@dataclass(frozen=True, slots=True)
class LabelDefinition:
    label_id: str
    canonical_name: str
    aliases: tuple[str, ...] = ()
    kind: LabelKind = LabelKind.BOOLEAN
    risk_level: RiskLevel = RiskLevel.LOW
    parent_ids: tuple[str, ...] = ()
    mutex_group: str | None = None
    numeric_tolerance: float = 0.0

    def __post_init__(self) -> None:
        if not self.label_id.strip():
            raise ValueError("label_id must not be empty")
        if not self.canonical_name.strip():
            raise ValueError("canonical_name must not be empty")
        if not math.isfinite(self.numeric_tolerance):
            raise ValueError("numeric_tolerance must be finite")
        if self.numeric_tolerance < 0:
            raise ValueError("numeric_tolerance must be non-negative")
        object.__setattr__(self, "aliases", tuple(self.aliases))
        object.__setattr__(self, "parent_ids", tuple(dict.fromkeys(self.parent_ids)))
        object.__setattr__(self, "kind", LabelKind(self.kind))
        object.__setattr__(self, "risk_level", RiskLevel(self.risk_level))


@dataclass(frozen=True, slots=True)
class LabelObservation:
    observation_id: str
    subject_scope: str
    subject_key: str
    raw_label: str
    value: object
    source_family: str
    source_type: SourceType
    raw_confidence: float
    calibrated_confidence: float | None
    evidence_hash: str | None
    trace_id: str
    evidence_valid: bool = True
    novel: bool = False
    correlation_group_id: str | None = None
    extraction_run_id: str | None = None
    model_version: str | None = None
    prompt_version_id: str | None = None
    evidence_ref_id: str | None = None
    evidence_start: float | None = None
    evidence_end: float | None = None

    def __post_init__(self) -> None:
        required_values = {
            "observation_id": self.observation_id,
            "subject_scope": self.subject_scope,
            "subject_key": self.subject_key,
            "raw_label": self.raw_label,
            "source_family": self.source_family,
            "trace_id": self.trace_id,
        }
        for name, value in required_values.items():
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        for name, confidence in (
            ("raw confidence", self.raw_confidence),
            ("calibrated confidence", self.calibrated_confidence),
        ):
            if confidence is not None and not 0 <= confidence <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if (self.evidence_start is None) != (self.evidence_end is None):
            raise ValueError("evidence interval requires both start and end")
        if (
            self.evidence_start is not None
            and self.evidence_end is not None
            and self.evidence_end <= self.evidence_start
        ):
            raise ValueError("evidence interval end must be greater than start")
        object.__setattr__(self, "source_type", SourceType(self.source_type))


@dataclass(frozen=True, slots=True)
class AggregationPolicy:
    mode: AggregationMode = AggregationMode.L1
    source_weights: tuple[SourceWeight, ...] = ()
    prior_probability: float = 0.5
    l2_accept_threshold: float = 0.95
    categorical_margin: float = 0.15
    temporal_iou_threshold: float = 0.6
    multi_selection_threshold: float = 0.5
    min_independent_sources: int = 2
    random_audit_rate: float = 0.05

    def __post_init__(self) -> None:
        probability_fields = {
            "prior_probability": self.prior_probability,
            "l2_accept_threshold": self.l2_accept_threshold,
            "categorical_margin": self.categorical_margin,
            "temporal_iou_threshold": self.temporal_iou_threshold,
            "multi_selection_threshold": self.multi_selection_threshold,
            "random_audit_rate": self.random_audit_rate,
        }
        for name, value in probability_fields.items():
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.min_independent_sources < 1:
            raise ValueError("min_independent_sources must be positive")
        object.__setattr__(self, "mode", AggregationMode(self.mode))
        object.__setattr__(self, "source_weights", tuple(self.source_weights))
        families = [weight.source_family for weight in self.source_weights]
        if len(families) != len(set(families)):
            raise ValueError("source family weights must be unique")

    def weight_for(self, source_family: str) -> float:
        return next(
            (
                source_weight.weight
                for source_weight in self.source_weights
                if source_weight.source_family == source_family
            ),
            1.0,
        )


@dataclass(frozen=True, slots=True)
class ObservationContribution:
    observation_id: str
    source_family: str
    source_type: SourceType
    source_priority: int
    effective_confidence: float
    confidence_basis: str
    source_weight: float
    weighted_log_odds: float
    included: bool
    exclusion_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "source_family": self.source_family,
            "source_type": self.source_type.value,
            "source_priority": self.source_priority,
            "effective_confidence": self.effective_confidence,
            "confidence_basis": self.confidence_basis,
            "source_weight": self.source_weight,
            "weighted_log_odds": self.weighted_log_odds,
            "included": self.included,
            "exclusion_reason": self.exclusion_reason,
        }


@dataclass(frozen=True, slots=True)
class AggregationExplanation:
    operator: str
    source_precedence: SourceType
    independent_source_families: tuple[str, ...]
    contributions: tuple[ObservationContribution, ...]
    candidate_scores: tuple[tuple[str, float], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "source_precedence": self.source_precedence.value,
            "independent_source_families": list(self.independent_source_families),
            "contributions": [contribution.to_dict() for contribution in self.contributions],
            "candidate_scores": [
                {"candidate": candidate, "score": score}
                for candidate, score in self.candidate_scores
            ],
        }


@dataclass(frozen=True, slots=True)
class LabelAggregate:
    aggregate_id: str
    subject_scope: str
    subject_key: str
    label_id: str
    value: object
    score: float
    margin: float | None
    decision: AggregateDecision
    reason_codes: tuple[str, ...]
    ancestor_label_ids: tuple[str, ...]
    explanation: AggregationExplanation
    canonical_hash: str

    def to_dict(self) -> dict[str, Any]:
        value: object = self.value
        if isinstance(value, TimeSpan):
            value = value.to_dict()
        elif isinstance(value, tuple):
            value = list(value)
        return {
            "aggregate_id": self.aggregate_id,
            "subject_scope": self.subject_scope,
            "subject_key": self.subject_key,
            "label_id": self.label_id,
            "value": value,
            "score": self.score,
            "margin": self.margin,
            "decision": self.decision.value,
            "reason_codes": list(self.reason_codes),
            "ancestor_label_ids": list(self.ancestor_label_ids),
            "explanation": self.explanation.to_dict(),
            "canonical_hash": self.canonical_hash,
        }


@dataclass(frozen=True, slots=True)
class UnknownLabelSuggestion:
    normalized_label: str
    raw_labels: tuple[str, ...]
    observation_ids: tuple[str, ...]
    reason_code: str = "UNKNOWN_LABEL_REQUIRES_TAXONOMY_REVIEW"

    def to_dict(self) -> dict[str, Any]:
        return {
            "normalized_label": self.normalized_label,
            "raw_labels": list(self.raw_labels),
            "observation_ids": list(self.observation_ids),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class AggregationBatch:
    aggregates: tuple[LabelAggregate, ...]
    unknown_suggestions: tuple[UnknownLabelSuggestion, ...]
    policy_hash: str
    label_set_hash: str
    canonical_hash: str
    engine_version: str = "label-aggregation/1.0.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "aggregates": [aggregate.to_dict() for aggregate in self.aggregates],
            "unknown_suggestions": [
                suggestion.to_dict() for suggestion in self.unknown_suggestions
            ],
            "policy_hash": self.policy_hash,
            "label_set_hash": self.label_set_hash,
            "canonical_hash": self.canonical_hash,
            "engine_version": self.engine_version,
        }
