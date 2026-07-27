from __future__ import annotations

import hashlib
import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.request_identifiers import public_suffix_from_hex

SHA256_PATTERN = r"^[0-9a-f]{64}$"
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$"


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceReference(StrictRequest):
    type: str = Field(min_length=1, max_length=64)
    id: str = Field(min_length=1, max_length=256)
    sha256: str = Field(pattern=SHA256_PATTERN)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_interval(self) -> EvidenceReference:
        if self.start_ms is not None and self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("evidence end_ms must be greater than start_ms")
        return self


class LabelObservationCreateRequest(StrictRequest):
    observation_id: str = Field(pattern=ID_PATTERN)
    extraction_run_id: str = Field(pattern=ID_PATTERN)
    subject_scope: str = Field(min_length=1, max_length=64)
    subject_key: str = Field(min_length=1, max_length=256)
    evidence_ref: EvidenceReference
    label_version_id: str = Field(pattern=ID_PATTERN)
    raw_label: str = Field(min_length=1, max_length=255)
    label_id: str | None = Field(default=None, max_length=128)
    value: Any
    value_type: Literal["boolean", "categorical", "multi", "numeric", "temporal", "hierarchical"]
    source_family: str = Field(min_length=1, max_length=128)
    source_type: Literal[
        "verified-business-document",
        "deterministic-rule",
        "model",
        "llm",
        "inferred",
    ]
    model_version: str = Field(min_length=1, max_length=128)
    prompt_version_id: str = Field(pattern=ID_PATTERN)
    schema_version: str = Field(min_length=1, max_length=64)
    calibration_version_id: str | None = Field(default=None, max_length=128)
    raw_confidence: float = Field(ge=0, le=1)
    calibrated_confidence: float | None = Field(default=None, ge=0, le=1)
    input_sha256: str = Field(pattern=SHA256_PATTERN)
    output_sha256: str = Field(pattern=SHA256_PATTERN)
    novel: bool = False

    @field_validator(
        "subject_scope",
        "subject_key",
        "raw_label",
        "source_family",
        "model_version",
        "schema_version",
    )
    @classmethod
    def text_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


class AggregationThresholds(StrictRequest):
    l2_accept_score: float = Field(default=0.95, ge=0.5, le=1)
    categorical_margin: float = Field(default=0.15, ge=0, le=1)
    temporal_iou: float = Field(default=0.6, ge=0, le=1)
    min_independent_sources: int = Field(default=2, ge=1, le=20)
    random_audit_rate: float = Field(default=0.05, ge=0, le=1)


