from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EvalDatasetPublicProvenance(BaseModel):
    """Immutable upstream facts for a public evaluation dataset snapshot."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_type: Literal["public_dataset"]
    registry_dataset_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]+$",
    )
    registry_schema_version: Literal["auris.public-audio-datasets.v1"]
    upstream_id: str = Field(min_length=1, max_length=128)
    upstream_revision: str = Field(min_length=1, max_length=128)
    upstream_split: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]+$",
    )
    upstream_url: str = Field(
        min_length=8,
        max_length=2048,
        pattern=r"^https://[^\s]+$",
    )
    license_spdx: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9.+-]+$",
    )
    license_url: str = Field(
        min_length=8,
        max_length=2048,
        pattern=r"^https://[^\s]+$",
    )
    license_accepted: Literal[True]
    repository_distribution: Literal["metadata-only"]
    evaluation_only: Literal[True]
    archive_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    prepared_manifest_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    citation: str = Field(min_length=1, max_length=4096)

    @field_validator(
        "upstream_id",
        "upstream_revision",
        "citation",
    )
    @classmethod
    def provenance_text_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("archive_sha256", "prepared_manifest_sha256")
    @classmethod
    def normalize_provenance_sha256(cls, value: str) -> str:
        return value.lower()


class EvalDatasetVersionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    eval_dataset_id: str | None = Field(
        default=None,
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    name: str = Field(min_length=1, max_length=255)
    capability: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]+$",
    )
    dataset_version: str = Field(min_length=1, max_length=64)
    manifest_storage_object_id: str = Field(min_length=3, max_length=128)
    manifest_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    sample_count: int = Field(gt=0, le=10_000_000)
    source: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: EvalDatasetPublicProvenance | None = None

    @field_validator("name", "dataset_version")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("manifest_sha256")
    @classmethod
    def normalize_sha256(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def public_dataset_provenance_is_complete(self) -> EvalDatasetVersionCreateRequest:
        if self.source == "public_dataset" and self.provenance is None:
            raise ValueError("public_dataset source requires immutable provenance")
        if self.provenance is not None and self.source != "public_dataset":
            raise ValueError("public dataset provenance requires source=public_dataset")
        if (
            self.provenance is not None
            and self.provenance.prepared_manifest_sha256 != self.manifest_sha256
        ):
            raise ValueError("public provenance must bind the registered manifest SHA-256")
        return self


class EvalDatasetVersionLockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_resource_version: int = Field(ge=1)
    confirmation: Literal["lock"]


LABEL_EVAL_SUITES = frozenset(
    {"golden", "boundary", "adversarial", "fresh", "canary", "regression"}
)


class LabelEvalMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    macro_f1: float = Field(ge=0, le=1)
    macro_f1_gain_pp: float = Field(ge=-100, le=100)
    critical_recall_delta_pp: float = Field(ge=-100, le=100)
    json_valid_rate: float = Field(ge=0, le=1)
    coverage_rate: float = Field(ge=0, le=1)
    conflict_rate: float = Field(ge=0, le=1)
    cost_ratio: float = Field(gt=0)
    latency_ratio: float = Field(gt=0)
    quality_passed: bool
    security_passed: bool
    format_passed: bool
    cost_passed: bool
    latency_passed: bool
    observability_passed: bool


class LabelEvalBootstrapCI(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    method: Literal["paired-bootstrap-v1"]
    confidence_level: float = Field(ge=0.95, le=0.95)
    resample_count: int = Field(ge=1000, le=1_000_000)
    random_seed: int = Field(ge=0, le=2**63 - 1)
    paired_sample_count: int = Field(gt=0, le=10_000_000)
    macro_f1_gain_lower_pp: float = Field(ge=-100, le=100)
    macro_f1_gain_upper_pp: float = Field(ge=-100, le=100)
    critical_recall_delta_lower_pp: float = Field(ge=-100, le=100)
    critical_recall_delta_upper_pp: float = Field(ge=-100, le=100)

    @model_validator(mode="after")
    def intervals_are_ordered(self) -> LabelEvalBootstrapCI:
        if self.macro_f1_gain_lower_pp > self.macro_f1_gain_upper_pp:
            raise ValueError("macro-F1 bootstrap interval is reversed")
        if self.critical_recall_delta_lower_pp > self.critical_recall_delta_upper_pp:
            raise ValueError("critical recall bootstrap interval is reversed")
        return self


class LabelEvalSuiteCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    suite: Literal["golden", "boundary", "adversarial", "fresh", "canary", "regression"]
    sample_count: int = Field(gt=0)
    sample_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metrics: LabelEvalMetrics


class LabelingEvalCompletionResult(BaseModel):
    """Signed evaluator payload that can become a release-gate fact."""

    model_config = ConfigDict(extra="forbid", strict=True)

    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sample_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    hidden_holdout_used: Literal[True]
    dev_set_used: Literal[False]
    suites: list[LabelEvalSuiteCompletion] = Field(min_length=6, max_length=6)
    overall: LabelEvalMetrics
    paired_bootstrap: LabelEvalBootstrapCI

    @field_validator("suites")
    @classmethod
    def all_suites_exactly_once(
        cls, suites: list[LabelEvalSuiteCompletion]
    ) -> list[LabelEvalSuiteCompletion]:
        names = [suite.suite for suite in suites]
        if len(set(names)) != len(names) or set(names) != LABEL_EVAL_SUITES:
            raise ValueError("all six locked evaluation suites are required exactly once")
        return suites

    @model_validator(mode="after")
    def paired_bootstrap_covers_locked_samples(self) -> LabelingEvalCompletionResult:
        sample_count = sum(suite.sample_count for suite in self.suites)
        if self.paired_bootstrap.paired_sample_count != sample_count:
            raise ValueError(
                "paired_sample_count must equal the total sample count of the six suites"
            )
        return self
