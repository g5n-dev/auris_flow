from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.schemas.common import ApiMeta

IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$"
NAMESPACE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$"
ENVIRONMENT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,31}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class StrictLabelFactSetModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LabelFactSetCreateRequest(StrictLabelFactSetModel):
    fact_namespace: StrictStr = Field(
        min_length=2,
        max_length=128,
        pattern=NAMESPACE_PATTERN,
    )
    target_label_version_id: StrictStr = Field(
        min_length=2,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    fact_as_of: datetime = Field(strict=False)
    partition_manifest: dict[StrictStr, JsonValue] = Field(min_length=1)
    partition_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    source_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    result_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    row_count: StrictInt = Field(ge=0)

    @field_validator("fact_as_of")
    @classmethod
    def fact_as_of_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fact_as_of must include a timezone")
        return value.astimezone(UTC)


class LabelFactSetValidateRequest(StrictLabelFactSetModel):
    expected_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)


class LabelFactSetApproveRequest(LabelFactSetValidateRequest):
    approval_id: StrictStr = Field(
        min_length=2,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    reason: StrictStr = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class LabelFactSetPromoteRequest(StrictLabelFactSetModel):
    environment: StrictStr = Field(
        min_length=2,
        max_length=32,
        pattern=ENVIRONMENT_PATTERN,
    )
    action: Literal["bootstrap", "promote", "rollback"]
    expected_generation: StrictInt = Field(ge=0)
    expected_current_fact_set_id: StrictStr | None = Field(
        default=None,
        min_length=2,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    expected_current_manifest_sha256: StrictStr | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
    )

    @model_validator(mode="after")
    def expected_head_anchor_matches_action(self) -> LabelFactSetPromoteRequest:
        current_pair = (
            self.expected_current_fact_set_id,
            self.expected_current_manifest_sha256,
        )
        if self.action == "bootstrap":
            if self.expected_generation != 0 or current_pair != (None, None):
                raise ValueError(
                    "bootstrap requires expected_generation=0 and no current head anchor"
                )
            return self
        if self.expected_generation < 1 or None in current_pair:
            raise ValueError(
                "promote and rollback require a positive generation and complete current anchor"
            )
        return self


class LabelFactSetPublishPromoteRequest(LabelFactSetPromoteRequest):
    """First publication or forward promotion of an approved FactSet."""

    action: Literal["bootstrap", "promote"]


class LabelFactSetRollbackRequest(LabelFactSetPromoteRequest):
    """Compensating Head move to the exact frozen previous FactSet."""

    action: Literal["rollback"] = "rollback"


class LabelFactSetMutationResponse(StrictLabelFactSetModel):
    fact_set_id: StrictStr
    fact_namespace: StrictStr
    target_label_version_id: StrictStr
    status: Literal["candidate", "validated", "approved", "published"]
    manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    row_count: StrictInt = Field(ge=0)
    audit_id: StrictInt = Field(ge=1)
    outbox_event_id: StrictInt = Field(ge=1)
    trace_id: StrictStr


class LabelFactSetPromotionResponse(StrictLabelFactSetModel):
    fact_set_head_id: StrictStr
    head_event_id: StrictStr
    environment: StrictStr
    fact_namespace: StrictStr
    action: Literal["bootstrap", "promote", "rollback"]
    generation: StrictInt = Field(ge=1)
    previous_generation: StrictInt | None = Field(default=None, ge=1)
    current_fact_set_id: StrictStr
    current_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    previous_fact_set_id: StrictStr | None = None
    previous_manifest_sha256: StrictStr | None = Field(default=None, pattern=SHA256_PATTERN)
    head_event_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    audit_id: StrictInt = Field(ge=1)
    outbox_event_id: StrictInt = Field(ge=1)
    trace_id: StrictStr


class LabelFactSetMutationEnvelope(StrictLabelFactSetModel):
    data: LabelFactSetMutationResponse
    meta: ApiMeta


class LabelFactSetPromotionEnvelope(StrictLabelFactSetModel):
    data: LabelFactSetPromotionResponse
    meta: ApiMeta


class LabelFactSetHeadChainVerification(StrictLabelFactSetModel):
    event_count: StrictInt = Field(ge=1)
    generation: StrictInt = Field(ge=1)
    head_event_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    head_id: StrictStr
