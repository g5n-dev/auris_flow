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

IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$"
NAMESPACE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$"


class StrictLabelMetricScopeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LabelMetricRunScopeRequest(StrictLabelMetricScopeModel):
    taxonomy_mode: Literal["native", "normalized", "recomputed"]
    source_label_version_ids: list[StrictStr] = Field(min_length=1, max_length=100)
    target_label_version_id: StrictStr | None = Field(
        default=None,
        min_length=2,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    mapping_bundle_id: StrictStr | None = Field(
        default=None,
        min_length=2,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    fact_namespace: StrictStr = Field(
        min_length=2,
        max_length=128,
        pattern=NAMESPACE_PATTERN,
    )
    fact_set_id: StrictStr = Field(
        min_length=2,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    expected_fact_set_generation: StrictInt = Field(ge=1)
    fact_as_of: datetime = Field(strict=False)
    timezone: StrictStr = Field(min_length=1, max_length=64)
    period_boundary: StrictStr = Field(min_length=1, max_length=128)
    denominator_definition: StrictStr = Field(min_length=1, max_length=512)

    @field_validator("timezone", "period_boundary", "denominator_definition")
    @classmethod
    def governed_text_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("governed text fields must not be blank")
        return normalized

    @field_validator("source_label_version_ids")
    @classmethod
    def source_versions_are_unique_and_canonical(cls, values: list[str]) -> list[str]:
        normalized = [item.strip() for item in values]
        if any(not item for item in normalized):
            raise ValueError("source_label_version_ids must not contain blanks")
        if len(normalized) != len(set(normalized)):
            raise ValueError("source_label_version_ids must be unique")
        return sorted(normalized)

    @field_validator("fact_as_of")
    @classmethod
    def fact_cutoff_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fact_as_of must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def taxonomy_mode_has_exact_binding(self) -> LabelMetricRunScopeRequest:
        target = self.target_label_version_id
        bundle = self.mapping_bundle_id
        if self.taxonomy_mode == "native" and (target is not None or bundle is not None):
            raise ValueError("native mode cannot bind target_label_version_id or mapping_bundle_id")
        if self.taxonomy_mode == "normalized" and (target is None or bundle is None):
            raise ValueError(
                "normalized mode requires target_label_version_id and mapping_bundle_id"
            )
        if self.taxonomy_mode == "recomputed" and target is None:
            raise ValueError("recomputed mode requires target_label_version_id")
        return self


class LabelMetricResultMaterializeRequest(StrictLabelMetricScopeModel):
    metric_result_id: StrictStr = Field(
        min_length=2,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    metric_key: StrictStr = Field(min_length=1, max_length=128)
    metric_family: StrictStr = Field(min_length=1, max_length=96)
    value: JsonValue
    unit: StrictStr = Field(min_length=1, max_length=64)
    sample_size: StrictInt = Field(ge=0)
    source_run_id: StrictStr = Field(
        min_length=2,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    taxonomy_mode: Literal["native", "normalized", "recomputed"]
    source_label_version_ids: list[StrictStr] = Field(min_length=1, max_length=100)
    target_label_version_id: StrictStr | None = Field(
        default=None,
        min_length=2,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    mapping_bundle_id: StrictStr | None = Field(
        default=None,
        min_length=2,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    fact_namespace: StrictStr = Field(
        min_length=2,
        max_length=128,
        pattern=NAMESPACE_PATTERN,
    )
    fact_set_id: StrictStr = Field(
        min_length=2,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    expected_fact_set_generation: StrictInt = Field(ge=1)
    fact_as_of: datetime = Field(strict=False)
    metric_definition_versions: dict[StrictStr, StrictStr] = Field(min_length=1, max_length=64)
    timezone: StrictStr = Field(min_length=1, max_length=64)
    period_boundary: StrictStr = Field(min_length=1, max_length=128)
    denominator_definition: StrictStr = Field(min_length=1, max_length=512)
    result_payload: dict[StrictStr, JsonValue] = Field(default_factory=dict, max_length=128)

    @field_validator(
        "metric_key",
        "metric_family",
        "unit",
        "timezone",
        "period_boundary",
        "denominator_definition",
    )
    @classmethod
    def governed_text_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("governed text fields must not be blank")
        return normalized

    @field_validator("source_label_version_ids")
    @classmethod
    def source_versions_are_unique_and_canonical(cls, values: list[str]) -> list[str]:
        normalized = [item.strip() for item in values]
        if any(not item for item in normalized):
            raise ValueError("source_label_version_ids must not contain blanks")
        if len(normalized) != len(set(normalized)):
            raise ValueError("source_label_version_ids must be unique")
        return sorted(normalized)

    @field_validator("metric_definition_versions")
    @classmethod
    def definition_versions_are_normalized(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not key.strip() or not value.strip() for key, value in values.items()):
            raise ValueError("metric definition version keys and values must not be blank")
        return {key.strip(): values[key].strip() for key in sorted(values)}

    @field_validator("fact_as_of")
    @classmethod
    def fact_cutoff_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fact_as_of must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def taxonomy_mode_has_exact_binding(self) -> LabelMetricResultMaterializeRequest:
        target = self.target_label_version_id
        bundle = self.mapping_bundle_id
        if self.taxonomy_mode == "native" and (target is not None or bundle is not None):
            raise ValueError("native mode cannot bind target_label_version_id or mapping_bundle_id")
        if self.taxonomy_mode == "normalized" and (target is None or bundle is None):
            raise ValueError(
                "normalized mode requires target_label_version_id and mapping_bundle_id"
            )
        if self.taxonomy_mode == "recomputed" and target is None:
            raise ValueError("recomputed mode requires target_label_version_id")
        return self