class AggregationLabelDefinition(StrictRequest):
    label_id: str = Field(min_length=1, max_length=128)
    canonical_name: str = Field(min_length=1, max_length=255)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    kind: Literal["boolean", "categorical", "multi", "numeric", "temporal", "hierarchical"]
    risk_level: Literal["low", "medium", "high"] = "low"
    parent_ids: list[str] = Field(default_factory=list, max_length=20)
    mutual_exclusion_group: str | None = Field(default=None, max_length=128)
    numeric_tolerance: float = Field(default=0, ge=0)

    @field_validator("aliases")
    @classmethod
    def aliases_are_unique_and_non_blank(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("aliases must not contain blanks")
        if len(set(normalized)) != len(normalized):
            raise ValueError("aliases must be unique")
        return normalized


class LabelAggregationPolicyCreateRequest(StrictRequest):
    policy_version_id: str = Field(pattern=ID_PATTERN)
    label_version_id: str = Field(pattern=ID_PATTERN)
    policy_version: str = Field(min_length=1, max_length=64)
    mode: Literal["l1", "l2"] = "l1"
    status: Literal["draft", "active"] = "draft"
    source_weights: dict[str, float] = Field(default_factory=dict, max_length=64)
    calibration_versions: dict[str, str] = Field(default_factory=dict, max_length=64)
    thresholds: AggregationThresholds = Field(default_factory=AggregationThresholds)
    label_definitions: list[AggregationLabelDefinition] = Field(min_length=1, max_length=1000)

    @field_validator("source_weights")
    @classmethod
    def source_weights_are_bounded(cls, values: dict[str, float]) -> dict[str, float]:
        if any(not key.strip() or value <= 0 or value > 10 for key, value in values.items()):
            raise ValueError("source weights require non-blank families and values in (0, 10]")
        return values

    @model_validator(mode="after")
    def definitions_do_not_collide(self) -> LabelAggregationPolicyCreateRequest:
        label_ids = [item.label_id for item in self.label_definitions]
        if len(set(label_ids)) != len(label_ids):
            raise ValueError("label definitions must have unique label_id values")
        if self.mode == "l2":
            if self.thresholds.min_independent_sources < 2:
                raise ValueError("L2 requires at least two independent source families")
            if self.thresholds.random_audit_rate < 0.05:
                raise ValueError("L2 requires at least a 5% deterministic stratified audit rate")
            if len(self.source_weights) < 2:
                raise ValueError("L2 requires weights for at least two source families")
            missing_calibrations = sorted(
                source_family
                for source_family in self.source_weights
                if source_family not in self.calibration_versions
                and any(
                    f"{definition.label_id}::{source_family}" not in self.calibration_versions
                    for definition in self.label_definitions
                )
            )
            if missing_calibrations:
                raise ValueError(
                    "L2 requires a locked calibration version for every weighted source family"
                )
        return self


class LabelAggregationRunCreateRequest(StrictRequest):
    aggregation_run_id: str = Field(pattern=ID_PATTERN)
    label_version_id: str = Field(pattern=ID_PATTERN)
    policy_version_id: str = Field(pattern=ID_PATTERN)
    observation_ids: list[str] = Field(min_length=1, max_length=5000)
    mode: Literal["l1", "l2"]

    @field_validator("observation_ids")
    @classmethod
    def observation_ids_are_unique(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("observation_ids must be unique")
        return values


class LabelCalibrationVersionCreateRequest(StrictRequest):
    calibration_version_id: str = Field(pattern=ID_PATTERN)
    label_version_id: str = Field(pattern=ID_PATTERN)
    label_id: str = Field(default="*", min_length=1, max_length=128)
    source_family: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    method: Literal["isotonic", "platt", "global-conservative"]
    status: Literal["draft", "published"] = "draft"
    gold_set_version_id: str = Field(pattern=ID_PATTERN)
    parameters: dict[str, Any] = Field(min_length=1, max_length=16)
    metrics: dict[str, Any] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def parameters_match_method(self) -> LabelCalibrationVersionCreateRequest:
        if self.method == "isotonic":
            xs = self.parameters.get("x")
            ys = self.parameters.get("y")
            valid = (
                isinstance(xs, list)
                and isinstance(ys, list)
                and len(xs) == len(ys)
                and len(xs) >= 2
                and all(isinstance(item, int | float) and math.isfinite(item) for item in xs)
                and all(
                    isinstance(item, int | float) and math.isfinite(item) and 0 <= item <= 1
                    for item in ys
                )
                and all(float(xs[index]) < float(xs[index + 1]) for index in range(len(xs) - 1))
                and all(float(ys[index]) <= float(ys[index + 1]) for index in range(len(ys) - 1))
            )
            if not valid:
                raise ValueError("isotonic parameters require increasing x and monotonic y")
        elif self.method == "platt":
            a = self.parameters.get("a")
            b = self.parameters.get("b")
            if not (
                isinstance(a, int | float)
                and isinstance(b, int | float)
                and math.isfinite(a)
                and math.isfinite(b)
                and a > 0
            ):
                raise ValueError("platt parameters require finite a > 0 and finite b")
        else:
            shrink = self.parameters.get("shrink")
            cap = self.parameters.get("cap", 0.95)
            if not (
                isinstance(shrink, int | float)
                and isinstance(cap, int | float)
                and math.isfinite(shrink)
                and math.isfinite(cap)
                and 0 < shrink <= 1
                and 0.5 <= cap < 1
            ):
                raise ValueError(
                    "global-conservative parameters require shrink in (0,1] and cap in [0.5,1)"
                )
        return self


class ExtractionSourceBinding(StrictRequest):
    source_family: str = Field(min_length=1, max_length=128)
    source_type: Literal[
        "verified-business-document",
        "deterministic-rule",
        "model",
        "llm",
        "inferred",
    ] = "model"
    provider: str = Field(default="internal", min_length=1, max_length=64)
    adapter: str = Field(default="model-adapter", min_length=1, max_length=64)

    @field_validator("source_family", "provider", "adapter")
    @classmethod
    def source_text_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("source binding values must not be blank")
        return normalized


class ExtractionSubjectReference(StrictRequest):
    """A canonical, bounded subject in one extraction manifest.

    ``id`` is retained as a compatibility alias for older clients, but every
    accepted item resolves to exactly one non-empty subject key.  This prevents
    an empty/malformed list item from silently disabling the completion
    whitelist.
    """

    subject_key: str | None = Field(default=None, min_length=1, max_length=256)
    id: str | None = Field(default=None, min_length=1, max_length=256)
    evidence_ref: str | None = Field(default=None, min_length=1, max_length=512)
    data_range: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def resolve_one_subject_key(self) -> ExtractionSubjectReference:
        subject_key = self.subject_key.strip() if self.subject_key is not None else None
        legacy_id = self.id.strip() if self.id is not None else None
        if subject_key is None and legacy_id is None:
            raise ValueError("subject_key or id is required")
        if subject_key is not None and legacy_id is not None and subject_key != legacy_id:
            raise ValueError("subject_key and id must identify the same subject")
        self.subject_key = subject_key or legacy_id
        return self


class LabelExtractionRunCreateRequest(StrictRequest):
    extraction_run_id: str = Field(pattern=ID_PATTERN)
    label_version_id: str = Field(pattern=ID_PATTERN)
    prompt_version_id: str = Field(pattern=ID_PATTERN)
    model_version: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=64)
    subject_scope: str = Field(min_length=1, max_length=64)
    subject_refs: list[ExtractionSubjectReference] = Field(min_length=1, max_length=1000)
    input_sha256: str = Field(pattern=SHA256_PATTERN)
    aggregation_policy_version_id: str = Field(
        max_length=128,
        pattern=ID_PATTERN,
    )
    source_bindings: list[ExtractionSourceBinding] = Field(default_factory=list, max_length=20)
    execution_mode: Literal["production", "shadow", "diagnostic"] = "production"

    @field_validator("subject_refs")
    @classmethod
    def subjects_are_unique(
        cls, values: list[ExtractionSubjectReference]
    ) -> list[ExtractionSubjectReference]:
        keys = [item.subject_key for item in values]
        if len(keys) != len(set(keys)):
            raise ValueError("subject_refs must resolve to unique subject keys")
        return values

    @model_validator(mode="after")
    def lock_at_least_one_source_lineage(self) -> LabelExtractionRunCreateRequest:
        if not self.source_bindings:
            digest = public_suffix_from_hex(
                hashlib.sha256(
                    f"{self.model_version}\0{self.prompt_version_id}".encode()
                ).hexdigest(),
                suffix_length=16,
            )
            self.source_bindings = [ExtractionSourceBinding(source_family=f"model:{digest}")]
        families = [item.source_family for item in self.source_bindings]
        if len(families) != len(set(families)):
            raise ValueError("source_bindings must use unique source_family values")
        return self


class PromptAssetCreateRequest(StrictRequest):
    prompt_asset_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=255)
    capability: Literal["labeling", "prompt-optimization"]
    label_version_id: str | None = Field(default=None, max_length=128)


