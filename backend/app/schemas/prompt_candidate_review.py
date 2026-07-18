from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PromptReviewDecision = Literal["accepted", "modified", "rejected"]
PROMPT_MUTABLE_FIELDS = frozenset({"template", "output_schema", "generation_params"})


class PromptFieldChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before: Any = None
    after: Any
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class PromptReviewSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: PromptReviewDecision
    note: str | None = Field(default=None, max_length=1000)
    field_diff: dict[str, PromptFieldChange] = Field(default_factory=dict, max_length=3)

    @field_validator("field_diff")
    @classmethod
    def only_prompt_content_fields_can_be_changed(
        cls, value: dict[str, PromptFieldChange]
    ) -> dict[str, PromptFieldChange]:
        invalid = sorted(set(value) - PROMPT_MUTABLE_FIELDS)
        if invalid:
            raise ValueError(
                "field_diff only accepts template, output_schema and generation_params"
            )
        return value

    @model_validator(mode="after")
    def decision_and_diff_are_consistent(self) -> PromptReviewSubmissionRequest:
        if self.decision == "modified" and not self.field_diff:
            raise ValueError("modified review requires field_diff")
        if self.decision != "modified" and self.field_diff:
            raise ValueError("field_diff is only allowed for modified review")
        return self


class PromptReviewAdjudicationRequest(PromptReviewSubmissionRequest):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def adjudication_reason_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized
