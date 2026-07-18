from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$"
METRIC_KEY_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]{1,95}$"


class StrictExperimentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExperimentArmRequest(StrictExperimentModel):
    arm_key: Literal["control", "candidate"]
    task_version_id: StrictStr = Field(min_length=2, max_length=128, pattern=IDENTIFIER_PATTERN)
    allocation_ppm: StrictInt = Field(ge=1, le=999_999)


class ExperimentMetricRequest(StrictExperimentModel):
    metric_key: StrictStr = Field(min_length=2, max_length=96, pattern=METRIC_KEY_PATTERN)
    direction: Literal["increase", "decrease"]
    minimum_effect: StrictFloat = 0.0

    @field_validator("minimum_effect")
    @classmethod
    def minimum_effect_is_finite(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("minimum_effect must be finite and non-negative")
        return value


class ExperimentGuardrailRequest(StrictExperimentModel):
    metric_key: StrictStr = Field(min_length=2, max_length=96, pattern=METRIC_KEY_PATTERN)
    direction: Literal["increase", "decrease"]
    maximum_regression: StrictFloat = 0.0

    @field_validator("maximum_regression")
    @classmethod
    def maximum_regression_is_finite(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("maximum_regression must be finite and non-negative")
        return value


class ExperimentCreateRequest(StrictExperimentModel):
    experiment_id: StrictStr | None = Field(
        default=None,
        min_length=2,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    name: StrictStr = Field(min_length=2, max_length=255)
    # The current execution path freezes and promotes TaskVersion resources. Keep
    # the public contract honest until model/prompt/policy arms have their own
    # typed execution and release semantics.
    experiment_kind: Literal["task_version"]
    variant_dimension: Literal["workflow", "model", "prompt", "label_policy", "bundle"] = "workflow"
    task_type_id: StrictStr = Field(min_length=2, max_length=128, pattern=IDENTIFIER_PATTERN)
    hypothesis: StrictStr = Field(min_length=10, max_length=2000)
    allocation_unit: Literal[
        "audio_session",
        "conversation",
        "store",
        "user",
        "device",
        "business_object",
    ]
    arms: list[ExperimentArmRequest] = Field(min_length=2, max_length=2)
    primary_metric: ExperimentMetricRequest
    guardrails: list[ExperimentGuardrailRequest] = Field(default_factory=list, max_length=16)
    min_sample_size_per_arm: StrictInt = Field(ge=3, le=10_000_000)
    confidence_level: StrictFloat = 0.95

    @field_validator("confidence_level")
    @classmethod
    def confidence_level_is_supported(cls, value: float) -> float:
        if value not in {0.9, 0.95, 0.99}:
            raise ValueError("confidence_level must be one of 0.9, 0.95, or 0.99")
        return value

    @model_validator(mode="after")
    def experiment_design_is_coherent(self) -> ExperimentCreateRequest:
        if {arm.arm_key for arm in self.arms} != {"control", "candidate"}:
            raise ValueError("arms must contain exactly control and candidate")
        if sum(arm.allocation_ppm for arm in self.arms) != 1_000_000:
            raise ValueError("arm allocation_ppm must sum to 1000000")
        if len({arm.task_version_id for arm in self.arms}) != 2:
            raise ValueError("control and candidate must use different task versions")
        guardrail_keys = [guardrail.metric_key for guardrail in self.guardrails]
        if len(guardrail_keys) != len(set(guardrail_keys)):
            raise ValueError("guardrail metric keys must be unique")
        if self.primary_metric.metric_key in set(guardrail_keys):
            raise ValueError("primary metric cannot also be a guardrail")
        return self


class ExperimentStartRequest(StrictExperimentModel):
    expected_resource_version: StrictInt = Field(ge=1)


class ExperimentAssignmentRequest(StrictExperimentModel):
    subject_key: StrictStr = Field(min_length=1, max_length=512)

    @field_validator("subject_key")
    @classmethod
    def subject_key_is_normalized(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("subject_key must not be blank")
        return normalized


class ExperimentExposureRequest(StrictExperimentModel):
    assignment_id: StrictStr = Field(min_length=2, max_length=128, pattern=IDENTIFIER_PATTERN)
    exposure_key: StrictStr = Field(min_length=1, max_length=512)
    occurred_at: datetime = Field(strict=False)
    context_refs: dict[StrictStr, StrictStr] = Field(default_factory=dict, max_length=32)

    @field_validator("exposure_key")
    @classmethod
    def exposure_key_is_normalized(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("exposure_key must not be blank")
        return normalized


class ExperimentOutcomeRequest(StrictExperimentModel):
    exposure_id: StrictStr = Field(min_length=2, max_length=128, pattern=IDENTIFIER_PATTERN)
    metric_values: dict[StrictStr, StrictFloat] = Field(min_length=1, max_length=32)
    occurred_at: datetime = Field(strict=False)
    evidence_refs: list[StrictStr] = Field(min_length=1, max_length=64)

    @field_validator("metric_values")
    @classmethod
    def metric_values_are_finite(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not math.isfinite(metric_value) for metric_value in value.values()):
            raise ValueError("metric values must be finite")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_are_unique(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("evidence_refs must be non-blank and unique")
        return normalized


class ExperimentMetricSnapshotRequest(StrictExperimentModel):
    pass


class ExperimentDecisionRequest(StrictExperimentModel):
    decision: Literal["pause", "resume", "stop", "promote_candidate", "reject_candidate"]
    metric_snapshot_id: StrictStr | None = Field(
        default=None,
        min_length=2,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    expected_resource_version: StrictInt = Field(ge=1)
    reason: StrictStr = Field(min_length=2, max_length=2000)

    @model_validator(mode="after")
    def terminal_decision_requires_snapshot(self) -> ExperimentDecisionRequest:
        if (
            self.decision in {"promote_candidate", "reject_candidate"}
            and not self.metric_snapshot_id
        ):
            raise ValueError("terminal decision requires metric_snapshot_id")
        return self
