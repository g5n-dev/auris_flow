from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictLabelFactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LabelFactRevisionCreate(StrictLabelFactModel):
    aggregate_id: str = Field(min_length=1, max_length=128)
    source_kind: Literal["aggregate", "human-decision"]
    human_review_decision_id: str | None = Field(default=None, min_length=1, max_length=128)
    fact_set_id: str | None = Field(default=None, min_length=1, max_length=128)
    fact_namespace: str = Field(min_length=1, max_length=128)
    subject_scope: str = Field(min_length=1, max_length=64)
    subject_key: str = Field(min_length=1, max_length=256)
    event_or_segment_id: str = Field(min_length=1, max_length=256)
    assertion_slot: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    occurred_at_origin: Literal["source", "legacy-recorded-fallback", "authorized-backfill"]
    label_version_id: str = Field(min_length=1, max_length=128)
    label_id: str = Field(min_length=1, max_length=128)
    value_type: Literal[
        "boolean",
        "categorical",
        "multi",
        "numeric",
        "temporal",
        "hierarchical",
    ]
    value: Any
    authority: Literal["l2-auto-accepted", "human-confirmed"]
    expected_head_generation: int = Field(ge=0)

    @field_validator(
        "aggregate_id",
        "fact_namespace",
        "subject_scope",
        "subject_key",
        "event_or_segment_id",
        "assertion_slot",
        "label_version_id",
        "label_id",
    )
    @classmethod
    def required_text_is_normalized(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def source_union_is_exact(self) -> LabelFactRevisionCreate:
        if self.source_kind == "aggregate" and self.human_review_decision_id is not None:
            raise ValueError("aggregate source cannot bind human_review_decision_id")
        if self.source_kind == "human-decision" and self.human_review_decision_id is None:
            raise ValueError("human-decision source requires human_review_decision_id")
        if self.source_kind == "aggregate" and self.authority != "l2-auto-accepted":
            raise ValueError("aggregate source requires l2-auto-accepted authority")
        if self.source_kind == "human-decision" and self.authority != "human-confirmed":
            raise ValueError("human-decision source requires human-confirmed authority")
        return self


class LabelFactAsOfRequest(StrictLabelFactModel):
    fact_namespace: str = Field(min_length=1, max_length=128)
    fact_as_of: datetime
    occurred_from: datetime
    occurred_to: datetime
    label_version_ids: list[str] = Field(min_length=1, max_length=100)
    label_ids: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator("fact_namespace")
    @classmethod
    def namespace_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("fact_namespace must not be blank")
        return normalized

    @field_validator("fact_as_of", "occurred_from", "occurred_to")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fact cutoffs must include a timezone")
        return value.astimezone(UTC)

    @field_validator("label_version_ids", "label_ids")
    @classmethod
    def identifiers_are_unique(cls, values: list[str]) -> list[str]:
        normalized = [item.strip() for item in values]
        if any(not item for item in normalized):
            raise ValueError("identifier lists must not contain blanks")
        if len(normalized) != len(set(normalized)):
            raise ValueError("identifier lists must be unique")
        return normalized

    @model_validator(mode="after")
    def business_window_is_half_open(self) -> LabelFactAsOfRequest:
        if self.occurred_to <= self.occurred_from:
            raise ValueError("occurred_to must be greater than occurred_from")
        return self
