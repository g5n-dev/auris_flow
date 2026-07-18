from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.json_keys import build_json_key_aliases, json_key_fingerprint
from app.schemas.common import FlexiblePayload

AudioCapability = Literal["vad", "asr", "diarization", "voiceprint", "quality"]
ObjectStorageProvider = Literal["minio", "s3", "obs", "oss"]
COMPLETION_REFERENCE_PAIRS = (
    ("ref_type", "ref_id"),
    ("resource_type", "resource_id"),
    ("aggregate_type", "aggregate_id"),
    ("object_type", "object_id"),
    ("subject_type", "subject_id"),
)
COMPLETION_REFERENCE_KEYS = frozenset(
    field for pair in COMPLETION_REFERENCE_PAIRS for field in pair
)
COMPLETION_REFERENCE_KEY_ALIASES = build_json_key_aliases(COMPLETION_REFERENCE_KEYS)


def default_audio_capabilities() -> list[AudioCapability]:
    return ["vad", "asr", "diarization", "voiceprint", "quality"]


def _contains_legacy_hotword_ref(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key in {"hotwords_ref", "legacy_hotwords_ref"} or _contains_legacy_hotword_ref(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_legacy_hotword_ref(nested) for nested in value)
    return False


def _normalize_completion_reference_field(field: str) -> str:
    return COMPLETION_REFERENCE_KEY_ALIASES.get(json_key_fingerprint(field), field)


def _normalize_completion_references(value: Any, *, depth: int = 0) -> Any:
    if depth > 16:
        raise ValueError("result_ref nesting exceeds 16 levels")
    if isinstance(value, list):
        return [_normalize_completion_references(item, depth=depth + 1) for item in value]
    if not isinstance(value, dict):
        return value

    normalized: dict[str, Any] = {}
    for field, item in value.items():
        if not isinstance(field, str):
            raise ValueError("result_ref fields must be strings")
        canonical = _normalize_completion_reference_field(field)
        if canonical in normalized:
            raise ValueError(f"duplicate completion reference field: {canonical}")
        normalized[canonical] = _normalize_completion_references(item, depth=depth + 1)

    for type_field, id_field in COMPLETION_REFERENCE_PAIRS:
        has_type = type_field in normalized
        has_id = id_field in normalized
        if has_type != has_id:
            raise ValueError(
                f"typed completion references require both {type_field} and {id_field}"
            )
        if has_type and (
            not isinstance(normalized[type_field], str)
            or not normalized[type_field].strip()
            or not isinstance(normalized[id_field], str)
            or not normalized[id_field].strip()
        ):
            raise ValueError(f"{type_field} and {id_field} must be non-blank strings")
    return normalized


class TaskVersionRequest(FlexiblePayload):
    task_version_id: str | None = None
    task_type_id: str | None = None
    version: str | None = None
    canvas_variant: str | None = None
    label_version: str | None = None


class TaskRunRequest(FlexiblePayload):
    task_version_id: str
    trigger_type: str = "manual"
    execution_mode: Literal["production", "diagnostic", "shadow", "experiment"] = "production"
    partition_key: str | None = None
    run_key: str | None = None
    experiment_id: str | None = None
    experiment_subject_key: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def experiment_binding_is_complete(self) -> TaskRunRequest:
        if bool(self.experiment_id) != bool(self.experiment_subject_key):
            raise ValueError("experiment_id and experiment_subject_key must be provided together")
        if self.experiment_id and self.execution_mode != "experiment":
            raise ValueError("experiment-bound task runs must use execution_mode=experiment")
        return self


class TaskRunRetryRequest(FlexiblePayload):
    reason: str | None = Field(default=None, max_length=500)
    payload_overrides: dict[str, Any] = Field(default_factory=dict)


class RunReleaseDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class RunCompletionReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "failed"] = "success"
    adapter: Literal["dagster", "object_storage", "external_callback"] | None = None
    completion_receipt_id: str | None = Field(default=None, max_length=128)
    source: str | None = Field(default=None, max_length=128)
    external_id: str | None = Field(default=None, max_length=256)
    result_ref: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=1000)
    error_code: str | None = Field(default=None, max_length=128)
    retryable: bool = True

    @field_validator("completion_receipt_id")
    @classmethod
    def receipt_id_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("completion_receipt_id must not be blank")
        return value

    @field_validator("result_ref", mode="before")
    @classmethod
    def result_references_are_canonical(cls, value: Any) -> Any:
        return _normalize_completion_references(value)


HumanReviewDecision = Literal[
    "accepted",
    "approved",
    "confirm",
    "modified",
    "rejected",
    "blocked",
    "escalate",
    "escalated",
]


HumanReviewTargetType = Literal[
    "label_candidate",
    "label_aggregate",
    "prompt_version_candidate",
    "taxonomy_suggestion",
    "event_link",
    "evidence_pack",
    "conversation_boundary",
    "voiceprint_sample",
    "work_item",
]


class HumanReviewTargetChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: HumanReviewTargetType
    target_id: str = Field(min_length=1, max_length=256)
    fields: dict[str, Any] = Field(min_length=1, max_length=50)

    @field_validator("fields")
    @classmethod
    def fields_must_not_override_server_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        protected = {
            "id",
            "tenant_id",
            "project_id",
            "trace_id",
            "status",
            "resource_version",
            "decision",
            "decided_by",
            "review_task_id",
            "review_decision_id",
        }
        attempted = sorted(protected.intersection(value))
        if attempted:
            raise ValueError(f"server-managed fields cannot be modified: {', '.join(attempted)}")
        return value


class HumanReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: HumanReviewDecision = "accepted"
    note: str | None = Field(default=None, max_length=1000)
    reason: str | None = Field(default=None, max_length=1000)
    changes: list[HumanReviewTargetChange] = Field(default_factory=list, max_length=20)


QualityAppealDecision = Literal[
    "original_upheld",
    "original_overturned",
    "original_remanded",
]


class QualityAppealCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_decision_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[str] = Field(min_length=1, max_length=50)

    @field_validator("source_decision_id", "reason")
    @classmethod
    def required_text_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_must_be_distinct_and_non_blank(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item or len(item) > 512 for item in normalized):
            raise ValueError("evidence refs must contain 1 to 512 characters")
        if len(set(normalized)) != len(normalized):
            raise ValueError("evidence refs must be distinct")
        return normalized


class QualityAppealClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_resource_version: int = Field(ge=1)


class QualityAppealDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: QualityAppealDecision
    reason: str = Field(min_length=1, max_length=2000)
    expected_resource_version: int = Field(ge=1)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class QualityAppealWithdrawalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)
    expected_resource_version: int = Field(ge=1)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class LabelVersionRequest(FlexiblePayload):
    label_version_id: str | None = None
    base_version: str | None = None
    changeset: list[dict] = Field(default_factory=list)
    status: Literal["draft"] = "draft"


class LabelOptimizationBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_rounds: int = Field(default=3, ge=1, le=3)
    candidates_per_round: int = Field(default=3, ge=2, le=5)
    max_duration_minutes: int = Field(default=120, ge=1, le=120)
    max_cost_micros: int | None = Field(default=None, gt=0)
    min_macro_f1_gain_ppm: int = Field(default=20_000, ge=0, le=1_000_000)
    max_critical_recall_regression_ppm: int = Field(default=5_000, ge=0, le=1_000_000)


class LabelOptimizationTriggerReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["manual", "threshold", "daily-incremental", "weekly-full", "feedback"]
    reason_codes: list[str] = Field(min_length=1, max_length=20)
    source_feedback_ids: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("reason_codes")
    @classmethod
    def reasons_are_unique_and_non_blank(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("reason_codes must not contain blanks")
        if len(set(normalized)) != len(normalized):
            raise ValueError("reason_codes must be unique")
        return normalized


class LabelOptimizationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    optimization_run_id: str | None = Field(default=None, min_length=3, max_length=128)
    label_version_id: str = Field(min_length=3, max_length=128)
    prompt_version_id: str = Field(min_length=3, max_length=128)
    model_version: str = Field(min_length=1, max_length=128)
    aggregation_policy_version_id: str = Field(min_length=3, max_length=128)
    eval_dataset_version_id: str = Field(min_length=3, max_length=128)
    trigger_reason: LabelOptimizationTriggerReasonRequest
    budget: LabelOptimizationBudgetRequest = Field(default_factory=LabelOptimizationBudgetRequest)
    sample_set: str | None = Field(default=None, max_length=256)
    partition_key: str | None = Field(default=None, max_length=512)
    source: str | None = Field(default=None, max_length=128)


class KnowledgeBuildRequest(FlexiblePayload):
    reason: str | None = Field(default=None, max_length=500)
    chunk_policy: str | None = None


class KnowledgeRecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)
    scope: dict[str, Any] = Field(default_factory=dict)

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class AudioIntelligenceRunRequest(FlexiblePayload):
    capabilities: list[AudioCapability] = Field(
        default_factory=default_audio_capabilities,
        min_length=1,
        max_length=5,
    )
    recording_id: str | None = Field(default=None, max_length=128)
    provider: str | None = Field(default="audio_intelligence_default", max_length=128)
    model_version: str | None = Field(default="audio-v2.3.1", max_length=128)
    execution_mode: Literal["production", "diagnostic", "shadow"] = "production"
    language: str = Field(default="zh-CN", min_length=2, max_length=32)
    hotword_pack_version_id: str | None = Field(default=None, min_length=3, max_length=128)
    task_version_id: str | None = Field(default=None, min_length=3, max_length=128)
    return_word_timestamps: bool = False
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def legacy_hotword_ref_is_read_only(self) -> AudioIntelligenceRunRequest:
        if _contains_legacy_hotword_ref(self.model_dump()):
            raise ValueError(
                "hotwords_ref is read-only; use hotword_pack_version_id for new requests"
            )
        return self


class AudioRecordingObjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_object_id: str = Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    provider: ObjectStorageProvider
    bucket: str = Field(min_length=3, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]+$")
    object_key: str = Field(min_length=1, max_length=1024)
    content_type: Literal["audio/wav", "audio/x-wav"] = "audio/wav"
    content_length: int = Field(ge=44, le=5 * 1024**4)
    checksum_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    etag: str | None = Field(default=None, max_length=255)

    @field_validator("object_key")
    @classmethod
    def object_key_must_be_safe(cls, value: str) -> str:
        normalized = value.strip().lstrip("/")
        parts = normalized.split("/")
        if (
            not normalized
            or "\\" in normalized
            or any(part in {"", ".", ".."} for part in parts)
            or any(ord(character) < 32 for character in normalized)
        ):
            raise ValueError("object_key must be a normalized object-storage key")
        return normalized

    @field_validator("checksum_sha256")
    @classmethod
    def checksum_must_be_lowercase(cls, value: str) -> str:
        return value.lower()


EvalSuite = Literal["golden", "boundary", "adversarial", "fresh", "canary", "regression"]


class EvalRunRequest(FlexiblePayload):
    """Evaluation request with an explicit strong binding for labeling runs.

    ``dataset_id`` and ``label_version`` remain compatibility aliases for the
    older generic evaluator. New label evaluations must set
    ``capability=labeling`` and lock every artifact that can change semantics.
    """

    dataset_id: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("dataset_id", "eval_dataset_version_id"),
    )
    eval_dataset_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    capability: Literal["generic", "labeling"] | None = None
    model_version: str | None = Field(default=None, min_length=1, max_length=128)
    label_version: str | None = Field(default=None, max_length=128)
    label_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    prompt_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    aggregation_policy_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    optimization_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    evaluation_suites: list[EvalSuite] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def validate_version_binding(self) -> EvalRunRequest:
        canonical_dataset_id = self.eval_dataset_version_id or self.dataset_id
        if (
            self.eval_dataset_version_id is not None
            and self.eval_dataset_version_id != self.dataset_id
        ):
            raise ValueError("dataset_id and eval_dataset_version_id must identify one version")
        if self.capability != "labeling":
            return self
        if self.eval_dataset_version_id is None:
            self.eval_dataset_version_id = canonical_dataset_id
        required = {
            "label_version_id": self.label_version_id,
            "prompt_version_id": self.prompt_version_id,
            "model_version": self.model_version,
            "aggregation_policy_version_id": self.aggregation_policy_version_id,
            "optimization_run_id": self.optimization_run_id,
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise ValueError("labeling eval requires locked fields: " + ", ".join(missing))
        required_suite_order: list[EvalSuite] = [
            "golden",
            "boundary",
            "adversarial",
            "fresh",
            "canary",
            "regression",
        ]
        required_suites = set(required_suite_order)
        if not self.evaluation_suites:
            self.evaluation_suites = required_suite_order
        elif set(self.evaluation_suites) != required_suites:
            raise ValueError(
                "labeling eval must cover golden, boundary, adversarial, fresh, "
                "canary and regression suites"
            )
        return self


class EvalFeedbackTaskRequest(FlexiblePayload):
    badcase_refs: list[str] = Field(min_length=1, max_length=50)
    target: str = Field(max_length=256)
    reason: str | None = Field(default=None, max_length=500)
    prompt_version: str | None = Field(default=None, max_length=128)
    candidate_version: str | None = Field(default=None, max_length=128)


class ExternalCallbackRequest(FlexiblePayload):
    target: str
    payload_template: dict | None = None

    @field_validator("target")
    @classmethod
    def target_must_be_named(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("target is required")
        return value
