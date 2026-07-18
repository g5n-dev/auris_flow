from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, NoReturn

from app.domain.label_mapping.canonical import (
    CanonicalJsonError,
    label_item_definition_sha256,
    label_item_display_sha256,
    label_item_semantic_sha256,
    sha256_document,
)
from app.domain.label_mapping.registry import (
    DEFAULT_METRIC_COMPATIBILITY_REGISTRY,
    MetricCompatibilityRegistry,
)
from app.domain.label_mapping.types import (
    ComparabilityStatus,
    CompatibilityEvidence,
    CompiledEdge,
    CompiledMappingItem,
    CompiledMappingTarget,
    EdgeCompileInput,
    EdgeCoverageReport,
    IdentityIntent,
    LabelItemSnapshot,
    LabelVersionSnapshot,
    MappingCompatibility,
    MappingCompileError,
    MappingIntent,
    MappingRelation,
    MergeIntent,
    RenameIntent,
    ReplaceIntent,
    RetireIntent,
    SplitRecomputeIntent,
)

EDGE_SCHEMA_VERSION = "auris.label-mapping-edge/1"
EDGE_COMPILER_VERSION = "label-mapping-edge-compiler/1.0.0"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TARGET_COMPILABLE_STATUSES = frozenset(
    {
        "candidate",
        "validated",
        "locked",
        "evaluating",
        "gate_blocked",
        "review_required",
        "approved",
        "published",
    }
)
_ITEM_STATUSES = frozenset({"active", "retired", "pending-configuration"})


@dataclass(frozen=True, slots=True)
class _MaterializedItem:
    snapshot: LabelItemSnapshot
    definition_sha256: str
    semantic_sha256: str
    display_sha256: str


def _raise(
    code: str,
    message: str,
    *,
    path: str | None = None,
    details: dict[str, Any] | None = None,
) -> NoReturn:
    raise MappingCompileError(code, message, path=path, details=details)


