from __future__ import annotations

import json
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.core.errors import ApiError
from app.schemas.common import parse_payload

MAX_POLICY_SOURCE_BYTES = 64 * 1024
MAX_EVALUATION_SOURCE_BYTES = 320 * 1024


class StrictPolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


ScalarValue: TypeAlias = StrictBool | StrictInt | StrictStr | None
ComparableValue: TypeAlias = StrictBool | StrictInt | StrictStr
PolicyEffect: TypeAlias = Literal["pass", "gray_only", "require_review", "block"]
PolicyKind: TypeAlias = Literal["label-candidate", "label-version-release"]
ConfusionMatrix: TypeAlias = dict[StrictStr, StrictInt | dict[StrictStr, StrictInt]]


class LogicalExpression(StrictPolicyModel):
    op: Literal["all", "any"]
    items: list[Expression] = Field(min_length=1, max_length=32)


class NotExpression(StrictPolicyModel):
    op: Literal["not"]
    item: Expression


class CompareExpression(StrictPolicyModel):
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte"]
    path: StrictStr = Field(min_length=3, max_length=128, pattern=r"^[a-z][a-z0-9_.]+$")
    value: ScalarValue = None
    threshold: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]+$",
    )

    @model_validator(mode="after")
    def exactly_one_rhs(self) -> CompareExpression:
        has_value = "value" in self.model_fields_set
        has_threshold = self.threshold is not None
        if has_value == has_threshold:
            raise ValueError("comparison requires exactly one of value or threshold")
        return self


class SetExpression(StrictPolicyModel):
    op: Literal["in", "not_in"]
    path: StrictStr = Field(min_length=3, max_length=128, pattern=r"^[a-z][a-z0-9_.]+$")
    values: list[ComparableValue] = Field(min_length=1, max_length=64)


class NullExpression(StrictPolicyModel):
    op: Literal["is_null", "is_not_null"]
    path: StrictStr = Field(min_length=3, max_length=128, pattern=r"^[a-z][a-z0-9_.]+$")


Expression: TypeAlias = Annotated[
    LogicalExpression | NotExpression | CompareExpression | SetExpression | NullExpression,
    Field(discriminator="op"),
]

LogicalExpression.model_rebuild(_types_namespace={"Expression": Expression})
NotExpression.model_rebuild(_types_namespace={"Expression": Expression})


class PolicyThreshold(StrictPolicyModel):
    key: StrictStr = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]+$",
    )
    type: Literal["ratio_ppm", "count", "u64", "duration_ms", "timestamp_ms"]
    value: StrictInt = Field(ge=0, le=9_223_372_036_854_775_807)

    @model_validator(mode="after")
    def ratio_is_bounded(self) -> PolicyThreshold:
        if self.type == "ratio_ppm" and self.value > 1_000_000:
            raise ValueError("ratio_ppm must be between 0 and 1000000")
        return self


class PolicyPrecedence(StrictPolicyModel):
    sources: list[
        Literal[
            "human_confirmed",
            "verified_business_document",
            "deterministic_rule",
            "model_candidate",
            "llm_candidate",
            "low_confidence_inference",
        ]
    ] = Field(min_length=1, max_length=8)
    equal_rank: Literal["conflict"] = "conflict"
    human_disagreement: Literal["require_arbitration"] = "require_arbitration"
    unknown_source: Literal["reject"] = "reject"

    @field_validator("sources")
    @classmethod
    def sources_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("precedence sources must be unique")
        return value


def default_precedence() -> PolicyPrecedence:
    return PolicyPrecedence(
        sources=[
            "human_confirmed",
            "verified_business_document",
            "deterministic_rule",
            "model_candidate",
            "llm_candidate",
            "low_confidence_inference",
        ]
    )


class PolicyRule(StrictPolicyModel):
    rule_id: StrictStr = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9-]+$",
    )
    priority: StrictInt = Field(ge=0, le=10_000)
    when: Expression
    effect: PolicyEffect
    reason_code: StrictStr = Field(
        min_length=3,
        max_length=96,
        pattern=r"^[A-Z][A-Z0-9_]+$",
    )
    evidence_group: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]+$",
    )


