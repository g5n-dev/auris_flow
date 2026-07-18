from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

Identifier = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
Sha256 = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class StrictLabelMappingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CompatibilityEvidenceRequest(StrictLabelMappingModel):
    evidence_type: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    evidence_id: Identifier
    resource_version: StrictInt = Field(ge=1)
    content_sha256: Sha256


class IdentityMappingItemRequest(StrictLabelMappingModel):
    relation: Literal["identity"]
    source_label_id: Identifier
    target_label_id: Identifier
    source_semantic_sha256: Sha256 | None = None
    target_semantic_sha256: Sha256 | None = None


class RenameMappingItemRequest(StrictLabelMappingModel):
    relation: Literal["rename"]
    source_label_id: Identifier
    target_label_id: Identifier
    source_semantic_sha256: Sha256 | None = None
    target_semantic_sha256: Sha256 | None = None


class ReplaceMappingItemRequest(StrictLabelMappingModel):
    relation: Literal["replace"]
    source_label_id: Identifier
    target_label_id: Identifier
    compatibility: Literal["structural-break", "exact"] = "structural-break"
    compatibility_evidence: CompatibilityEvidenceRequest | None = None


class MergeMappingItemRequest(StrictLabelMappingModel):
    relation: Literal["merge"]
    source_label_ids: list[Identifier] = Field(min_length=1)
    target_label_id: Identifier
    allowed_metric_families: list[Annotated[StrictStr, Field(min_length=1, max_length=64)]]
    metric_grain: Annotated[StrictStr, Field(min_length=1, max_length=64)] | None = None
    lineage_key: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None = None
    reducer: Annotated[StrictStr, Field(min_length=1, max_length=64)] | None = None


class RetireMappingItemRequest(StrictLabelMappingModel):
    relation: Literal["retire"]
    source_label_id: Identifier
    target_label_id: Identifier | None = None


class SplitRecomputeMappingItemRequest(StrictLabelMappingModel):
    relation: Literal["split-recompute"]
    source_label_id: Identifier
    target_label_ids: list[Identifier] = Field(min_length=1)
    requires_recompute: StrictBool
    allocation_weights: list[StrictInt] | None = None
    copy_existing_facts: StrictBool = False


LabelMappingItemRequest = Annotated[
    IdentityMappingItemRequest
    | RenameMappingItemRequest
    | ReplaceMappingItemRequest
    | MergeMappingItemRequest
    | RetireMappingItemRequest
    | SplitRecomputeMappingItemRequest,
    Field(discriminator="relation"),
]


class LabelMappingCreateRequest(StrictLabelMappingModel):
    mapping_version: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    source_label_version_id: Identifier
    target_label_version_id: Identifier
    expected_source_resource_version: StrictInt = Field(ge=1)
    expected_target_resource_version: StrictInt = Field(ge=1)
    items: list[LabelMappingItemRequest] = Field(min_length=1)


class LabelMappingValidationRequest(StrictLabelMappingModel):
    expected_resource_version: StrictInt = Field(ge=1)


class LabelMappingApprovalRequest(StrictLabelMappingModel):
    expected_resource_version: StrictInt = Field(ge=1)
    reason: Annotated[StrictStr, Field(min_length=1, max_length=1000)]

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class LabelMappingBundlePublishRequest(StrictLabelMappingModel):
    mapping_version_ids: list[Identifier] = Field(min_length=1)
    expected_mapping_resource_versions: dict[Identifier, StrictInt]
    source_label_version_ids: list[Identifier] = Field(min_length=1)
    expected_source_resource_versions: dict[Identifier, StrictInt]
    target_label_version_id: Identifier
    expected_target_resource_version: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def require_complete_frozen_sets(self) -> LabelMappingBundlePublishRequest:
        dynamic_references = {"current", "head", "latest"}
        all_identifiers = [
            *self.mapping_version_ids,
            *self.source_label_version_ids,
            self.target_label_version_id,
        ]
        if any(value.lower() in dynamic_references for value in all_identifiers):
            raise ValueError("dynamic latest/head/current references are forbidden")
        if len(self.mapping_version_ids) != len(set(self.mapping_version_ids)):
            raise ValueError("mapping_version_ids must be unique")
        if len(self.source_label_version_ids) != len(set(self.source_label_version_ids)):
            raise ValueError("source_label_version_ids must be unique")
        if self.target_label_version_id in self.source_label_version_ids:
            raise ValueError("target_label_version_id cannot also be a source")
        if set(self.expected_mapping_resource_versions) != set(self.mapping_version_ids):
            raise ValueError(
                "expected_mapping_resource_versions must exactly match mapping_version_ids"
            )
        if set(self.expected_source_resource_versions) != set(self.source_label_version_ids):
            raise ValueError(
                "expected_source_resource_versions must exactly match source_label_version_ids"
            )
        if any(value < 1 for value in self.expected_mapping_resource_versions.values()):
            raise ValueError("expected mapping resource versions must be positive")
        if any(value < 1 for value in self.expected_source_resource_versions.values()):
            raise ValueError("expected source resource versions must be positive")
        return self
