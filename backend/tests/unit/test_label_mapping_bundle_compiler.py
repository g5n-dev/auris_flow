from __future__ import annotations

from dataclasses import replace

import pytest

from app.domain.label_mapping import (
    CompatibilityEvidence,
    EdgeCompileInput,
    IdentityIntent,
    LabelItemSnapshot,
    LabelVersionSnapshot,
    MergeIntent,
    ReplaceIntent,
    RetireIntent,
    SplitRecomputeIntent,
    compile_edge,
)
from app.domain.label_mapping.bundle_compiler import (
    BundleCompileError,
    BundleCompileInput,
    BundleEdgeSnapshot,
    compile_bundle,
)


def _item(label_id: str) -> LabelItemSnapshot:
    return LabelItemSnapshot(
        label_id=label_id,
        canonical_name=label_id,
        aliases=(),
        value_type="boolean",
        risk_level="low",
        mutual_exclusion_group=None,
        parent_ids=(),
        aggregation_rule={"mode": "presence"},
    )


def _version(label_version_id: str, *label_ids: str) -> LabelVersionSnapshot:
    digest_character = chr(ord("a") + len(label_version_id) % 6)
    return LabelVersionSnapshot(
        tenant_id="tenant_bundle",
        project_id="project_bundle",
        taxonomy_id="taxonomy_bundle",
        label_version_id=label_version_id,
        resource_version=1,
        artifact_status="published",
        content_sha256=digest_character * 64,
        items=tuple(_item(label_id) for label_id in label_ids),
    )


def _edge(
    mapping_version_id: str,
    source: LabelVersionSnapshot,
    target: LabelVersionSnapshot,
    *intents,
) -> BundleEdgeSnapshot:
    compiled = compile_edge(
        EdgeCompileInput(
            mapping_version=f"version-{mapping_version_id}",
            source=source,
            target=target,
            expected_source_resource_version=source.resource_version,
            expected_target_resource_version=target.resource_version,
            dispositions=intents,
        )
    )
    return BundleEdgeSnapshot(
        mapping_version_id=mapping_version_id,
        mapping_resource_version=3,
        compiled_edge=compiled,
    )


def _compile_input(
    sources: tuple[LabelVersionSnapshot, ...],
    target: LabelVersionSnapshot,
    edges: tuple[BundleEdgeSnapshot, ...],
) -> BundleCompileInput:
    return BundleCompileInput(
        tenant_id="tenant_bundle",
        project_id="project_bundle",
        taxonomy_id="taxonomy_bundle",
        source_versions=sources,
        target_version=target,
        edges=edges,
    )


def test_direct_bundle_freezes_one_path_per_source_label_and_metric_family() -> None:
    source = _version("lv_v1", "label_a")
    target = _version("lv_v2", "label_a")
    edge = _edge("mapping_v1_v2", source, target, IdentityIntent("label_a", "label_a"))

    compiled = compile_bundle(_compile_input((source,), target, (edge,)))

    assert compiled.source_label_version_ids == ("lv_v1",)
    assert compiled.target_label_version_id == "lv_v2"
    assert [path.metric_family for path in compiled.paths] == ["distinct-count", "presence"]
    assert {path.target_label_id for path in compiled.paths} == {"label_a"}
    assert {path.mapping_version_ids for path in compiled.paths} == {("mapping_v1_v2",)}
    assert {path.comparability_status for path in compiled.paths} == {"comparable"}
    assert compiled.content_sha256 == compiled.canonical_manifest_sha256


def test_multi_source_bundle_is_deterministic_under_input_order() -> None:
    source_a = _version("lv_a", "label_a")
    source_b = _version("lv_b", "label_b")
    target = _version("lv_target", "label_a", "label_b")
    edge_a = _edge(
        "mapping_a_target",
        source_a,
        target,
        IdentityIntent("label_a", "label_a"),
    )
    edge_b = _edge(
        "mapping_b_target",
        source_b,
        target,
        IdentityIntent("label_b", "label_b"),
    )

    forward = compile_bundle(_compile_input((source_a, source_b), target, (edge_a, edge_b)))
    reverse = compile_bundle(_compile_input((source_b, source_a), target, (edge_b, edge_a)))

    assert forward.content_sha256 == reverse.content_sha256
    assert forward.canonical_manifest == reverse.canonical_manifest
    assert forward.source_label_version_ids == ("lv_a", "lv_b")
    assert len(forward.paths) == 4


