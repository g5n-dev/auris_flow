from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class StrictManualLabelModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ManualLabelEvidenceRef(StrictManualLabelModel):
    type: str = Field(min_length=1, max_length=64)
    id: str = Field(min_length=1, max_length=256)
    sha256: str = Field(pattern=SHA256_PATTERN)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def interval_is_valid(self) -> ManualLabelEvidenceRef:
        if self.start_ms is not None and self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class ManualLabelDraftCreateRequest(StrictManualLabelModel):
    annotation_kind: Literal["label-fact-draft"] = "label-fact-draft"
    annotation_id: str = Field(pattern=ID_PATTERN)
    label_version_id: str = Field(pattern=ID_PATTERN)
    label_id: str = Field(min_length=1, max_length=128)
    subject_scope: str = Field(min_length=1, max_length=64)
    subject_key: str = Field(min_length=1, max_length=256)
    event_or_segment_id: str = Field(min_length=1, max_length=256)
    assertion_slot: str = Field(default="canonical", min_length=1, max_length=128)
    occurred_at: datetime = Field(strict=False)
    evidence_ref: ManualLabelEvidenceRef
    value_type: Literal[
        "boolean",
        "categorical",
        "multi",
        "numeric",
        "temporal",
        "hierarchical",
    ]
    value: Any
    environment: Literal["production"] = "production"
    expected_release_head_generation: int = Field(ge=1)

    @field_validator(
        "label_id",
        "subject_scope",
        "subject_key",
        "event_or_segment_id",
        "assertion_slot",
    )
    @classmethod
    def required_text_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("required text must not be blank")
        return normalized

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)


class ManualLabelDraftSubmitRequest(StrictManualLabelModel):
    expected_draft_sha256: str = Field(pattern=SHA256_PATTERN)
    expected_release_head_generation: int = Field(ge=1)
    confirmation: Literal["submit-frozen-manual-label"]


class ManualLabelDraftRebaseRequest(StrictManualLabelModel):
    action: Literal["preview", "confirm"]
    mapping_bundle_id: str = Field(pattern=ID_PATTERN)
    target_label_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_release_head_generation: int = Field(ge=1)
    new_annotation_id: str | None = Field(default=None, pattern=ID_PATTERN)
    preview_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    confirmation: Literal["confirm-reviewed-manual-label-rebase"] | None = None

    @model_validator(mode="after")
    def confirmation_fields_match_action(self) -> ManualLabelDraftRebaseRequest:
        if self.action == "preview":
            if (
                self.new_annotation_id is not None
                or self.preview_sha256 is not None
                or self.confirmation is not None
            ):
                raise ValueError("preview cannot include confirmation fields")
            return self
        if (
            self.new_annotation_id is None
            or self.preview_sha256 is None
            or self.confirmation != "confirm-reviewed-manual-label-rebase"
        ):
            raise ValueError("confirm requires new_annotation_id, preview_sha256 and confirmation")
        return self