def _require_text(value: str, *, path: str, max_length: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        _raise(
            "LABEL_MAPPING_INPUT_INVALID",
            f"{path} must be a non-empty string with at most {max_length} characters",
            path=path,
        )


def _require_sha256(value: str, *, path: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        _raise(
            "LABEL_MAPPING_INPUT_INVALID",
            f"{path} must be a lowercase SHA-256 digest",
            path=path,
        )


def _require_unique(values: tuple[str, ...], *, path: str) -> None:
    if len(values) != len(set(values)):
        _raise(
            "LABEL_MAPPING_AMBIGUOUS",
            f"{path} contains duplicate values",
            path=path,
            details={"values": list(values)},
        )


def _validate_version_snapshot(version: LabelVersionSnapshot, *, role: str) -> None:
    for field_name, value, max_length in (
        ("tenant_id", version.tenant_id, 64),
        ("project_id", version.project_id, 64),
        ("taxonomy_id", version.taxonomy_id, 128),
        ("label_version_id", version.label_version_id, 128),
    ):
        _require_text(value, path=f"{role}.{field_name}", max_length=max_length)
    if version.resource_version <= 0:
        _raise(
            "LABEL_MAPPING_INPUT_INVALID",
            f"{role}.resource_version must be positive",
            path=f"{role}.resource_version",
        )
    _require_sha256(version.content_sha256, path=f"{role}.content_sha256")
    if role == "source" and version.artifact_status != "published":
        _raise(
            "LABEL_MAPPING_SOURCE_NOT_PUBLISHED",
            "source label version must be published",
            path="source.artifact_status",
            details={"actual_status": version.artifact_status},
        )
    if role == "target" and version.artifact_status not in _TARGET_COMPILABLE_STATUSES:
        _raise(
            "LABEL_MAPPING_TARGET_STATUS_INVALID",
            "target label version is not eligible for edge compilation",
            path="target.artifact_status",
            details={"actual_status": version.artifact_status},
        )


def _materialize_items(
    version: LabelVersionSnapshot,
    *,
    role: str,
) -> dict[str, _MaterializedItem]:
    materialized: dict[str, _MaterializedItem] = {}
    for index, item in enumerate(version.items):
        path = f"{role}.items[{index}]"
        _require_text(item.label_id, path=f"{path}.label_id", max_length=128)
        _require_text(item.canonical_name, path=f"{path}.canonical_name", max_length=255)
        _require_text(item.value_type, path=f"{path}.value_type", max_length=32)
        _require_text(item.risk_level, path=f"{path}.risk_level", max_length=32)
        if item.status not in _ITEM_STATUSES:
            _raise(
                "LABEL_MAPPING_INPUT_INVALID",
                "label item has an unsupported status",
                path=f"{path}.status",
                details={"actual_status": item.status},
            )
        _require_unique(item.aliases, path=f"{path}.aliases")
        _require_unique(item.parent_ids, path=f"{path}.parent_ids")
        if item.label_id in materialized:
            _raise(
                "LABEL_MAPPING_AMBIGUOUS",
                "label version contains duplicate label IDs",
                path=f"{role}.items.{item.label_id}",
            )
        definition_sha256 = label_item_definition_sha256(item)
        semantic_sha256 = label_item_semantic_sha256(item)
        display_sha256 = label_item_display_sha256(item)
        if item.definition_sha256 is not None:
            _require_sha256(item.definition_sha256, path=f"{path}.definition_sha256")
            if item.definition_sha256 != definition_sha256:
                _raise(
                    "LABEL_VERSION_ITEM_HASH_DRIFT",
                    "label item definition differs from its frozen hash",
                    path=f"{path}.definition_sha256",
                    details={
                        "actual_sha256": definition_sha256,
                        "expected_sha256": item.definition_sha256,
                        "label_id": item.label_id,
                    },
                )
        materialized[item.label_id] = _MaterializedItem(
            snapshot=item,
            definition_sha256=definition_sha256,
            semantic_sha256=semantic_sha256,
            display_sha256=display_sha256,
        )
    return materialized


def _source_ids(intent: MappingIntent) -> tuple[str, ...]:
    if isinstance(intent, MergeIntent):
        if len(intent.source_label_ids) < 2:
            _raise(
                "LABEL_MAPPING_AMBIGUOUS",
                "merge requires at least two source labels",
                path="dispositions.merge.source_label_ids",
            )
        return intent.source_label_ids
    if isinstance(
        intent,
        (IdentityIntent, RenameIntent, ReplaceIntent, RetireIntent, SplitRecomputeIntent),
    ):
        return (intent.source_label_id,)
    _raise(
        "LABEL_MAPPING_RELATION_UNSUPPORTED",
        "mapping disposition type is not supported",
        path="dispositions",
    )


def _index_dispositions(
    request: EdgeCompileInput,
    source_items: dict[str, _MaterializedItem],
) -> list[tuple[str, MappingIntent]]:
    indexed: list[tuple[str, MappingIntent]] = []
    seen: set[str] = set()
    for intent in request.dispositions:
        for source_label_id in _source_ids(intent):
            _require_text(
                source_label_id,
                path="dispositions.source_label_id",
                max_length=128,
            )
            if source_label_id in seen:
                _raise(
                    "LABEL_MAPPING_AMBIGUOUS",
                    "one source label cannot have multiple dispositions",
                    path=f"dispositions.{source_label_id}",
                    details={"source_label_id": source_label_id},
                )
            materialized = source_items.get(source_label_id)
            if materialized is None:
                _raise(
                    "LABEL_MAPPING_SOURCE_ITEM_NOT_FOUND",
                    "mapping references an unknown source label",
                    path=f"dispositions.{source_label_id}",
                )
            if materialized.snapshot.status != "active":
                _raise(
                    "LABEL_MAPPING_SOURCE_ITEM_INACTIVE",
                    "only active source labels may receive a disposition",
                    path=f"dispositions.{source_label_id}",
                    details={"actual_status": materialized.snapshot.status},
                )
            seen.add(source_label_id)
            indexed.append((source_label_id, intent))

    active_source_ids = {
        label_id
        for label_id, materialized in source_items.items()
        if materialized.snapshot.status == "active"
    }
    missing = sorted(active_source_ids - seen)
    if missing:
        _raise(
            "LABEL_MAPPING_COVERAGE_GAP",
            "every active source label requires exactly one disposition",
            path="dispositions",
            details={"unmapped_source_label_ids": missing},
        )
    return sorted(indexed, key=lambda value: value[0])


def _target_item(
    target_items: dict[str, _MaterializedItem],
    target_label_id: str,
    *,
    path: str,
) -> _MaterializedItem:
    _require_text(target_label_id, path=path, max_length=128)
    target = target_items.get(target_label_id)
    if target is None:
        _raise(
            "LABEL_MAPPING_TARGET_ITEM_NOT_FOUND",
            "mapping references an unknown target label",
            path=path,
            details={"target_label_id": target_label_id},
        )
    if target.snapshot.status != "active":
        _raise(
            "LABEL_MAPPING_TARGET_ITEM_INACTIVE",
            "mapping target must be active in the target version",
            path=path,
            details={"actual_status": target.snapshot.status},
        )
    return target


def _compiled_targets(
    request: EdgeCompileInput,
    target_items: dict[str, _MaterializedItem],
    target_label_ids: tuple[str, ...],
) -> tuple[CompiledMappingTarget, ...]:
    _require_unique(target_label_ids, path="dispositions.target_label_ids")
    result: list[CompiledMappingTarget] = []
    for order, target_label_id in enumerate(sorted(target_label_ids)):
        target = _target_item(
            target_items,
            target_label_id,
            path=f"dispositions.targets.{target_label_id}",
        )
        target_document = {
            "definition_sha256": target.definition_sha256,
            "schema_version": "auris.label-mapping-target/1",
            "semantic_sha256": target.semantic_sha256,
            "target_label_id": target_label_id,
            "target_label_version_id": request.target.label_version_id,
            "target_order": order,
        }
        result.append(
            CompiledMappingTarget(
                target_label_id=target_label_id,
                target_order=order,
                definition_sha256=target.definition_sha256,
                semantic_sha256=target.semantic_sha256,
                content_sha256=sha256_document(target_document),
            )
        )
    return tuple(result)


def _validate_semantic_expectations(
    intent: IdentityIntent | RenameIntent,
    source: _MaterializedItem,
    target: _MaterializedItem,
) -> None:
    for path, expected, actual in (
        (
            "source_semantic_sha256",
            intent.source_semantic_sha256,
            source.semantic_sha256,
        ),
        (
            "target_semantic_sha256",
            intent.target_semantic_sha256,
            target.semantic_sha256,
        ),
    ):
        if expected is None:
            continue
        _require_sha256(expected, path=f"dispositions.{path}")
        if expected != actual:
            _raise(
                "LABEL_MAPPING_SEMANTIC_HASH_CHANGED",
                "client semantic precondition differs from authoritative label content",
                path=f"dispositions.{path}",
                details={"actual_sha256": actual, "expected_sha256": expected},
            )


def _validate_evidence(evidence: CompatibilityEvidence) -> None:
    _require_text(
        evidence.evidence_type,
        path="compatibility_evidence.evidence_type",
        max_length=64,
    )
    _require_text(
        evidence.evidence_id,
        path="compatibility_evidence.evidence_id",
        max_length=128,
    )
    if evidence.resource_version <= 0:
        _raise(
            "LABEL_MAPPING_COMPATIBILITY_EVIDENCE_REQUIRED",
            "compatibility evidence must freeze a positive resource version",
            path="compatibility_evidence.resource_version",
        )
    _require_sha256(
        evidence.content_sha256,
        path="compatibility_evidence.content_sha256",
    )


def _validated_metric_configuration(
    intent: MergeIntent,
    registry: MetricCompatibilityRegistry,
) -> tuple[tuple[str, ...], str, str, str]:
    grain = intent.metric_grain
    lineage_key = intent.lineage_key
    reducer = intent.reducer
    families = intent.allowed_metric_families
    if not families or not grain or not lineage_key or not reducer:
        _raise(
            "LABEL_MAPPING_REDUCER_REQUIRED",
            "merge requires metric families, grain, lineage key, and reducer",
            path="dispositions.merge",
        )
    _require_unique(families, path="dispositions.merge.allowed_metric_families")
    normalized_families = tuple(sorted(families))
    for family in normalized_families:
        rule = registry.rule_for(family)
        if rule is None or not rule.supports(
            grain=grain,
            lineage_key=lineage_key,
            reducer=reducer,
        ):
            _raise(
                "LABEL_MAPPING_REDUCER_REQUIRED",
                "merge metric configuration is not approved by the metric registry",
                path=f"dispositions.merge.allowed_metric_families.{family}",
                details={
                    "lineage_key": lineage_key,
                    "metric_family": family,
                    "metric_grain": grain,
                    "reducer": reducer,
                    "registry_version": registry.version,
                },
            )
    return normalized_families, grain, lineage_key, reducer


def _build_item(
    request: EdgeCompileInput,
    *,
    source: _MaterializedItem,
    relation: MappingRelation,
    targets: tuple[CompiledMappingTarget, ...],
    compatibility: MappingCompatibility,
    comparability_status: ComparabilityStatus,
    allowed_metric_families: tuple[str, ...] = (),
    metric_grain: str | None = None,
    lineage_key: str | None = None,
    reducer: str | None = None,
    requires_recompute: bool = False,
    compatibility_evidence: CompatibilityEvidence | None = None,
    merge_group_sha256: str | None = None,
) -> CompiledMappingItem:
    target_semantic_sha256 = targets[0].semantic_sha256 if len(targets) == 1 else None
    content_document = {
        "allowed_metric_families": list(allowed_metric_families),
        "comparability_status": comparability_status.value,
        "compatibility": compatibility.value,
        "compatibility_evidence": (
            compatibility_evidence.to_document() if compatibility_evidence is not None else None
        ),
        "lineage_key": lineage_key,
        "merge_group_sha256": merge_group_sha256,
        "metric_grain": metric_grain,
        "reducer": reducer,
        "relation": relation.value,
        "requires_recompute": requires_recompute,
        "schema_version": "auris.label-mapping-item/1",
        "source_definition_sha256": source.definition_sha256,
        "source_label_id": source.snapshot.label_id,
        "source_label_version_id": request.source.label_version_id,
        "source_semantic_sha256": source.semantic_sha256,
        "target_label_version_id": request.target.label_version_id,
        "target_semantic_sha256": target_semantic_sha256,
        "targets": [target.to_document() for target in targets],
    }
    return CompiledMappingItem(
        source_label_id=source.snapshot.label_id,
        relation=relation,
        targets=targets,
        compatibility=compatibility,
        comparability_status=comparability_status,
        allowed_metric_families=allowed_metric_families,
        metric_grain=metric_grain,
        lineage_key=lineage_key,
        reducer=reducer,
        requires_recompute=requires_recompute,
        source_definition_sha256=source.definition_sha256,
        source_semantic_sha256=source.semantic_sha256,
        target_semantic_sha256=target_semantic_sha256,
        compatibility_evidence=compatibility_evidence,
        merge_group_sha256=merge_group_sha256,
        content_sha256=sha256_document(content_document),
    )


def _compile_identity_or_rename(
    request: EdgeCompileInput,
    source: _MaterializedItem,
    target_items: dict[str, _MaterializedItem],
    intent: IdentityIntent | RenameIntent,
) -> CompiledMappingItem:
    relation = (
        MappingRelation.IDENTITY if isinstance(intent, IdentityIntent) else MappingRelation.RENAME
    )
    target = _target_item(
        target_items,
        intent.target_label_id,
        path=f"dispositions.{source.snapshot.label_id}.target_label_id",
    )
    _validate_semantic_expectations(intent, source, target)
    if source.snapshot.label_id != target.snapshot.label_id:
        _raise(
            "LABEL_MAPPING_SEMANTIC_HASH_CHANGED",
            "identity and rename require the stable label ID to be preserved",
            path=f"dispositions.{source.snapshot.label_id}.target_label_id",
        )
    if source.semantic_sha256 != target.semantic_sha256:
        _raise(
            "LABEL_MAPPING_SEMANTIC_HASH_CHANGED",
            "identity and rename require unchanged non-display semantics",
            path=f"dispositions.{source.snapshot.label_id}",
            details={
                "source_semantic_sha256": source.semantic_sha256,
                "target_semantic_sha256": target.semantic_sha256,
            },
        )
    display_changed = source.display_sha256 != target.display_sha256
    if relation is MappingRelation.IDENTITY and display_changed:
        _raise(
            "LABEL_MAPPING_RELATION_MISMATCH",
            "display-only changes must use rename rather than identity",
            path=f"dispositions.{source.snapshot.label_id}.relation",
            details={"suggested_relation": MappingRelation.RENAME.value},
        )
    if relation is MappingRelation.RENAME and not display_changed:
        _raise(
            "LABEL_MAPPING_RELATION_MISMATCH",
            "unchanged labels must use identity rather than rename",
            path=f"dispositions.{source.snapshot.label_id}.relation",
            details={"suggested_relation": MappingRelation.IDENTITY.value},
        )
    targets = _compiled_targets(
        request,
        target_items,
        (intent.target_label_id,),
    )
    return _build_item(
        request,
        source=source,
        relation=relation,
        targets=targets,
        compatibility=MappingCompatibility.EXACT,
        comparability_status=ComparabilityStatus.COMPARABLE,
    )


def _compile_replace(
    request: EdgeCompileInput,
    source: _MaterializedItem,
    target_items: dict[str, _MaterializedItem],
    intent: ReplaceIntent,
) -> CompiledMappingItem:
    targets = _compiled_targets(request, target_items, (intent.target_label_id,))
    if intent.compatibility is MappingCompatibility.EXACT:
        if intent.compatibility_evidence is None:
            _raise(
                "LABEL_MAPPING_COMPATIBILITY_EVIDENCE_REQUIRED",
                "one-to-one cardinality cannot prove exact compatibility",
                path=f"dispositions.{source.snapshot.label_id}.compatibility_evidence",
            )
        _validate_evidence(intent.compatibility_evidence)
        comparability_status = ComparabilityStatus.COMPARABLE
    elif intent.compatibility is MappingCompatibility.STRUCTURAL_BREAK:
        comparability_status = ComparabilityStatus.STRUCTURAL_BREAK
    else:
        _raise(
            "LABEL_MAPPING_COMPATIBILITY_INVALID",
            "replace supports structural-break or evidence-backed exact compatibility",
            path=f"dispositions.{source.snapshot.label_id}.compatibility",
        )
    return _build_item(
        request,
        source=source,
        relation=MappingRelation.REPLACE,
        targets=targets,
        compatibility=intent.compatibility,
        comparability_status=comparability_status,
        compatibility_evidence=intent.compatibility_evidence,
    )


def _merge_group_sha256(
    request: EdgeCompileInput,
    intent: MergeIntent,
    *,
    allowed_metric_families: tuple[str, ...],
    metric_grain: str,
    lineage_key: str,
    reducer: str,
) -> str:
    return sha256_document(
        {
            "allowed_metric_families": list(allowed_metric_families),
            "lineage_key": lineage_key,
            "metric_grain": metric_grain,
            "reducer": reducer,
            "schema_version": "auris.label-mapping-merge-group/1",
            "source_label_ids": sorted(intent.source_label_ids),
            "source_label_version_id": request.source.label_version_id,
            "target_label_id": intent.target_label_id,
            "target_label_version_id": request.target.label_version_id,
        }
    )


def _compile_merge(
    request: EdgeCompileInput,
    source: _MaterializedItem,
    target_items: dict[str, _MaterializedItem],
    intent: MergeIntent,
    registry: MetricCompatibilityRegistry,
) -> CompiledMappingItem:
    families, grain, lineage_key, reducer = _validated_metric_configuration(intent, registry)
    targets = _compiled_targets(request, target_items, (intent.target_label_id,))
    group_sha256 = _merge_group_sha256(
        request,
        intent,
        allowed_metric_families=families,
        metric_grain=grain,
        lineage_key=lineage_key,
        reducer=reducer,
    )
    return _build_item(
        request,
        source=source,
        relation=MappingRelation.MERGE,
        targets=targets,
        compatibility=MappingCompatibility.METRIC_DEPENDENT,
        comparability_status=ComparabilityStatus.PARTIAL,
        allowed_metric_families=families,
        metric_grain=grain,
        lineage_key=lineage_key,
        reducer=reducer,
        merge_group_sha256=group_sha256,
    )


def _compile_retire(
    request: EdgeCompileInput,
    source: _MaterializedItem,
    intent: RetireIntent,
) -> CompiledMappingItem:
    if intent.target_label_id is not None:
        _raise(
            "LABEL_MAPPING_RETIRE_TARGET_FORBIDDEN",
            "retire disposition cannot reference a target label",
            path=f"dispositions.{source.snapshot.label_id}.target_label_id",
        )
    return _build_item(
        request,
        source=source,
        relation=MappingRelation.RETIRE,
        targets=(),
        compatibility=MappingCompatibility.STRUCTURAL_BREAK,
        comparability_status=ComparabilityStatus.STRUCTURAL_BREAK,
    )


def _compile_split(
    request: EdgeCompileInput,
    source: _MaterializedItem,
    target_items: dict[str, _MaterializedItem],
    intent: SplitRecomputeIntent,
) -> CompiledMappingItem:
    if (
        not intent.requires_recompute
        or len(intent.target_label_ids) < 2
        or intent.allocation_weights is not None
        or intent.copy_existing_facts
    ):
        _raise(
            "LABEL_MAPPING_RECOMPUTE_REQUIRED",
            "split requires at least two targets and evidence recomputation without allocation",
            path=f"dispositions.{source.snapshot.label_id}",
        )
    targets = _compiled_targets(request, target_items, intent.target_label_ids)
    return _build_item(
        request,
        source=source,
        relation=MappingRelation.SPLIT_RECOMPUTE,
        targets=targets,
        compatibility=MappingCompatibility.STRUCTURAL_BREAK,
        comparability_status=ComparabilityStatus.STRUCTURAL_BREAK,
        requires_recompute=True,
    )


def _compile_disposition(
    request: EdgeCompileInput,
    source: _MaterializedItem,
    target_items: dict[str, _MaterializedItem],
    intent: MappingIntent,
    registry: MetricCompatibilityRegistry,
) -> CompiledMappingItem:
    if isinstance(intent, (IdentityIntent, RenameIntent)):
        return _compile_identity_or_rename(request, source, target_items, intent)
    if isinstance(intent, ReplaceIntent):
        return _compile_replace(request, source, target_items, intent)
    if isinstance(intent, MergeIntent):
        return _compile_merge(request, source, target_items, intent, registry)
    if isinstance(intent, RetireIntent):
        return _compile_retire(request, source, intent)
    if isinstance(intent, SplitRecomputeIntent):
        return _compile_split(request, source, target_items, intent)
    _raise(
        "LABEL_MAPPING_RELATION_UNSUPPORTED",
        "mapping disposition type is not supported",
        path=f"dispositions.{source.snapshot.label_id}",
    )


def _coverage(items: tuple[CompiledMappingItem, ...]) -> EdgeCoverageReport:
    exact_count = sum(item.compatibility is MappingCompatibility.EXACT for item in items)
    metric_dependent_count = sum(
        item.compatibility is MappingCompatibility.METRIC_DEPENDENT for item in items
    )
    structural_break_count = sum(
        item.compatibility is MappingCompatibility.STRUCTURAL_BREAK for item in items
    )
    coverage_gaps = tuple(
        item.source_label_id for item in items if item.relation is MappingRelation.RETIRE
    )
    recompute_required = tuple(item.source_label_id for item in items if item.requires_recompute)
    return EdgeCoverageReport(
        active_source_item_count=len(items),
        disposition_count=len(items),
        normalizable_count=exact_count + metric_dependent_count,
        exact_count=exact_count,
        metric_dependent_count=metric_dependent_count,
        structural_break_count=structural_break_count,
        coverage_gap_source_label_ids=coverage_gaps,
        recompute_required_source_label_ids=recompute_required,
    )


def _compile_edge(
    request: EdgeCompileInput,
    registry: MetricCompatibilityRegistry,
) -> CompiledEdge:
    _require_text(request.mapping_version, path="mapping_version", max_length=64)
    _validate_version_snapshot(request.source, role="source")
    _validate_version_snapshot(request.target, role="target")
    if (
        request.source.tenant_id != request.target.tenant_id
        or request.source.project_id != request.target.project_id
    ):
        _raise(
            "LABEL_MAPPING_SCOPE_MISMATCH",
            "source and target label versions must belong to the same scope",
            details={
                "source_project_id": request.source.project_id,
                "source_tenant_id": request.source.tenant_id,
                "target_project_id": request.target.project_id,
                "target_tenant_id": request.target.tenant_id,
            },
        )
    if request.source.taxonomy_id != request.target.taxonomy_id:
        _raise(
            "LABEL_MAPPING_TAXONOMY_MISMATCH",
            "source and target label versions must belong to the same taxonomy",
        )
    if request.source.label_version_id == request.target.label_version_id:
        _raise(
            "LABEL_MAPPING_VERSION_PAIR_INVALID",
            "source and target label versions must be different",
        )
    for role, expected, actual in (
        (
            "source",
            request.expected_source_resource_version,
            request.source.resource_version,
        ),
        (
            "target",
            request.expected_target_resource_version,
            request.target.resource_version,
        ),
    ):
        if expected <= 0 or expected != actual:
            _raise(
                "LABEL_MAPPING_RESOURCE_VERSION_CONFLICT",
                f"{role} label version resource version has drifted",
                path=f"expected_{role}_resource_version",
                details={
                    "actual_resource_version": actual,
                    "expected_resource_version": expected,
                    "role": role,
                },
            )

    source_items = _materialize_items(request.source, role="source")
    target_items = _materialize_items(request.target, role="target")
    indexed = _index_dispositions(request, source_items)
    compiled_items = tuple(
        _compile_disposition(
            request,
            source_items[source_label_id],
            target_items,
            intent,
            registry,
        )
        for source_label_id, intent in indexed
    )
    coverage = _coverage(compiled_items)
    manifest: dict[str, Any] = {
        "compiler_version": EDGE_COMPILER_VERSION,
        "coverage": coverage.to_document(),
        "items": [item.to_document() for item in compiled_items],
        "mapping_version": request.mapping_version,
        "metric_registry_version": registry.version,
        "schema_version": EDGE_SCHEMA_VERSION,
        "scope": {
            "project_id": request.source.project_id,
            "tenant_id": request.source.tenant_id,
        },
        "source_label_version": {
            "content_sha256": request.source.content_sha256,
            "label_version_id": request.source.label_version_id,
            "resource_version": request.source.resource_version,
            "taxonomy_id": request.source.taxonomy_id,
        },
        "target_label_version": {
            "content_sha256": request.target.content_sha256,
            "label_version_id": request.target.label_version_id,
            "resource_version": request.target.resource_version,
            "taxonomy_id": request.target.taxonomy_id,
        },
    }
    return CompiledEdge(
        mapping_version=request.mapping_version,
        compiler_version=EDGE_COMPILER_VERSION,
        metric_registry_version=registry.version,
        source_label_version_id=request.source.label_version_id,
        target_label_version_id=request.target.label_version_id,
        source_resource_version=request.source.resource_version,
        target_resource_version=request.target.resource_version,
        items=compiled_items,
        coverage=coverage,
        canonical_manifest=manifest,
        content_sha256=sha256_document(manifest),
    )


def compile_edge(
    request: EdgeCompileInput,
    *,
    registry: MetricCompatibilityRegistry = DEFAULT_METRIC_COMPATIBILITY_REGISTRY,
) -> CompiledEdge:
    """Compile one immutable source-version to target-version mapping edge."""

    try:
        return _compile_edge(request, registry)
    except CanonicalJsonError as exc:
        raise MappingCompileError(
            "LABEL_MAPPING_CANONICAL_JSON_INVALID",
            str(exc),
        ) from exc
