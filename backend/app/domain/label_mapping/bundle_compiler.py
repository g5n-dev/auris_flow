from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NoReturn

from app.domain.label_mapping.canonical import sha256_document
from app.domain.label_mapping.registry import (
    DEFAULT_METRIC_COMPATIBILITY_REGISTRY,
    MetricCompatibilityRegistry,
)
from app.domain.label_mapping.types import (
    CompiledEdge,
    CompiledMappingItem,
    LabelVersionSnapshot,
    MappingCompatibility,
    MappingRelation,
)

BUNDLE_SCHEMA_VERSION = "auris.label-mapping-bundle/1"
BUNDLE_COMPILER_VERSION = "label-mapping-bundle-compiler/1.0.0"
_DYNAMIC_REFERENCES = frozenset({"current", "head", "latest"})
_COMPARABILITY_RANK = {
    "comparable": 0,
    "partial": 1,
    "structural-break": 2,
    "not-applicable": 3,
}


class BundleCompileError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class BundleEdgeSnapshot:
    mapping_version_id: str
    mapping_resource_version: int
    compiled_edge: CompiledEdge


@dataclass(frozen=True, slots=True)
class BundleCompileInput:
    tenant_id: str
    project_id: str
    taxonomy_id: str
    source_versions: tuple[LabelVersionSnapshot, ...]
    target_version: LabelVersionSnapshot
    edges: tuple[BundleEdgeSnapshot, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_versions", tuple(self.source_versions))
        object.__setattr__(self, "edges", tuple(self.edges))


@dataclass(frozen=True, slots=True)
class CompiledBundleSource:
    source_label_version_id: str
    source_resource_version: int
    source_order: int
    version_content_sha256: str
    content_sha256: str

    def to_document(self) -> dict[str, Any]:
        return {
            "content_sha256": self.content_sha256,
            "source_label_version_id": self.source_label_version_id,
            "source_order": self.source_order,
            "source_resource_version": self.source_resource_version,
            "version_content_sha256": self.version_content_sha256,
        }


@dataclass(frozen=True, slots=True)
class CompiledBundleMember:
    mapping_version_id: str
    mapping_resource_version: int
    source_label_version_id: str
    target_label_version_id: str
    edge_order: int
    edge_content_sha256: str

    def to_document(self) -> dict[str, Any]:
        return {
            "edge_content_sha256": self.edge_content_sha256,
            "edge_order": self.edge_order,
            "mapping_resource_version": self.mapping_resource_version,
            "mapping_version_id": self.mapping_version_id,
            "source_label_version_id": self.source_label_version_id,
            "target_label_version_id": self.target_label_version_id,
        }


@dataclass(frozen=True, slots=True)
class CompiledBundlePath:
    source_label_version_id: str
    target_label_version_id: str
    source_label_id: str
    target_label_id: str | None
    metric_family: str
    relation_path: tuple[dict[str, Any], ...]
    mapping_version_ids: tuple[str, ...]
    metric_grain: str | None
    lineage_key: str | None
    reducer: str | None
    comparability_status: str
    requires_recompute: bool
    coverage_gap: bool
    path_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "relation_path", tuple(self.relation_path))
        object.__setattr__(self, "mapping_version_ids", tuple(self.mapping_version_ids))

    def to_document(self) -> dict[str, Any]:
        return {
            "comparability_status": self.comparability_status,
            "coverage_gap": self.coverage_gap,
            "lineage_key": self.lineage_key,
            "mapping_version_ids": list(self.mapping_version_ids),
            "metric_family": self.metric_family,
            "metric_grain": self.metric_grain,
            "path_sha256": self.path_sha256,
            "reducer": self.reducer,
            "relation_path": list(self.relation_path),
            "requires_recompute": self.requires_recompute,
            "source_label_id": self.source_label_id,
            "source_label_version_id": self.source_label_version_id,
            "target_label_id": self.target_label_id,
            "target_label_version_id": self.target_label_version_id,
        }


@dataclass(frozen=True, slots=True)
class CompiledBundle:
    compiler_version: str
    metric_registry_version: str
    source_label_version_ids: tuple[str, ...]
    target_label_version_id: str
    source_manifest_sha256: str
    sources: tuple[CompiledBundleSource, ...]
    members: tuple[CompiledBundleMember, ...]
    paths: tuple[CompiledBundlePath, ...]
    canonical_manifest: dict[str, Any]
    canonical_manifest_sha256: str
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_label_version_ids",
            tuple(self.source_label_version_ids),
        )
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "members", tuple(self.members))
        object.__setattr__(self, "paths", tuple(self.paths))


