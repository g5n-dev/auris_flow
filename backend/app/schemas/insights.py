from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.label_metric_scopes import (
    LabelMetricRunScopeRequest,
    MetricResultReasonCode,
    MetricResultStatus,
    validate_result_reason_semantics,
)

InsightReportType = Literal[
    "business",
    "store",
    "sales",
    "tags",
    "quality",
    "reports",
    "daily",
    "weekly",
    "management_summary",
    "custom",
]
InsightReportArtifactContentType = Literal["application/json"]
InsightReportDocumentSchemaVersion = Literal["auris.insight-report.v2"]
InsightActionType = Literal[
    "create_training_action",
    "create_experiment",
    "create_review_action",
    "create_operation_action",
]
InsightActionBranch = Literal["auto", "experiment", "human_review"]
InsightRiskLevel = Literal["low", "medium", "high", "critical"]


class InsightMetricRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_keys: list[str] = Field(min_length=1, max_length=20)
    time_range: str = Field(min_length=1, max_length=512)
    store_ids: list[str] = Field(default_factory=list, max_length=50)
    model_version: str | None = Field(default=None, max_length=128)
    label_version: str | None = Field(default=None, max_length=128)
    label_scope: LabelMetricRunScopeRequest | None = None
    source: str = Field(default="ui", min_length=1, max_length=128)

    @field_validator("metric_keys", "store_ids")
    @classmethod
    def unique_non_blank_values(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for item in value:
            normalized = item.strip()
            if not normalized:
                raise ValueError("values must not contain blank entries")
            if normalized not in result:
                result.append(normalized)
        return result

    @field_validator("time_range")
    @classmethod
    def non_blank_time_range(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("time_range must not be blank")
        return normalized


class InsightMetricAggregationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_key: str = Field(min_length=1, max_length=128)
    value: float | None
    unit: str = Field(min_length=1, max_length=64)
    sample_size: int = Field(ge=0)
    result_status: MetricResultStatus = "value"
    reason_codes: list[MetricResultReasonCode] = Field(default_factory=list, max_length=64)

    @field_validator("metric_key", "unit")
    @classmethod
    def non_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("reason_codes")
    @classmethod
    def unique_reasons(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("reason_codes must be unique and non-blank")
        return normalized

    @model_validator(mode="after")
    def governed_result_outcome(self) -> InsightMetricAggregationResult:
        validate_result_reason_semantics(self.result_status, list(self.reason_codes))
        finite_value = self.value is not None and math.isfinite(self.value)
        if self.result_status == "value":
            if not finite_value or self.sample_size < 1:
                raise ValueError("value result requires finite value and sample_size >= 1")
        elif self.result_status == "coverage-gap":
            if not self.reason_codes:
                raise ValueError("coverage-gap requires reason_codes")
            if self.value is None and self.sample_size != 0:
                raise ValueError("null coverage-gap requires sample_size = 0")
            if self.value is not None and (not finite_value or self.sample_size < 1):
                raise ValueError("numeric coverage-gap requires finite value and samples")
        elif self.value is not None or self.sample_size != 0 or not self.reason_codes:
            raise ValueError("non-value result requires value=null, sample_size=0 and reason_codes")
        return self


class InsightReportArtifactResult(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    storage_object_id: str = Field(min_length=1, max_length=128)
    object_uri: str = Field(min_length=1, max_length=2048)
    content_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    content_type: InsightReportArtifactContentType

    @field_validator("content_sha256")
    @classmethod
    def normalize_content_sha256(cls, value: str) -> str:
        return value.lower()


class InsightReportMetricSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    metric_result_id: str = Field(min_length=1, max_length=128)
    metric_key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=255)
    value: float | None
    unit: str = Field(min_length=1, max_length=64)
    sample_size: int = Field(ge=0)
    result_status: MetricResultStatus
    reason_codes: list[MetricResultReasonCode] = Field(default_factory=list, max_length=64)
    comparability_status: Literal["comparable", "partial", "structural-break", "not-applicable"]
    comparability_reason_codes: list[str] = Field(default_factory=list, max_length=64)
    definition_version: str = Field(min_length=1, max_length=128)
    scope: dict[str, Any]
    source_run_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("reason_codes", "comparability_reason_codes")
    @classmethod
    def report_reasons_are_valid(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("report reason codes must be unique and non-blank")
        return normalized

    @model_validator(mode="after")
    def report_outcome_is_explicit(self) -> InsightReportMetricSnapshot:
        validate_result_reason_semantics(self.result_status, list(self.reason_codes))
        if self.value is not None:
            if not math.isfinite(self.value) or self.sample_size < 1:
                raise ValueError("numeric report metric requires finite value and samples")
            return self
        if (
            self.sample_size != 0
            or self.result_status == "value"
            or not self.reason_codes
            or self.comparability_status == "comparable"
            or not self.comparability_reason_codes
        ):
            raise ValueError("N/A report metric requires governed non-comparable disposition")
        return self


class InsightReportEvidenceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_ref: str = Field(min_length=1, max_length=512)
    source_collection: Literal["evidence_packs", "documents"]
    title: str = Field(min_length=1, max_length=255)
    status: str | None = Field(default=None, max_length=64)
    summary: str = Field(min_length=1, max_length=2000)
    trace_id: str | None = Field(default=None, max_length=128)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class InsightReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    section_id: str = Field(min_length=1, max_length=128)
    section_version: int = Field(ge=1)
    order: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=255)
    summary: str = Field(min_length=1, max_length=4000)
    metric_result_ids: list[str] = Field(min_length=1, max_length=20)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)


class InsightReportGeneratorProof(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    proof_type: Literal["bff-governed-snapshot"]
    generator_id: Literal["auris-flow-bff"]
    generator_version: str = Field(min_length=1, max_length=128)
    generation_mode: Literal["deterministic-governed-snapshot"]
    metric_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    section_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class InsightReportDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: InsightReportDocumentSchemaVersion
    document_version: int = Field(ge=1)
    artifact_state: Literal["materialized"]
    report_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    trace_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)
    report_type: InsightReportType
    time_range: str = Field(min_length=1, max_length=512)
    owner: str = Field(min_length=1, max_length=128)
    metric_results: list[InsightReportMetricSnapshot] = Field(min_length=1, max_length=20)
    evidence: list[InsightReportEvidenceSnapshot] = Field(default_factory=list, max_length=100)
    sections: list[InsightReportSection] = Field(min_length=1, max_length=30)
    generator_proof: InsightReportGeneratorProof

    @model_validator(mode="after")
    def unique_document_references(self) -> InsightReportDocument:
        metric_ids = [item.metric_result_id for item in self.metric_results]
        evidence_refs = [item.evidence_ref for item in self.evidence]
        section_ids = [item.section_id for item in self.sections]
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("metric_results must be unique")
        if len(evidence_refs) != len(set(evidence_refs)):
            raise ValueError("evidence must be unique")
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("sections must be unique")
        if [item.order for item in self.sections] != list(range(1, len(self.sections) + 1)):
            raise ValueError("section order must be contiguous and start at 1")
        if any(item.metric_result_ids != metric_ids for item in self.sections):
            raise ValueError("every section must bind the complete frozen metric set")
        if any(not set(item.evidence_refs).issubset(evidence_refs) for item in self.sections):
            raise ValueError("section evidence_refs must exist in document evidence")
        return self


class InsightReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str | None = Field(default=None, min_length=3, max_length=128)
    title: str = Field(default="智能 BI 管理层摘要", min_length=1, max_length=255)
    report_type: InsightReportType = "daily"
    time_range: str | None = Field(default=None, max_length=512)
    range: str | None = Field(default=None, max_length=512)
    owner: str = Field(default="业务运营", min_length=1, max_length=128)
    metric_result_ids: list[str] = Field(min_length=1, max_length=20)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    report_sections: list[str] = Field(default_factory=list, max_length=30)
    store_ids: list[str] = Field(default_factory=list, max_length=50)
    model_version: str | None = Field(default=None, max_length=128)
    label_version: str | None = Field(default=None, max_length=128)
    source: str = Field(default="ui", min_length=1, max_length=128)

    @field_validator("metric_result_ids", "evidence_refs", "report_sections", "store_ids")
    @classmethod
    def unique_non_blank_refs(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for item in value:
            normalized = item.strip()
            if not normalized:
                raise ValueError("references must not contain blank values")
            if normalized not in result:
                result.append(normalized)
        return result

    @model_validator(mode="after")
    def normalize_time_range(self) -> InsightReportRequest:
        effective = (self.time_range or self.range or "").strip()
        if not effective:
            raise ValueError("time_range or range is required")
        self.time_range = effective
        self.range = effective
        return self


class InsightActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str | None = Field(default=None, min_length=3, max_length=128)
    report_id: str = Field(min_length=3, max_length=128)
    metric_result_id: str = Field(min_length=3, max_length=128)
    metric_key: str | None = Field(default=None, max_length=128)
    action_type: InsightActionType
    owner: str = Field(min_length=1, max_length=128)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    branch: InsightActionBranch = "auto"
    risk_level: InsightRiskLevel = "medium"
    hypothesis: str | None = Field(default=None, max_length=1000)
    target_value: float | None = None
    source: str = Field(default="ui", min_length=1, max_length=128)

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence_refs(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for item in value:
            normalized = item.strip()
            if not normalized:
                raise ValueError("evidence_refs must not contain blank values")
            if normalized not in result:
                result.append(normalized)
        return result


class InsightExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: str | None = Field(default=None, min_length=3, max_length=128)
    allocation_percent: int = Field(default=20, ge=1, le=50)
    duration_days: int = Field(default=7, ge=1, le=90)
    min_sample_size: int = Field(ge=10, le=10_000_000)
    primary_metric_key: str | None = Field(default=None, max_length=128)
    hypothesis: str = Field(min_length=1, max_length=1000)
    candidate: dict[str, Any] = Field(min_length=1)
    control: dict[str, Any] = Field(min_length=1)
    guardrails: dict[str, float] = Field(min_length=1)
    source: str = Field(default="ui", min_length=1, max_length=128)

    @field_validator("hypothesis")
    @classmethod
    def non_blank_hypothesis(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("hypothesis must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_experiment_design(self) -> InsightExperimentRequest:
        if self.candidate == self.control:
            raise ValueError("candidate and control must describe different variants")
        if any(not math.isfinite(value) for value in self.guardrails.values()):
            raise ValueError("guardrails must contain finite numeric thresholds")
        return self


class InsightExperimentRetryAttemptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=500)
    source: str = Field(default="ui", min_length=1, max_length=128)