class PromptVersionCreateRequest(StrictRequest):
    prompt_version_id: str = Field(pattern=ID_PATTERN)
    prompt_asset_id: str = Field(pattern=ID_PATTERN)
    version: str = Field(min_length=1, max_length=64)
    parent_version_id: str | None = Field(default=None, max_length=128)
    label_version_id: str | None = Field(default=None, max_length=128)
    schema_version: str = Field(min_length=1, max_length=64)
    model_version: str | None = Field(default=None, max_length=128)
    template: dict[str, Any] = Field(min_length=1, max_length=32)
    output_schema: dict[str, Any] = Field(min_length=1, max_length=128)
    generation_params: dict[str, Any] = Field(default_factory=dict, max_length=64)
    structured_diff: dict[str, Any] = Field(default_factory=dict, max_length=128)
    source_badcase_refs: list[str] = Field(default_factory=list, max_length=500)


class LabelVersionEvaluationLockRequest(StrictRequest):
    expected_resource_version: int = Field(ge=1)
    prompt_version_id: str = Field(pattern=ID_PATTERN)
    model_version: str = Field(min_length=1, max_length=128)
    aggregation_policy_version_id: str = Field(pattern=ID_PATTERN)
    eval_dataset_version_id: str = Field(pattern=ID_PATTERN)
    optimization_run_id: str = Field(pattern=ID_PATTERN)
    confirmation: Literal["lock-for-evaluation"]


class ReleaseDeploymentCreateRequest(StrictRequest):
    deployment_id: str = Field(pattern=ID_PATTERN)
    environment: Literal["shadow", "staging", "production"]
    label_version_id: str = Field(pattern=ID_PATTERN)
    prompt_version_id: str = Field(pattern=ID_PATTERN)
    model_version: str = Field(min_length=1, max_length=128)
    aggregation_policy_version_id: str = Field(pattern=ID_PATTERN)
    eval_dataset_version_id: str = Field(pattern=ID_PATTERN)
    eval_run_id: str = Field(pattern=ID_PATTERN)
    rollback_target_deployment_id: str | None = Field(default=None, max_length=128)