def test_multi_hop_path_freezes_every_edge_in_order() -> None:
    version_1 = _version("lv_v1", "label_a")
    version_2 = _version("lv_v2", "label_a")
    version_3 = _version("lv_v3", "label_a")
    edge_12 = _edge(
        "mapping_v1_v2",
        version_1,
        version_2,
        IdentityIntent("label_a", "label_a"),
    )
    edge_23 = _edge(
        "mapping_v2_v3",
        version_2,
        version_3,
        IdentityIntent("label_a", "label_a"),
    )

    compiled = compile_bundle(_compile_input((version_1,), version_3, (edge_23, edge_12)))

    assert [member.mapping_version_id for member in compiled.members] == [
        "mapping_v1_v2",
        "mapping_v2_v3",
    ]
    assert all(
        path.mapping_version_ids == ("mapping_v1_v2", "mapping_v2_v3") for path in compiled.paths
    )
    assert all(len(path.relation_path) == 2 for path in compiled.paths)


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("cycle", "LABEL_MAPPING_BUNDLE_CYCLE"),
        ("ambiguous", "LABEL_MAPPING_BUNDLE_AMBIGUOUS"),
        ("disconnected", "LABEL_MAPPING_BUNDLE_DISCONNECTED"),
    ],
)
def test_invalid_version_graph_fails_closed(case: str, expected_code: str) -> None:
    version_a = _version("lv_a", "label_a")
    version_b = _version("lv_b", "label_a")
    version_c = _version("lv_c", "label_a")
    edge_ab = _edge(
        "mapping_a_b",
        version_a,
        version_b,
        IdentityIntent("label_a", "label_a"),
    )
    edge_ac = _edge(
        "mapping_a_c",
        version_a,
        version_c,
        IdentityIntent("label_a", "label_a"),
    )
    edge_ba = _edge(
        "mapping_b_a",
        version_b,
        version_a,
        IdentityIntent("label_a", "label_a"),
    )
    if case == "cycle":
        request = _compile_input((version_a,), version_b, (edge_ab, edge_ba))
    elif case == "ambiguous":
        request = _compile_input((version_a,), version_c, (edge_ab, edge_ac))
    else:
        request = _compile_input((version_a,), version_c, (edge_ab,))

    with pytest.raises(BundleCompileError) as caught:
        compile_bundle(request)

    assert caught.value.code == expected_code


def test_merge_path_freezes_approved_metric_reducer_contract() -> None:
    source = _version("lv_v1", "label_a", "label_b")
    target = _version("lv_v2", "label_merged")
    edge = _edge(
        "mapping_merge",
        source,
        target,
        MergeIntent(
            source_label_ids=("label_a", "label_b"),
            target_label_id="label_merged",
            allowed_metric_families=("presence", "distinct-count"),
            metric_grain="business-event",
            lineage_key="event_id",
            reducer="presence-any",
        ),
    )

    compiled = compile_bundle(_compile_input((source,), target, (edge,)))

    assert len(compiled.paths) == 4
    assert {path.target_label_id for path in compiled.paths} == {"label_merged"}
    assert {path.metric_grain for path in compiled.paths} == {"business-event"}
    assert {path.lineage_key for path in compiled.paths} == {"event_id"}
    assert {path.reducer for path in compiled.paths} == {"presence-any"}
    assert {path.comparability_status for path in compiled.paths} == {"partial"}


def test_split_and_retire_end_paths_without_inventing_target_mappings() -> None:
    source = _version("lv_v1", "label_split", "label_retire")
    target = _version("lv_v2", "label_split_a", "label_split_b")
    edge = _edge(
        "mapping_breaks",
        source,
        target,
        SplitRecomputeIntent(
            source_label_id="label_split",
            target_label_ids=("label_split_a", "label_split_b"),
            requires_recompute=True,
        ),
        RetireIntent(source_label_id="label_retire"),
    )

    compiled = compile_bundle(_compile_input((source,), target, (edge,)))
    split_paths = [path for path in compiled.paths if path.source_label_id == "label_split"]
    retire_paths = [path for path in compiled.paths if path.source_label_id == "label_retire"]

    assert split_paths and all(path.target_label_id is None for path in split_paths)
    assert all(path.requires_recompute for path in split_paths)
    assert all(not path.coverage_gap for path in split_paths)
    assert retire_paths and all(path.target_label_id is None for path in retire_paths)
    assert all(not path.requires_recompute for path in retire_paths)
    assert all(path.coverage_gap for path in retire_paths)
    assert {path.comparability_status for path in compiled.paths} == {"structural-break"}


def test_dynamic_latest_reference_and_exact_replace_without_evidence_fail_closed() -> None:
    source = _version("lv_v1", "label_old")
    target = _version("lv_v2", "label_new")
    evidence = CompatibilityEvidence(
        evidence_type="compatibility-evaluation",
        evidence_id="evaluation-v1-v2",
        resource_version=1,
        content_sha256="e" * 64,
    )
    edge = _edge(
        "mapping_replace",
        source,
        target,
        ReplaceIntent(
            source_label_id="label_old",
            target_label_id="label_new",
            compatibility="exact",
            compatibility_evidence=evidence,
        ),
    )

    dynamic_source = replace(source, label_version_id="latest")
    with pytest.raises(BundleCompileError) as dynamic_error:
        compile_bundle(_compile_input((dynamic_source,), target, (edge,)))
    assert dynamic_error.value.code == "LABEL_MAPPING_BUNDLE_DYNAMIC_REFERENCE"

    unsafe_item = replace(edge.compiled_edge.items[0], compatibility_evidence=None)
    unsafe_compiled_edge = replace(edge.compiled_edge, items=(unsafe_item,))
    unsafe_edge = replace(edge, compiled_edge=unsafe_compiled_edge)
    with pytest.raises(BundleCompileError) as evidence_error:
        compile_bundle(_compile_input((source,), target, (unsafe_edge,)))
    assert evidence_error.value.code == "LABEL_MAPPING_BUNDLE_EXACT_EVIDENCE_REQUIRED"
