from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class MappingRelation(StrEnum):
    IDENTITY = "identity"
    RENAME = "rename"
    REPLACE = "replace"
    MERGE = "merge"
    RETIRE = "retire"
    SPLIT_RECOMPUTE = "split-recompute"


class MappingCompatibility(StrEnum):
    EXACT = "exact"
    METRIC_DEPENDENT = "metric-dependent"
    STRUCTURAL_BREAK = "structural-break"
    NOT_APPLICABLE = "not-applicable"


class ComparabilityStatus(StrEnum):
    COMPARABLE = "comparable"
    PARTIAL = "partial"
    STRUCTURAL_BREAK = "structural-break"
    NOT_APPLICABLE = "not-applicable"


class MappingCompileError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class LabelItemSnapshot:
    label_id: str
    canonical_name: str
    aliases: tuple[str, ...]
    value_type: str
    risk_level: str
    mutual_exclusion_group: str | None
    parent_ids: tuple[str, ...]
    aggregation_rule: dict[str, Any]
    status: str = "active"
    definition_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", tuple(self.aliases))
        object.__setattr__(self, "parent_ids", tuple(self.parent_ids))


@dataclass(frozen=True, slots=True)
class LabelVersionSnapshot:
    tenant_id: str
    project_id: str
    taxonomy_id: str
    label_version_id: str
    resource_version: int
    artifact_status: str
    content_sha256: str
    items: tuple[LabelItemSnapshot, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))


@dataclass(frozen=True, slots=True)
class CompatibilityEvidence:
    evidence_type: str
    evidence_id: str
    resource_version: int
    content_sha256: str

    def to_document(self) -> dict[str, object]:
        return {
            "content_sha256": self.content_sha256,
            "evidence_id": self.evidence_id,
            "evidence_type": self.evidence_type,
            "resource_version": self.resource_version,
        }


@dataclass(frozen=True, slots=True)
class IdentityIntent:
    source_label_id: str
    target_label_id: str
    source_semantic_sha256: str | None = None
    target_semantic_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class RenameIntent:
    source_label_id: str
    target_label_id: str
    source_semantic_sha256: str | None = None
    target_semantic_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ReplaceIntent:
    source_label_id: str
    target_label_id: str
    compatibility: MappingCompatibility = MappingCompatibility.STRUCTURAL_BREAK
    compatibility_evidence: CompatibilityEvidence | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "compatibility", MappingCompatibility(self.compatibility))


@dataclass(frozen=True, slots=True)
class MergeIntent:
    source_label_ids: tuple[str, ...]
    target_label_id: str
    allowed_metric_families: tuple[str, ...]
    metric_grain: str | None
    lineage_key: str | None
    reducer: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_label_ids", tuple(self.source_label_ids))
        object.__setattr__(
            self,
            "allowed_metric_families",
            tuple(self.allowed_metric_families),
        )


@dataclass(frozen=True, slots=True)
class RetireIntent:
    source_label_id: str
    target_label_id: str | None = None


@dataclass(frozen=True, slots=True)
class SplitRecomputeIntent:
    source_label_id: str
    target_label_ids: tuple[str, ...]
    requires_recompute: bool
    allocation_weights: tuple[int, ...] | None = None
    copy_existing_facts: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_label_ids", tuple(self.target_label_ids))
        if self.allocation_weights is not None:
            object.__setattr__(self, "allocation_weights", tuple(self.allocation_weights))


MappingIntent = (
    IdentityIntent
    | RenameIntent
    | ReplaceIntent
    | MergeIntent
    | RetireIntent
    | SplitRecomputeIntent
)


@dataclass(frozen=True, slots=True)
class EdgeCompileInput:
    mapping_version: str
    source: LabelVersionSnapshot
    target: LabelVersionSnapshot
    expected_source_resource_version: int
    expected_target_resource_version: int
    dispositions: tuple[MappingIntent, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "dispositions", tuple(self.dispositions))


@dataclass(frozen=True, slots=True)
class CompiledMappingTarget:
    target_label_id: str
    target_order: int
    definition_sha256: str
    semantic_sha256: str
    content_sha256: str

    def to_document(self) -> dict[str, object]:
        return {
            "content_sha256": self.content_sha256,
            "definition_sha256": self.definition_sha256,
            "semantic_sha256": self.semantic_sha256,
            "target_label_id": self.target_label_id,
            "target_order": self.target_order,
        }


@dataclass(frozen=True, slots=True)
class CompiledMappingItem:
    source_label_id: str
    relation: MappingRelation
    targets: tuple[CompiledMappingTarget, ...]
    compatibility: MappingCompatibility
    comparability_status: ComparabilityStatus
    allowed_metric_families: tuple[str, ...]
    metric_grain: str | None
    lineage_key: str | None
    reducer: str | None
    requires_recompute: bool
    source_definition_sha256: str
    source_semantic_sha256: str
    target_semantic_sha256: str | None
    compatibility_evidence: CompatibilityEvidence | None
    merge_group_sha256: str | None
    content_sha256: str

    def to_document(self) -> dict[str, object]:
        return {
            "allowed_metric_families": list(self.allowed_metric_families),
            "comparability_status": self.comparability_status.value,
            "compatibility": self.compatibility.value,
            "compatibility_evidence": (
                self.compatibility_evidence.to_document()
                if self.compatibility_evidence is not None
                else None
            ),
            "content_sha256": self.content_sha256,
            "lineage_key": self.lineage_key,
            "merge_group_sha256": self.merge_group_sha256,
            "metric_grain": self.metric_grain,
            "reducer": self.reducer,
            "relation": self.relation.value,
            "requires_recompute": self.requires_recompute,
            "source_definition_sha256": self.source_definition_sha256,
            "source_label_id": self.source_label_id,
            "source_semantic_sha256": self.source_semantic_sha256,
            "target_semantic_sha256": self.target_semantic_sha256,
            "targets": [target.to_document() for target in self.targets],
        }


@dataclass(frozen=True, slots=True)
class EdgeCoverageReport:
    active_source_item_count: int
    disposition_count: int
    normalizable_count: int
    exact_count: int
    metric_dependent_count: int
    structural_break_count: int
    coverage_gap_source_label_ids: tuple[str, ...]
    recompute_required_source_label_ids: tuple[str, ...]
    unmapped_source_label_ids: tuple[str, ...] = ()

    def to_document(self) -> dict[str, object]:
        return {
            "active_source_item_count": self.active_source_item_count,
            "coverage_gap_source_label_ids": list(self.coverage_gap_source_label_ids),
            "disposition_count": self.disposition_count,
            "exact_count": self.exact_count,
            "metric_dependent_count": self.metric_dependent_count,
            "normalizable_count": self.normalizable_count,
            "recompute_required_source_label_ids": list(self.recompute_required_source_label_ids),
            "structural_break_count": self.structural_break_count,
            "unmapped_source_label_ids": list(self.unmapped_source_label_ids),
        }


@dataclass(frozen=True, slots=True)
class CompiledEdge:
    mapping_version: str
    compiler_version: str
    metric_registry_version: str
    source_label_version_id: str
    target_label_version_id: str
    source_resource_version: int
    target_resource_version: int
    items: tuple[CompiledMappingItem, ...]
    coverage: EdgeCoverageReport
    canonical_manifest: dict[str, Any]
    content_sha256: str
