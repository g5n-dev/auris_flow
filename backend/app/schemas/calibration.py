from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

RESOURCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
SHA256_PATTERN = r"^[a-f0-9]{64}$"
CalibrationReasonCode = Literal[
    "evidence_consistent",
    "evidence_conflict",
    "insufficient_evidence",
    "policy_exception",
    "other",
]


class StrictCalibrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class CalibrationDecisionValue(StrictCalibrationModel):
    """Frozen binary rubric output used by the first calibration profile.

    Keeping the value typed prevents arbitrary JSON from silently becoming a
    gold annotation. More rubric profiles can add discriminated value models
    without weakening the persisted contract.
    """

    decision: Literal["pass", "fail"]
    reason_code: CalibrationReasonCode | None = None
    evidence_refs: list[StrictStr] = Field(default_factory=list, max_length=16)

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_are_bounded(cls, value: list[str]) -> list[str]:
        if any(not ref.strip() or len(ref) > 1024 for ref in value):
            raise ValueError("evidence_refs must contain non-blank bounded references")
        if len(set(value)) != len(value):
            raise ValueError("evidence_refs must be unique")
        return value


class CalibrationSampleRequest(StrictCalibrationModel):
    source_case_id: StrictStr = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$",
    )
    evidence_ref: StrictStr = Field(min_length=1, max_length=1024)


class CalibrationRoundCreateRequest(StrictCalibrationModel):
    dataset_id: StrictStr = Field(min_length=1, max_length=128, pattern=RESOURCE_ID_PATTERN)
    dataset_version: StrictStr = Field(min_length=1, max_length=128)
    label_version: StrictStr = Field(min_length=1, max_length=128)
    rubric_version: StrictStr = Field(min_length=1, max_length=128)
    sample_manifest_sha256: StrictStr | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=SHA256_PATTERN,
    )
    reviewer_ids: list[StrictStr] = Field(min_length=2, max_length=2)
    adjudicator_id: StrictStr = Field(
        min_length=1,
        max_length=64,
        pattern=RESOURCE_ID_PATTERN,
    )
    samples: list[CalibrationSampleRequest] = Field(min_length=1, max_length=1000)

    @field_validator("reviewer_ids")
    @classmethod
    def reviewer_ids_are_valid(cls, value: list[str]) -> list[str]:
        if any(not reviewer_id or len(reviewer_id) > 64 for reviewer_id in value):
            raise ValueError("reviewer_ids must contain valid user ids")
        return value

    @model_validator(mode="after")
    def participants_and_samples_are_unambiguous(self) -> CalibrationRoundCreateRequest:
        if len(set(self.reviewer_ids)) != 2:
            raise ValueError("reviewer_ids must contain two distinct users")
        if self.adjudicator_id in self.reviewer_ids:
            raise ValueError("adjudicator_id must be different from both reviewers")
        case_ids = [sample.source_case_id for sample in self.samples]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("source_case_id must be unique within a calibration round")
        return self


class CalibrationSubmissionRequest(StrictCalibrationModel):
    value: CalibrationDecisionValue = Field(validation_alias=AliasChoices("value", "value_json"))
    expected_resource_version: StrictInt | None = Field(default=None, ge=1)


class CalibrationAdjudicationClaimRequest(StrictCalibrationModel):
    expected_resource_version: StrictInt | None = Field(default=None, ge=1)


class CalibrationAdjudicationRequest(StrictCalibrationModel):
    decision: Literal["accept_a", "accept_b", "revise", "exclude"]
    reason: StrictStr = Field(min_length=1, max_length=2000)
    value: CalibrationDecisionValue | None = Field(
        default=None,
        validation_alias=AliasChoices("value", "value_json"),
    )
    expected_resource_version: StrictInt | None = Field(default=None, ge=1)

    @field_validator("reason")
    @classmethod
    def reason_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def value_matches_decision(self) -> CalibrationAdjudicationRequest:
        if self.decision == "revise" and self.value is None:
            raise ValueError("revise adjudication requires value")
        if self.decision != "revise" and self.value is not None:
            raise ValueError("value is only accepted for revise adjudication")
        return self


class CalibrationGoldReleaseRequest(StrictCalibrationModel):
    gold_set_key: StrictStr = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    expected_resource_version: StrictInt | None = Field(default=None, ge=1)


class CalibrationItemDTO(BaseModel):
    item_id: str
    ordinal: int
    source_case_id: str
    evidence_ref: str
    status: str
    review_outcome: str
    resource_version: int
    trace_id: str


class CalibrationRoundDTO(BaseModel):
    id: str
    round_id: str
    dataset_id: str
    dataset_version: str
    label_version: str
    rubric_version: str
    sample_manifest_sha256: str
    status: str
    sealed: bool
    my_role: str
    resource_version: int
    sample_count: int
    paired_submission_count: int | None = None
    agreed_count: int | None = None
    conflict_count: int | None = None
    adjudication_count: int | None = None
    excluded_count: int | None = None
    observed_agreement_ppm: int | None = None
    cohen_kappa_micros: int | None = None
    cohen_kappa_defined: bool | None = None
    root_trace_id: str
    current_trace_id: str
    items: list[CalibrationItemDTO] | None = None
    created_at: str | None = None
    updated_at: str | None = None
    published_at: str | None = None


class CalibrationAssignmentDTO(BaseModel):
    assignment_id: str
    round_id: str
    item_id: str
    review_task_id: str
    slot: Literal["A", "B"]
    ordinal: int
    source_case_id: str
    evidence_ref: str
    status: str
    resource_version: int
    trace_id: str


class CalibrationConflictSubmissionDTO(BaseModel):
    submission_id: str
    slot: Literal["A", "B"]
    value: Any
    submitted_at: str


class CalibrationConflictDTO(BaseModel):
    item_id: str
    round_id: str
    ordinal: int
    source_case_id: str
    evidence_ref: str
    status: str
    review_outcome: str
    resource_version: int
    adjudication_claimed: bool
    submissions: list[CalibrationConflictSubmissionDTO]


__all__ = [
    "CalibrationDecisionValue",
    "CalibrationAdjudicationClaimRequest",
    "CalibrationAdjudicationRequest",
    "CalibrationAssignmentDTO",
    "CalibrationConflictDTO",
    "CalibrationConflictSubmissionDTO",
    "CalibrationGoldReleaseRequest",
    "CalibrationItemDTO",
    "CalibrationRoundCreateRequest",
    "CalibrationRoundDTO",
    "CalibrationSampleRequest",
    "CalibrationSubmissionRequest",
]