def _raise(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> NoReturn:
    raise BundleCompileError(code, message, details=details)


def _require_static_reference(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        _raise(
            "LABEL_MAPPING_BUNDLE_INPUT_INVALID",
            f"{field} must be a non-empty frozen identifier",
            details={"field": field},
        )
    if value.strip().lower() in _DYNAMIC_REFERENCES:
        _raise(
            "LABEL_MAPPING_BUNDLE_DYNAMIC_REFERENCE",
            "mapping bundles cannot resolve dynamic latest/head/current references",
            details={"field": field, "value": value},
        )


def _require_unique(values: list[str], *, field: str) -> None:
    if len(values) != len(set(values)):
        _raise(
            "LABEL_MAPPING_BUNDLE_AMBIGUOUS",
            f"{field} contains duplicate frozen identifiers",
            details={"field": field, "values": values},
        )


def _validate_scope(request: BundleCompileInput) -> None:
    for field, value in (
        ("tenant_id", request.tenant_id),
        ("project_id", request.project_id),
        ("taxonomy_id", request.taxonomy_id),
    ):
        _require_static_reference(value, field=field)
    versions = (*request.source_versions, request.target_version)
    for index, version in enumerate(versions):
        _require_static_reference(
            version.label_version_id,
            field=f"versions[{index}].label_version_id",
        )
        if (
            version.tenant_id != request.tenant_id
            or version.project_id != request.project_id
            or version.taxonomy_id != request.taxonomy_id
        ):
            _raise(
                "LABEL_MAPPING_BUNDLE_SCOPE_MISMATCH",
                "all source and target versions must share tenant, project, and taxonomy",
                details={"label_version_id": version.label_version_id},
            )
        if version.artifact_status != "published":
            _raise(
                "LABEL_MAPPING_BUNDLE_VERSION_NOT_PUBLISHED",
                "bundle source and target versions must be published artifacts",
                details={
                    "actual_status": version.artifact_status,
                    "label_version_id": version.label_version_id,
                },
            )
    source_ids = [version.label_version_id for version in request.source_versions]
    if not source_ids:
        _raise(
            "LABEL_MAPPING_BUNDLE_INPUT_INVALID",
            "at least one frozen source label version is required",
        )
    _require_unique(source_ids, field="source_label_version_ids")
    if request.target_version.label_version_id in source_ids:
        _raise(
            "LABEL_MAPPING_BUNDLE_AMBIGUOUS",
            "the final target cannot also be a bundle source",
            details={"target_label_version_id": request.target_version.label_version_id},
        )


def _validate_edges(request: BundleCompileInput) -> None:
    if not request.edges:
        _raise(
            "LABEL_MAPPING_BUNDLE_DISCONNECTED",
            "at least one frozen mapping edge is required",
        )
    mapping_ids: list[str] = []
    for index, edge in enumerate(request.edges):
        _require_static_reference(
            edge.mapping_version_id,
            field=f"edges[{index}].mapping_version_id",
        )
        mapping_ids.append(edge.mapping_version_id)
        if edge.mapping_resource_version <= 0:
            _raise(
                "LABEL_MAPPING_BUNDLE_INPUT_INVALID",
                "mapping edge resource versions must be positive",
                details={"mapping_version_id": edge.mapping_version_id},
            )
        compiled = edge.compiled_edge
        for field, value in (
            ("source_label_version_id", compiled.source_label_version_id),
            ("target_label_version_id", compiled.target_label_version_id),
        ):
            _require_static_reference(
                value,
                field=f"edges[{index}].{field}",
            )
        for item in compiled.items:
            if (
                item.relation is MappingRelation.REPLACE
                and item.compatibility is MappingCompatibility.EXACT
                and item.compatibility_evidence is None
            ):
                _raise(
                    "LABEL_MAPPING_BUNDLE_EXACT_EVIDENCE_REQUIRED",
                    "exact replacement paths require immutable compatibility evidence",
                    details={
                        "mapping_version_id": edge.mapping_version_id,
                        "source_label_id": item.source_label_id,
                    },
                )
    _require_unique(mapping_ids, field="mapping_version_ids")


def _outgoing_edges(
    edges: tuple[BundleEdgeSnapshot, ...],
) -> dict[str, BundleEdgeSnapshot]:
    outgoing: dict[str, BundleEdgeSnapshot] = {}
    for edge in edges:
        source_id = edge.compiled_edge.source_label_version_id
        existing = outgoing.get(source_id)
        if existing is not None:
            _raise(
                "LABEL_MAPPING_BUNDLE_AMBIGUOUS",
                "one version cannot have multiple outgoing mapping edges in a bundle",
                details={
                    "mapping_version_ids": sorted(
                        [existing.mapping_version_id, edge.mapping_version_id]
                    ),
                    "source_label_version_id": source_id,
                },
            )
        outgoing[source_id] = edge
    return outgoing


def _reject_cycles(outgoing: dict[str, BundleEdgeSnapshot]) -> None:
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(version_id: str, trail: tuple[str, ...]) -> None:
        if version_id in visiting:
            _raise(
                "LABEL_MAPPING_BUNDLE_CYCLE",
                "mapping bundle edge graph contains a cycle",
                details={"version_path": [*trail, version_id]},
            )
        if version_id in visited:
            return
        visiting.add(version_id)
        edge = outgoing.get(version_id)
        if edge is not None:
            visit(
                edge.compiled_edge.target_label_version_id,
                (*trail, version_id),
            )
        visiting.remove(version_id)
        visited.add(version_id)

    for version_id in sorted(outgoing):
        visit(version_id, ())


def _routes_to_target(
    request: BundleCompileInput,
    outgoing: dict[str, BundleEdgeSnapshot],
) -> dict[str, tuple[BundleEdgeSnapshot, ...]]:
    target_id = request.target_version.label_version_id
    routes: dict[str, tuple[BundleEdgeSnapshot, ...]] = {}
    used_mapping_ids: set[str] = set()
    for source in sorted(
        request.source_versions,
        key=lambda version: version.label_version_id,
    ):
        current_id = source.label_version_id
        route: list[BundleEdgeSnapshot] = []
        while current_id != target_id:
            edge = outgoing.get(current_id)
            if edge is None:
                _raise(
                    "LABEL_MAPPING_BUNDLE_DISCONNECTED",
                    "a frozen source version cannot reach the final target",
                    details={
                        "disconnected_at_label_version_id": current_id,
                        "source_label_version_id": source.label_version_id,
                        "target_label_version_id": target_id,
                    },
                )
            route.append(edge)
            used_mapping_ids.add(edge.mapping_version_id)
            current_id = edge.compiled_edge.target_label_version_id
        routes[source.label_version_id] = tuple(route)
    all_mapping_ids = {edge.mapping_version_id for edge in request.edges}
    unused = sorted(all_mapping_ids - used_mapping_ids)
    if unused:
        _raise(
            "LABEL_MAPPING_BUNDLE_DISCONNECTED",
            "mapping bundle contains edges disconnected from the frozen source set",
            details={"unused_mapping_version_ids": unused},
        )
    return routes


def _distance_to_target(
    source_id: str,
    target_id: str,
    outgoing: dict[str, BundleEdgeSnapshot],
) -> int:
    distance = 0
    current_id = source_id
    while current_id != target_id:
        edge = outgoing.get(current_id)
        if edge is None:
            return -1
        distance += 1
        current_id = edge.compiled_edge.target_label_version_id
    return distance


def _compile_members(
    request: BundleCompileInput,
    outgoing: dict[str, BundleEdgeSnapshot],
) -> tuple[CompiledBundleMember, ...]:
    target_id = request.target_version.label_version_id
    ordered = sorted(
        request.edges,
        key=lambda edge: (
            -_distance_to_target(
                edge.compiled_edge.source_label_version_id,
                target_id,
                outgoing,
            ),
            edge.compiled_edge.source_label_version_id,
            edge.compiled_edge.target_label_version_id,
            edge.mapping_version_id,
        ),
    )
    return tuple(
        CompiledBundleMember(
            mapping_version_id=edge.mapping_version_id,
            mapping_resource_version=edge.mapping_resource_version,
            source_label_version_id=edge.compiled_edge.source_label_version_id,
            target_label_version_id=edge.compiled_edge.target_label_version_id,
            edge_order=index,
            edge_content_sha256=edge.compiled_edge.content_sha256,
        )
        for index, edge in enumerate(ordered)
    )


def _compile_sources(
    request: BundleCompileInput,
) -> tuple[tuple[CompiledBundleSource, ...], str]:
    sources: list[CompiledBundleSource] = []
    for order, version in enumerate(
        sorted(request.source_versions, key=lambda value: value.label_version_id)
    ):
        document = {
            "schema_version": "auris.label-mapping-bundle-source/1",
            "source_label_version_id": version.label_version_id,
            "source_order": order,
            "source_resource_version": version.resource_version,
            "version_content_sha256": version.content_sha256,
        }
        sources.append(
            CompiledBundleSource(
                source_label_version_id=version.label_version_id,
                source_resource_version=version.resource_version,
                source_order=order,
                version_content_sha256=version.content_sha256,
                content_sha256=sha256_document(document),
            )
        )
    source_manifest = {
        "schema_version": "auris.label-mapping-bundle-source-set/1",
        "sources": [source.to_document() for source in sources],
    }
    return tuple(sources), sha256_document(source_manifest)


def _worst_comparability(current: str, candidate: str) -> str:
    if _COMPARABILITY_RANK[candidate] > _COMPARABILITY_RANK[current]:
        return candidate
    return current


def _relation_step(
    edge: BundleEdgeSnapshot,
    item: CompiledMappingItem,
) -> dict[str, Any]:
    return {
        "comparability_status": item.comparability_status.value,
        "compatibility": item.compatibility.value,
        "content_sha256": item.content_sha256,
        "mapping_version_id": edge.mapping_version_id,
        "relation": item.relation.value,
        "source_label_id": item.source_label_id,
        "source_label_version_id": edge.compiled_edge.source_label_version_id,
        "target_label_ids": [target.target_label_id for target in item.targets],
        "target_label_version_id": edge.compiled_edge.target_label_version_id,
    }


def _single_target_label_id(
    edge: BundleEdgeSnapshot,
    item: CompiledMappingItem,
) -> str:
    if len(item.targets) != 1:
        _raise(
            "LABEL_MAPPING_BUNDLE_PATH_DISCONNECTED",
            "a continuing mapping path must have exactly one target label",
            details={
                "mapping_version_id": edge.mapping_version_id,
                "relation": item.relation.value,
                "source_label_id": item.source_label_id,
            },
        )
    return item.targets[0].target_label_id


def _path_for_metric(
    *,
    source_version: LabelVersionSnapshot,
    source_label_id: str,
    target_version: LabelVersionSnapshot,
    route: tuple[BundleEdgeSnapshot, ...],
    metric_family: str,
) -> CompiledBundlePath:
    current_label_id = source_label_id
    relation_path: list[dict[str, Any]] = []
    mapping_version_ids: list[str] = []
    comparability_status = "comparable"
    metric_configuration: tuple[str, str, str] | None = None
    target_label_id: str | None = None
    requires_recompute = False
    coverage_gap = False
    stopped = False

    for edge in route:
        indexed = {item.source_label_id: item for item in edge.compiled_edge.items}
        item = indexed.get(current_label_id)
        if item is None:
            _raise(
                "LABEL_MAPPING_BUNDLE_PATH_DISCONNECTED",
                "an intermediate target label has no disposition in the next edge",
                details={
                    "mapping_version_id": edge.mapping_version_id,
                    "source_label_id": current_label_id,
                    "source_label_version_id": edge.compiled_edge.source_label_version_id,
                },
            )
        relation_path.append(_relation_step(edge, item))
        mapping_version_ids.append(edge.mapping_version_id)
        comparability_status = _worst_comparability(
            comparability_status,
            item.comparability_status.value,
        )

        if item.relation is MappingRelation.RETIRE:
            target_label_id = None
            comparability_status = "structural-break"
            coverage_gap = True
            stopped = True
            break
        if item.relation is MappingRelation.SPLIT_RECOMPUTE:
            target_label_id = None
            comparability_status = "structural-break"
            requires_recompute = True
            stopped = True
            break
        if item.relation is MappingRelation.MERGE:
            if metric_family not in item.allowed_metric_families:
                target_label_id = None
                comparability_status = "structural-break"
                coverage_gap = True
                stopped = True
                relation_path[-1] = {
                    **relation_path[-1],
                    "path_outcome": "metric-family-not-approved",
                }
                break
            assert item.metric_grain is not None
            assert item.lineage_key is not None
            assert item.reducer is not None
            candidate_configuration = (
                item.metric_grain,
                item.lineage_key,
                item.reducer,
            )
            if metric_configuration is not None and metric_configuration != candidate_configuration:
                _raise(
                    "LABEL_MAPPING_BUNDLE_REDUCER_CONFLICT",
                    "multi-hop merge path has incompatible metric reducer contracts",
                    details={
                        "mapping_version_id": edge.mapping_version_id,
                        "metric_family": metric_family,
                        "source_label_id": source_label_id,
                    },
                )
            metric_configuration = candidate_configuration

        current_label_id = _single_target_label_id(edge, item)
        target_label_id = current_label_id

    if not stopped:
        active_target_ids = {
            item.label_id for item in target_version.items if item.status == "active"
        }
        if target_label_id is None:
            target_label_id = current_label_id
        if target_label_id not in active_target_ids:
            _raise(
                "LABEL_MAPPING_BUNDLE_PATH_DISCONNECTED",
                "compiled path does not terminate at an active label in the final target",
                details={
                    "source_label_id": source_label_id,
                    "source_label_version_id": source_version.label_version_id,
                    "target_label_id": target_label_id,
                    "target_label_version_id": target_version.label_version_id,
                },
            )

    metric_grain: str | None = None
    lineage_key: str | None = None
    reducer: str | None = None
    if metric_configuration is not None:
        metric_grain, lineage_key, reducer = metric_configuration
    path_document = {
        "comparability_status": comparability_status,
        "coverage_gap": coverage_gap,
        "lineage_key": lineage_key,
        "mapping_version_ids": mapping_version_ids,
        "metric_family": metric_family,
        "metric_grain": metric_grain,
        "reducer": reducer,
        "relation_path": relation_path,
        "requires_recompute": requires_recompute,
        "schema_version": "auris.label-mapping-bundle-path/1",
        "source_label_id": source_label_id,
        "source_label_version_id": source_version.label_version_id,
        "target_label_id": target_label_id,
        "target_label_version_id": target_version.label_version_id,
    }
    return CompiledBundlePath(
        source_label_version_id=source_version.label_version_id,
        target_label_version_id=target_version.label_version_id,
        source_label_id=source_label_id,
        target_label_id=target_label_id,
        metric_family=metric_family,
        relation_path=tuple(relation_path),
        mapping_version_ids=tuple(mapping_version_ids),
        metric_grain=metric_grain,
        lineage_key=lineage_key,
        reducer=reducer,
        comparability_status=comparability_status,
        requires_recompute=requires_recompute,
        coverage_gap=coverage_gap,
        path_sha256=sha256_document(path_document),
    )


def _compile_paths(
    request: BundleCompileInput,
    routes: dict[str, tuple[BundleEdgeSnapshot, ...]],
    registry: MetricCompatibilityRegistry,
) -> tuple[CompiledBundlePath, ...]:
    metric_families = sorted(rule.metric_family for rule in registry.rules)
    paths: list[CompiledBundlePath] = []
    for source_version in sorted(
        request.source_versions,
        key=lambda version: version.label_version_id,
    ):
        route = routes[source_version.label_version_id]
        active_source_items = sorted(
            (item for item in source_version.items if item.status == "active"),
            key=lambda item: item.label_id,
        )
        for item in active_source_items:
            for metric_family in metric_families:
                paths.append(
                    _path_for_metric(
                        source_version=source_version,
                        source_label_id=item.label_id,
                        target_version=request.target_version,
                        route=route,
                        metric_family=metric_family,
                    )
                )
    return tuple(paths)


def compile_bundle(
    request: BundleCompileInput,
    *,
    registry: MetricCompatibilityRegistry = DEFAULT_METRIC_COMPATIBILITY_REGISTRY,
) -> CompiledBundle:
    """Compile a deterministic, immutable multi-edge normalization closure."""

    _validate_scope(request)
    _validate_edges(request)
    outgoing = _outgoing_edges(request.edges)
    _reject_cycles(outgoing)
    routes = _routes_to_target(request, outgoing)
    sources, source_manifest_sha256 = _compile_sources(request)
    members = _compile_members(request, outgoing)
    paths = _compile_paths(request, routes, registry)
    canonical_manifest: dict[str, Any] = {
        "compiler_version": BUNDLE_COMPILER_VERSION,
        "members": [member.to_document() for member in members],
        "metric_registry_version": registry.version,
        "paths": [path.to_document() for path in paths],
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "scope": {
            "project_id": request.project_id,
            "tenant_id": request.tenant_id,
        },
        "source_manifest_sha256": source_manifest_sha256,
        "sources": [source.to_document() for source in sources],
        "target_label_version": {
            "content_sha256": request.target_version.content_sha256,
            "label_version_id": request.target_version.label_version_id,
            "resource_version": request.target_version.resource_version,
            "taxonomy_id": request.target_version.taxonomy_id,
        },
        "taxonomy_id": request.taxonomy_id,
    }
    content_sha256 = sha256_document(canonical_manifest)
    return CompiledBundle(
        compiler_version=BUNDLE_COMPILER_VERSION,
        metric_registry_version=registry.version,
        source_label_version_ids=tuple(source.source_label_version_id for source in sources),
        target_label_version_id=request.target_version.label_version_id,
        source_manifest_sha256=source_manifest_sha256,
        sources=sources,
        members=members,
        paths=paths,
        canonical_manifest=canonical_manifest,
        canonical_manifest_sha256=content_sha256,
        content_sha256=content_sha256,
    )