class LabelPolicyDSL(StrictPolicyModel):
    dsl_version: Literal["1.0"]
    policy_kind: PolicyKind
    policy_key: StrictStr = Field(
        min_length=3,
        max_length=96,
        pattern=r"^[a-z][a-z0-9-]+$",
    )
    revision: StrictInt = Field(ge=1, le=1_000_000)
    fact_schema_version: Literal["label-policy-facts/1"] = "label-policy-facts/1"
    thresholds: list[PolicyThreshold] = Field(default_factory=list, max_length=64)
    precedence: PolicyPrecedence = Field(default_factory=default_precedence)
    rules: list[PolicyRule] = Field(min_length=1, max_length=128)
    default_effect: PolicyEffect

    @model_validator(mode="after")
    def identifiers_are_unambiguous(self) -> LabelPolicyDSL:
        threshold_keys = [threshold.key for threshold in self.thresholds]
        if len(threshold_keys) != len(set(threshold_keys)):
            raise ValueError("threshold keys must be unique")
        rule_ids = [rule.rule_id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("rule ids must be unique")
        priorities = [rule.priority for rule in self.rules]
        if len(priorities) != len(set(priorities)):
            raise ValueError("rule priorities must be unique to avoid ambiguous decisions")
        return self


class RequestFacts(StrictPolicyModel):
    action: Literal["evaluate_candidate", "publish_label_version"] | None = None
    automation_level: Literal["manual", "assisted", "agentic"] | None = None


class ActorFacts(StrictPolicyModel):
    kind: Literal["human", "service", "agent", "llm"] | None = None


class TargetFacts(StrictPolicyModel):
    status: StrictStr | None = Field(default=None, max_length=32)
    risk_level: Literal["low", "medium", "high", "critical"] | None = None
    resource_version: StrictInt | None = Field(default=None, ge=1)
    same_scope: StrictBool | None = None


class CandidateFacts(StrictPolicyModel):
    source_type: (
        Literal[
            "human_confirmed",
            "verified_business_document",
            "deterministic_rule",
            "model_candidate",
            "llm_candidate",
            "low_confidence_inference",
        ]
        | None
    ) = None
    confidence_ppm: StrictInt | None = Field(default=None, ge=0, le=1_000_000)
    version_matches: StrictBool | None = None
    overwrites_human: StrictBool | None = None
    business_document_conflict: StrictBool | None = None
    competing_source_type: (
        Literal[
            "human_confirmed",
            "verified_business_document",
            "deterministic_rule",
            "model_candidate",
            "llm_candidate",
            "low_confidence_inference",
        ]
        | None
    ) = None


class EvidenceArtifactFacts(StrictPolicyModel):
    evidence_pack_id: StrictStr = Field(min_length=1, max_length=128)
    status: StrictStr = Field(min_length=1, max_length=32)
    resource_version: StrictInt | None = Field(default=None, ge=1)
    checksum_sha256: StrictStr | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
    )
    window_start_ms: StrictInt | None = Field(default=None, ge=0)
    window_end_ms: StrictInt | None = Field(default=None, ge=0)
    trace_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)


class EvidenceFacts(StrictPolicyModel):
    total_count: StrictInt | None = Field(default=None, ge=0)
    valid_count: StrictInt | None = Field(default=None, ge=0)
    pending_count: StrictInt | None = Field(default=None, ge=0)
    cross_scope_count: StrictInt | None = Field(default=None, ge=0)
    stale_count: StrictInt | None = Field(default=None, ge=0)
    invalid_window_count: StrictInt | None = Field(default=None, ge=0)
    missing_checksum_count: StrictInt | None = Field(default=None, ge=0)
    artifacts: list[EvidenceArtifactFacts] = Field(default_factory=list, max_length=64)


class ConflictFacts(StrictPolicyModel):
    open_count: StrictInt | None = Field(default=None, ge=0)
    high_risk_open_count: StrictInt | None = Field(default=None, ge=0)
    human_disagreement_count: StrictInt | None = Field(default=None, ge=0)
    equal_precedence_count: StrictInt | None = Field(default=None, ge=0)


class ReviewFacts(StrictPolicyModel):
    pending_count: StrictInt | None = Field(default=None, ge=0)
    rejected_count: StrictInt | None = Field(default=None, ge=0)
    distinct_human_approver_count: StrictInt | None = Field(default=None, ge=0)