class ReleaseTransitionRequest(StrictRequest):
    action: Literal["approve-gray", "promote", "rollback"]
    reason: str = Field(min_length=1, max_length=1000)
    expected_status: str = Field(min_length=1, max_length=32)
    monitor_metrics: dict[str, Any] = Field(default_factory=dict, max_length=128)


class ReleaseHeadBootstrapRequest(StrictRequest):
    confirmation: Literal["bootstrap-last-known-good"]
    reason: str = Field(min_length=1, max_length=1000)
    expected_no_active_head: Literal[True]

    @field_validator("reason")
    @classmethod
    def bootstrap_reason_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class ReleaseOnlineMetrics(StrictRequest):
    sample_count: int = Field(ge=1)
    json_valid_rate: float = Field(ge=0, le=1)
    conflict_rate: float = Field(ge=0, le=1)
    critical_recall_delta_pp: float = Field(ge=-100, le=100)
    human_override_delta_pp: float = Field(ge=-100, le=100)
    cost_ratio: float = Field(gt=0)
    latency_ratio: float = Field(gt=0)
    abstention_rate: float | None = Field(default=None, ge=0, le=1)
    p95_latency_ms: float | None = Field(default=None, ge=0)


class ReleaseMonitorSampleRequest(StrictRequest):
    sample_id: str = Field(pattern=ID_PATTERN)
    observed_at: datetime
    expected_status: Literal["shadowing", "gray-releasing", "monitoring"]
    window_minutes: int = Field(default=5, ge=1, le=60)
    stable_window_complete: bool = False
    metrics: ReleaseOnlineMetrics


class TaxonomySuggestionDecisionRequest(StrictRequest):
    action: Literal["alias", "create", "merge", "split", "reject", "escalate"]
    canonical_target_label_id: str | None = Field(default=None, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)


class ClosedLoopReviewSubmissionRequest(StrictRequest):
    decision: Literal["accepted", "modified", "rejected"]
    note: str | None = Field(default=None, max_length=1000)
    value: Any | None = None
    taxonomy_action: Literal["alias", "create", "merge", "split", "reject"] | None = None
    canonical_target_label_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def taxonomy_fields_are_consistent(self) -> ClosedLoopReviewSubmissionRequest:
        if self.taxonomy_action in {"alias", "merge"} and not self.canonical_target_label_id:
            raise ValueError("alias/merge requires canonical_target_label_id")
        if self.decision == "rejected" and self.taxonomy_action not in {None, "reject"}:
            raise ValueError("rejected taxonomy review only accepts reject action")
        if self.decision != "rejected" and self.taxonomy_action == "reject":
            raise ValueError("reject taxonomy action requires rejected decision")
        return self


class ClosedLoopReviewAdjudicationRequest(ClosedLoopReviewSubmissionRequest):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def reason_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class LabelBadcaseCreateRequest(StrictRequest):
    badcase_id: str | None = Field(default=None, min_length=3, max_length=128)
    capability: Literal["labeling", "prompt-optimization"]
    failure_reason: str = Field(min_length=1, max_length=128)
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    source_ref: dict[str, Any] = Field(min_length=1, max_length=16)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    label_version_id: str | None = Field(default=None, max_length=128)
    prompt_version_id: str | None = Field(default=None, max_length=128)
    aggregate_id: str | None = Field(default=None, max_length=128)
    review_decision_id: str | None = Field(default=None, max_length=128)
    expected_value: Any | None = None
    actual_value: Any | None = None
    field_diff: dict[str, Any] = Field(default_factory=dict, max_length=128)

    @field_validator("source_ref")
    @classmethod
    def source_ref_has_type_and_id(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value.get("type"), str) or not str(value["type"]).strip():
            raise ValueError("source_ref.type is required")
        if not isinstance(value.get("id"), str) or not str(value["id"]).strip():
            raise ValueError("source_ref.id is required")
        return value


class HumanReviewDecisionBatchItem(StrictRequest):
    review_task_id: str = Field(pattern=ID_PATTERN)
    decision: Literal["accepted", "rejected", "escalated"]
    note: str | None = Field(default=None, max_length=1000)


class HumanReviewDecisionBatchRequest(StrictRequest):
    items: list[HumanReviewDecisionBatchItem] = Field(min_length=1, max_length=100)

    @field_validator("items")
    @classmethod
    def task_ids_are_unique(
        cls, values: list[HumanReviewDecisionBatchItem]
    ) -> list[HumanReviewDecisionBatchItem]:
        ids = [item.review_task_id for item in values]
        if len(set(ids)) != len(ids):
            raise ValueError("review_task_id values must be unique")
        return values
