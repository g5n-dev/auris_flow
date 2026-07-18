from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.schemas.common import ApiMeta

IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class StrictLabelRecomputeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LabelRecomputePartition(StrictLabelRecomputeModel):
    partition_id: StrictStr = Field(min_length=2, max_length=128, pattern=IDENTIFIER_PATTERN)
    source_scope: dict[StrictStr, JsonValue] = Field(min_length=1)


class LabelRecomputeRunCreateRequest(StrictLabelRecomputeModel):
    target_label_version_id: StrictStr = Field(
        min_length=2, max_length=128, pattern=IDENTIFIER_PATTERN
    )
    mapping_bundle_id: StrictStr | None = Field(
        default=None, min_length=2, max_length=128, pattern=IDENTIFIER_PATTERN
    )
    mapping_bundle_sha256: StrictStr | None = Field(default=None, pattern=SHA256_PATTERN)
    source_environment: StrictStr = Field(min_length=2, max_length=32, pattern=IDENTIFIER_PATTERN)
    source_fact_namespace: StrictStr = Field(
        min_length=2, max_length=128, pattern=IDENTIFIER_PATTERN
    )
    source_head_generation: StrictInt = Field(ge=1)
    source_fact_set_id: StrictStr = Field(min_length=2, max_length=128, pattern=IDENTIFIER_PATTERN)
    source_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fact_namespace: StrictStr = Field(min_length=2, max_length=128, pattern=IDENTIFIER_PATTERN)
    fact_as_of: datetime = Field(strict=False)
    partitions: list[LabelRecomputePartition] = Field(min_length=1, max_length=1000)
    asset_scope: dict[StrictStr, JsonValue] = Field(min_length=1)
    coverage_policy: dict[StrictStr, JsonValue] = Field(min_length=1)
    coverage_min: StrictFloat = Field(ge=0, le=1)
    budget: dict[StrictStr, JsonValue] = Field(min_length=1)
    budget_units: StrictInt = Field(ge=1)

    @field_validator("fact_as_of")
    @classmethod
    def fact_as_of_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fact_as_of must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def anchors_and_partitions_are_unambiguous(self) -> LabelRecomputeRunCreateRequest:
        if (self.mapping_bundle_id is None) != (self.mapping_bundle_sha256 is None):
            raise ValueError("mapping bundle id and sha256 must be provided together")
        partition_ids = [partition.partition_id for partition in self.partitions]
        if len(partition_ids) != len(set(partition_ids)):
            raise ValueError("partition_id values must be unique")
        if self.fact_namespace == self.source_fact_namespace:
            raise ValueError("full recompute requires an independent candidate namespace")
        return self


class LabelRecomputeFactCandidate(StrictLabelRecomputeModel):
    aggregate_id: StrictStr = Field(min_length=2, max_length=128, pattern=IDENTIFIER_PATTERN)
    observation_ids: list[StrictStr] = Field(min_length=1, max_length=1000)
    subject_scope: StrictStr = Field(min_length=1, max_length=64)
    subject_key: StrictStr = Field(min_length=1, max_length=256)
    event_or_segment_id: StrictStr = Field(min_length=1, max_length=256)
    assertion_slot: StrictStr = Field(min_length=1, max_length=128)
    occurred_at: datetime = Field(strict=False)
    label_id: StrictStr = Field(min_length=1, max_length=128)
    value_type: Literal["boolean", "categorical", "multi", "numeric", "temporal", "hierarchical"]
    value: JsonValue

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)

    @field_validator("observation_ids")
    @classmethod
    def observation_ids_are_unique(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("observation_ids must be unique")
        return values


class LabelRecomputeRunItemCompletionRequest(StrictLabelRecomputeModel):
    attempt_generation: StrictInt = Field(ge=1)
    completion_receipt_id: StrictStr = Field(
        min_length=2, max_length=128, pattern=IDENTIFIER_PATTERN
    )
    status: Literal["success", "failed"]
    facts: list[LabelRecomputeFactCandidate] = Field(default_factory=list, max_length=100000)
    error_code: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    retryable: bool = True

    @model_validator(mode="after")
    def completion_shape_matches_status(self) -> LabelRecomputeRunItemCompletionRequest:
        if self.status == "success" and self.error_code is not None:
            raise ValueError("successful completion cannot include error_code")
        if self.status == "failed" and (self.facts or self.error_code is None):
            raise ValueError("failed completion requires error_code and cannot include facts")
        return self


class LabelRecomputeRunItemRetryRequest(StrictLabelRecomputeModel):
    expected_attempt_generation: StrictInt = Field(ge=1)


class LabelRecomputeMutationResponse(StrictLabelRecomputeModel):
    recompute_run_id: StrictStr
    status: Literal[
        "requested", "running", "candidate-complete", "partial-failed", "failed", "blocked"
    ]
    candidate_fact_set_id: StrictStr
    candidate_manifest_sha256: StrictStr | None = Field(default=None, pattern=SHA256_PATTERN)
    fact_namespace: StrictStr
    partition_count: StrictInt = Field(ge=1)
    completed_partition_count: StrictInt = Field(ge=0)
    failed_partition_count: StrictInt = Field(ge=0)
    row_count: StrictInt = Field(ge=0)
    audit_id: StrictInt = Field(ge=1)
    outbox_event_id: StrictInt = Field(ge=1)
    trace_id: StrictStr


class LabelRecomputeRunItemMutationResponse(StrictLabelRecomputeModel):
    recompute_run_id: StrictStr
    recompute_run_item_id: StrictStr
    partition_id: StrictStr
    status: Literal["queued", "running", "succeeded", "failed"]
    attempt_generation: StrictInt = Field(ge=1)
    row_count: StrictInt = Field(ge=0)
    source_manifest_sha256: StrictStr | None = Field(default=None, pattern=SHA256_PATTERN)
    result_manifest_sha256: StrictStr | None = Field(default=None, pattern=SHA256_PATTERN)
    content_sha256: StrictStr | None = Field(default=None, pattern=SHA256_PATTERN)
    run_status: Literal[
        "requested", "running", "candidate-complete", "partial-failed", "failed", "blocked"
    ]
    candidate_manifest_sha256: StrictStr | None = Field(default=None, pattern=SHA256_PATTERN)
    audit_id: StrictInt = Field(ge=1)
    outbox_event_id: StrictInt = Field(ge=1)
    trace_id: StrictStr


class LabelRecomputeMutationEnvelope(StrictLabelRecomputeModel):
    data: LabelRecomputeMutationResponse
    meta: ApiMeta


class LabelRecomputeRunItemMutationEnvelope(StrictLabelRecomputeModel):
    data: LabelRecomputeRunItemMutationResponse
    meta: ApiMeta