class EvaluationFacts(StrictPolicyModel):
    status: StrictStr | None = Field(default=None, max_length=32)
    same_optimization_run: StrictBool | None = None
    dataset_locked: StrictBool | None = None
    metric_schema_version: StrictStr | None = Field(default=None, max_length=64)
    eligible_count: StrictInt | None = Field(default=None, ge=0)
    processed_count: StrictInt | None = Field(default=None, ge=0)
    skipped_count: StrictInt | None = Field(default=None, ge=0)
    invalid_count: StrictInt | None = Field(default=None, ge=0)
    abstain_count: StrictInt | None = Field(default=None, ge=0)
    duplicate_count: StrictInt | None = Field(default=None, ge=0)
    effective_count: StrictInt | None = Field(default=None, ge=0)
    effective_coverage_ppm: StrictInt | None = Field(default=None, ge=0, le=1_000_000)
    counts_conserved: StrictBool | None = None
    confusion_matrix: ConfusionMatrix | None = None
    # Compatibility field: release facts populate this from processed_count, never dataset metadata.
    sample_count: StrictInt | None = Field(default=None, ge=0)
    labeling_f1_ppm: StrictInt | None = Field(default=None, ge=0, le=1_000_000)
    conflict_rate_ppm: StrictInt | None = Field(default=None, ge=0, le=1_000_000)
    json_validity_ppm: StrictInt | None = Field(default=None, ge=0, le=1_000_000)
    blocking_regression_count: StrictInt | None = Field(default=None, ge=0)
    blocking_badcase_count: StrictInt | None = Field(default=None, ge=0)


class ImpactFacts(StrictPolicyModel):
    assets_confirmed: StrictBool | None = None
    downstream_incompatible_count: StrictInt | None = Field(default=None, ge=0)


class ReleaseFacts(StrictPolicyModel):
    rollback_available: StrictBool | None = None
    gray_traffic_ppm: StrictInt | None = Field(default=None, ge=0, le=1_000_000)


class PolicyFactProvenance(StrictPolicyModel):
    target_type: Literal["label_candidate", "label_version"] | None = None
    target_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    target_resource_version: StrictInt | None = Field(default=None, ge=1)
    label_version_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    evidence_pack_ids: list[StrictStr] = Field(default_factory=list, max_length=64)
    conflict_ids: list[StrictStr] = Field(default_factory=list, max_length=128)
    review_task_ids: list[StrictStr] = Field(default_factory=list, max_length=128)
    eval_run_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    optimization_run_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    eval_dataset_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    eval_dataset_version: StrictStr | None = Field(default=None, min_length=1, max_length=128)


class LabelPolicyFacts(StrictPolicyModel):
    request: RequestFacts = Field(default_factory=RequestFacts)
    actor: ActorFacts = Field(default_factory=ActorFacts)
    target: TargetFacts = Field(default_factory=TargetFacts)
    candidate: CandidateFacts = Field(default_factory=CandidateFacts)
    evidence: EvidenceFacts = Field(default_factory=EvidenceFacts)
    conflicts: ConflictFacts = Field(default_factory=ConflictFacts)
    reviews: ReviewFacts = Field(default_factory=ReviewFacts)
    evaluation: EvaluationFacts = Field(default_factory=EvaluationFacts)
    impact: ImpactFacts = Field(default_factory=ImpactFacts)
    release: ReleaseFacts = Field(default_factory=ReleaseFacts)
    provenance: PolicyFactProvenance = Field(default_factory=PolicyFactProvenance)


class LabelPolicyValidationRequest(StrictPolicyModel):
    policy: LabelPolicyDSL
    activate: StrictBool = False
    expected_label_resource_version: StrictInt | None = Field(default=None, ge=1)


class LabelCandidateEvaluationRequest(StrictPolicyModel):
    candidate_id: StrictStr = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    policy_version_id: StrictStr | None = Field(
        default=None,
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    expected_candidate_resource_version: StrictInt | None = Field(default=None, ge=1)
    create_human_review: StrictBool = True
    facts: LabelPolicyFacts


class LabelVersionPublishRequest(StrictPolicyModel):
    expected_label_resource_version: StrictInt | None = Field(default=None, ge=1)
    eval_run_id: StrictStr = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    gray_traffic_ppm: StrictInt = Field(default=100_000, ge=1, le=500_000)


class DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_float(value: str) -> object:
    raise ValueError(f"floating-point values are not allowed: {value}")


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite values are not allowed: {value}")


def parse_strict_json_request(
    raw: bytes,
    model: type[BaseModel],
    *,
    max_bytes: int,
) -> BaseModel:
    if len(raw) > max_bytes:
        raise ApiError(
            "POLICY_RESOURCE_LIMIT",
            f"请求体超过允许大小：{max_bytes} bytes",
            413,
        )
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKey, ValueError) as exc:
        raise ApiError("POLICY_JSON_INVALID", str(exc), 422) from exc
    if not isinstance(payload, dict):
        raise ApiError("POLICY_JSON_INVALID", "请求体必须是 JSON object", 422)
    return parse_payload(model, payload)
