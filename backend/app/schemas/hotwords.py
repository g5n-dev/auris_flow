from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HotwordErrorType = Literal[
    "missing_term",
    "misrecognition",
    "alias_gap",
    "weight_issue",
    "false_boost",
]
HotwordVersionStatus = Literal[
    "draft",
    "validating",
    "ready_for_eval",
    "evaluating",
    "gate_blocked",
    "review_required",
    "approved",
    "published",
    "deprecated",
    "rolled_back",
    "archived",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HotwordPackCreateRequest(StrictModel):
    pack_id: str | None = Field(default=None, min_length=3, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    language: str = Field(default="zh-CN", min_length=2, max_length=32)
    domain: str = Field(min_length=1, max_length=128)

    @field_validator("name", "language", "domain")
    @classmethod
    def strip_non_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


class HotwordPackVersionCreateRequest(StrictModel):
    version_id: str | None = Field(default=None, min_length=3, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    baseline_version_id: str | None = Field(default=None, max_length=128)
    task_type_id: str | None = Field(default=None, min_length=1, max_length=128)
    manifest_storage_object_id: str | None = Field(default=None, max_length=128)

    @field_validator("version", "task_type_id")
    @classmethod
    def version_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


class HotwordPackVersionPatchRequest(StrictModel):
    expected_resource_version: int = Field(ge=1)
    status: HotwordVersionStatus | None = None
    eval_run_id: str | None = Field(default=None, max_length=128)
    provider: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def requires_change(self) -> HotwordPackVersionPatchRequest:
        if not any(
            value is not None
            for value in (
                self.status,
                self.eval_run_id,
                self.provider,
            )
        ):
            raise ValueError("at least one mutable field is required")
        return self


class HotwordItemCreateRequest(StrictModel):
    item_id: str | None = Field(default=None, min_length=3, max_length=128)
    canonical_term: str = Field(min_length=1, max_length=255)
    aliases: list[str] = Field(default_factory=list, max_length=32)
    category: str = Field(min_length=1, max_length=64)
    weight: int = Field(ge=0, le=100)
    source_badcase_id: str | None = Field(default=None, max_length=128)
    source_type: Literal["manual", "badcase", "knowledge_candidate"] = "manual"

    @field_validator("canonical_term", "category")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("aliases")
    @classmethod
    def aliases_are_explicit_and_unique(cls, value: list[str]) -> list[str]:
        normalized = [alias.strip() for alias in value]
        if any(not alias for alias in normalized):
            raise ValueError("aliases must not contain blank values")
        if len(set(normalized)) != len(normalized):
            raise ValueError("aliases must be unique")
        return normalized

    @model_validator(mode="after")
    def source_reference_is_consistent(self) -> HotwordItemCreateRequest:
        if self.source_badcase_id and "source_type" not in self.model_fields_set:
            self.source_type = "badcase"
        if self.source_type == "badcase" and not self.source_badcase_id:
            raise ValueError("source_badcase_id is required for badcase source")
        if self.source_type != "badcase" and self.source_badcase_id is not None:
            raise ValueError("source_badcase_id is only valid for badcase source")
        return self


class HotwordItemPatchRequest(StrictModel):
    expected_resource_version: int = Field(ge=1)
    canonical_term: str | None = Field(default=None, min_length=1, max_length=255)
    aliases: list[str] | None = Field(default=None, max_length=32)
    category: str | None = Field(default=None, min_length=1, max_length=64)
    weight: int | None = Field(default=None, ge=0, le=100)
    source_badcase_id: str | None = Field(default=None, max_length=128)
    source_type: Literal["manual", "badcase", "knowledge_candidate"] | None = None


class HotwordBadcaseCreateRequest(StrictModel):
    badcase_id: str | None = Field(default=None, min_length=3, max_length=128)
    capability: Literal["asr-hotword"] = "asr-hotword"
    standard_term: str = Field(min_length=1, max_length=255)
    recognized_text: str = Field(min_length=0, max_length=1000)
    error_type: HotwordErrorType
    evidence_storage_object_id: str = Field(min_length=1, max_length=128)
    evidence_ref: str | None = Field(default=None, min_length=1, max_length=1024)
    evidence_level: Literal["gold", "human-confirmed", "business-master", "discovery"]
    expected_count: int = Field(default=0, ge=0)
    correct_count: int = Field(default=0, ge=0)
    weighted_error_count: float = Field(default=0, ge=0)
    manual_correction_count: int = Field(default=0, ge=0)
    business_weight: float = Field(default=1.0, ge=0, le=2)
    downstream_impact: dict[str, object] = Field(default_factory=dict)
    root_cause: str | None = Field(default=None, max_length=64)
    fix_suggestion: str | None = Field(default=None, max_length=1000)
    hotword_pack_version_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def counts_are_consistent(self) -> HotwordBadcaseCreateRequest:
        if self.correct_count > self.expected_count:
            raise ValueError("correct_count cannot exceed expected_count")
        if self.error_type != "missing_term" and not self.recognized_text.strip():
            raise ValueError("recognized_text is required unless error_type is missing_term")
        return self


class HotwordBadcasePatchRequest(StrictModel):
    expected_resource_version: int = Field(ge=1)
    status: (
        Literal[
            "pending-attribution",
            "pending-review",
            "in-regression",
        ]
        | None
    ) = None
    root_cause: str | None = Field(default=None, max_length=64)
    fix_suggestion: str | None = Field(default=None, max_length=1000)
    downstream_impact: dict[str, object] | None = None


class HotwordBadcaseDecisionRequest(StrictModel):
    decision: Literal["confirmed", "rejected", "needs-evidence"]
    reason: str = Field(min_length=1, max_length=1000)
    expected_resource_version: int = Field(ge=1)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class AsrTranscriptCorrectionRequest(StrictModel):
    annotation_id: str = Field(min_length=3, max_length=128)
    annotation_kind: Literal["asr-transcript-correction"]
    confirmation: Literal["record_correction"]
    track: Literal["asr"]
    audio_session_id: str | None = Field(default=None, min_length=1, max_length=128)
    recognized_text: str = Field(min_length=0, max_length=1000)
    corrected_text: str = Field(min_length=0, max_length=1000)
    error_type: HotwordErrorType
    evidence_window: str = Field(min_length=1, max_length=128)
    evidence_storage_object_id: str = Field(min_length=1, max_length=128)
    hotword_pack_version_id: str = Field(min_length=1, max_length=128)
    source_badcase_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_asr_segment_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator(
        "annotation_id",
        "evidence_window",
        "evidence_storage_object_id",
        "hotword_pack_version_id",
        "source_badcase_id",
        "source_asr_segment_id",
    )
    @classmethod
    def trim_non_blank_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("recognized_text", "corrected_text")
    @classmethod
    def trim_correction_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def text_shape_matches_error_type(self) -> AsrTranscriptCorrectionRequest:
        has_recognized = bool(self.recognized_text)
        has_corrected = bool(self.corrected_text)
        if self.error_type == "missing_term":
            if has_recognized or not has_corrected:
                raise ValueError("missing_term requires blank recognized_text and corrected_text")
        elif self.error_type == "false_boost":
            if not has_recognized or has_corrected:
                raise ValueError("false_boost requires recognized_text and blank corrected_text")
        elif not has_recognized or not has_corrected:
            raise ValueError("recognized_text and corrected_text are required for this error_type")
        return self


class HotwordAnalysisRunRequest(StrictModel):
    date_from: str | None = Field(default=None, max_length=10)
    date_to: str | None = Field(default=None, max_length=10)
    store_id: str | None = Field(default=None, max_length=128)
    provider: str | None = Field(default=None, max_length=128)
    model_version: str | None = Field(default=None, max_length=128)
    hotword_pack_version_id: str | None = Field(default=None, max_length=128)


class HotwordEvalMetrics(StrictModel):
    trusted_occurrences: int | None = Field(default=None, ge=0)
    unique_terms: int | None = Field(default=None, ge=0)
    error_rate: float = Field(ge=0, le=1)
    recall_rate: float = Field(ge=0, le=1)
    false_boost_rate: float = Field(ge=0, le=1)
    cer: float = Field(ge=0, le=1)
    wer: float = Field(ge=0, le=1)
    downstream_f1: float = Field(ge=0, le=1)
    p95_latency_ms: float = Field(gt=0)
    cost_per_minute: float = Field(gt=0)


class HotwordEvalRunRequest(StrictModel):
    eval_dataset_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(default="auris-audio-stack", min_length=1, max_length=128)
    expected_resource_version: int = Field(ge=1)


class HotwordPublishRequest(StrictModel):
    expected_resource_version: int = Field(ge=1)
    eval_run_id: str = Field(min_length=1, max_length=128)
    confirmation: Literal["publish"]


class HotwordRollbackRequest(StrictModel):
    target_version_id: str = Field(min_length=1, max_length=128)
    expected_resource_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("target_version_id", "reason")
    @classmethod
    def rollback_text_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized
