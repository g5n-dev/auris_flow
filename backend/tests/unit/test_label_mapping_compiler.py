from __future__ import annotations

from dataclasses import replace

import pytest

from app.domain.label_mapping import (
    EdgeCompileInput,
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
    compile_edge,
)


def _item(
    label_id: str,
    *,
    canonical_name: str | None = None,
    aliases: tuple[str, ...] = (),
    value_type: str = "boolean",
    risk_level: str = "low",
    parent_ids: tuple[str, ...] = (),
    aggregation_rule: dict[str, object] | None = None,
    status: str = "active",
) -> LabelItemSnapshot:
    return LabelItemSnapshot(
        label_id=label_id,
        canonical_name=canonical_name or label_id,
        aliases=aliases,
        value_type=value_type,
        risk_level=risk_level,
        mutual_exclusion_group=None,
        parent_ids=parent_ids,
        aggregation_rule=aggregation_rule or {"mode": "presence"},
        status=status,
    )


def _version(
    label_version_id: str,
    items: tuple[LabelItemSnapshot, ...],
    *,
    tenant_id: str = "tenant_a",
    project_id: str = "project_a",
    taxonomy_id: str = "taxonomy_a",
    resource_version: int = 1,
    artifact_status: str = "published",
    content_sha256: str = "a" * 64,
) -> LabelVersionSnapshot:
    return LabelVersionSnapshot(
        tenant_id=tenant_id,
        project_id=project_id,
        taxonomy_id=taxonomy_id,
        label_version_id=label_version_id,
        resource_version=resource_version,
        artifact_status=artifact_status,
        content_sha256=content_sha256,
        items=items,
    )


def _compile_input(
    source: LabelVersionSnapshot,
    target: LabelVersionSnapshot,
    *dispositions: MappingIntent,
    expected_source_resource_version: int | None = None,
    expected_target_resource_version: int | None = None,
) -> EdgeCompileInput:
    return EdgeCompileInput(
        mapping_version="1.0.0",
        source=source,
        target=target,
        expected_source_resource_version=(
            source.resource_version
            if expected_source_resource_version is None
            else expected_source_resource_version
        ),
        expected_target_resource_version=(
            target.resource_version
            if expected_target_resource_version is None
            else expected_target_resource_version
        ),
        dispositions=dispositions,
    )


def _deterministic_fixture(*, reverse: bool = False) -> EdgeCompileInput:
    source_items: tuple[LabelItemSnapshot, ...] = (
        _item("label_identity", aliases=("stable-b", "stable-a")),
        _item("label_rename", canonical_name="旧名称", aliases=("旧别名",)),
        _item("label_replace", value_type="categorical"),
        _item("label_merge_a", parent_ids=("root-b", "root-a")),
        _item("label_merge_b", parent_ids=("root-a", "root-b")),
        _item("label_split"),
        _item("label_retire"),
    )
    target_items: tuple[LabelItemSnapshot, ...] = (
        _item("label_identity", aliases=("stable-a", "stable-b")),
        _item("label_rename", canonical_name="新名称", aliases=("新别名",)),
        _item("label_replacement", value_type="categorical"),
        _item("label_merged"),
        _item("label_split_a"),
        _item("label_split_b"),
    )
    dispositions: tuple[MappingIntent, ...] = (
        IdentityIntent("label_identity", "label_identity"),
        RenameIntent("label_rename", "label_rename"),
        ReplaceIntent("label_replace", "label_replacement"),
        MergeIntent(
            source_label_ids=("label_merge_b", "label_merge_a"),
            target_label_id="label_merged",
            allowed_metric_families=("presence", "distinct-count"),
            metric_grain="business-event",
            lineage_key="event_id",
            reducer="presence-any",
        ),
        SplitRecomputeIntent(
            "label_split",
            target_label_ids=("label_split_b", "label_split_a"),
            requires_recompute=True,
        ),
        RetireIntent("label_retire"),
    )
    if reverse:
        source_items = tuple(reversed(source_items))
        target_items = tuple(reversed(target_items))
        dispositions = tuple(reversed(dispositions))
        merge = next(value for value in dispositions if isinstance(value, MergeIntent))
        split = next(value for value in dispositions if isinstance(value, SplitRecomputeIntent))
        dispositions = tuple(
            replace(
                value,
                source_label_ids=tuple(reversed(merge.source_label_ids)),
                allowed_metric_families=tuple(reversed(merge.allowed_metric_families)),
            )
            if value is merge
            else replace(value, target_label_ids=tuple(reversed(split.target_label_ids)))
            if value is split
            else value
            for value in dispositions
        )
    source = _version("lv_source", source_items, resource_version=7, content_sha256="a" * 64)
    target = _version("lv_target", target_items, resource_version=11, content_sha256="b" * 64)
    return _compile_input(source, target, *dispositions)


def test_edge_hash_is_invariant_to_item_target_and_json_order() -> None:
    forward = compile_edge(_deterministic_fixture())
    backward = compile_edge(_deterministic_fixture(reverse=True))

    assert forward.content_sha256 == backward.content_sha256
    assert forward.canonical_manifest == backward.canonical_manifest
    assert [item.source_label_id for item in forward.items] == sorted(
        item.source_label_id for item in forward.items
    )
    split = next(item for item in forward.items if item.relation is MappingRelation.SPLIT_RECOMPUTE)
    assert [target.target_label_id for target in split.targets] == [
        "label_split_a",
        "label_split_b",
    ]


def test_every_active_source_item_requires_one_disposition() -> None:
    source = _version("lv_source", (_item("mapped"), _item("missing")))
    target = _version("lv_target", (_item("mapped"),), content_sha256="b" * 64)

    with pytest.raises(MappingCompileError) as caught:
        compile_edge(_compile_input(source, target, IdentityIntent("mapped", "mapped")))

    assert caught.value.code == "LABEL_MAPPING_COVERAGE_GAP"
    assert caught.value.details["unmapped_source_label_ids"] == ["missing"]


def test_same_active_source_cannot_have_multiple_dispositions() -> None:
    source = _version("lv_source", (_item("label_a"),))
    target = _version(
        "lv_target",
        (_item("label_a"), _item("label_b")),
        content_sha256="b" * 64,
    )

    with pytest.raises(MappingCompileError) as caught:
        compile_edge(
            _compile_input(
                source,
                target,
                IdentityIntent("label_a", "label_a"),
                ReplaceIntent("label_a", "label_b"),
            )
        )

    assert caught.value.code == "LABEL_MAPPING_AMBIGUOUS"
    assert caught.value.path == "dispositions.label_a"


def test_identity_requires_the_same_label_and_unchanged_display_definition() -> None:
    source = _version("lv_source", (_item("label_a", canonical_name="旧名称"),))
    target = _version(
        "lv_target",
        (_item("label_a", canonical_name="新名称"),),
        content_sha256="b" * 64,
    )

    with pytest.raises(MappingCompileError) as caught:
        compile_edge(_compile_input(source, target, IdentityIntent("label_a", "label_a")))

    assert caught.value.code == "LABEL_MAPPING_RELATION_MISMATCH"
    assert caught.value.details["suggested_relation"] == "rename"


def test_rename_is_exact_only_when_non_display_semantics_are_unchanged() -> None:
    source = _version("lv_source", (_item("label_a", canonical_name="旧名称"),))
    target = _version(
        "lv_target",
        (_item("label_a", canonical_name="新名称", value_type="categorical"),),
        content_sha256="b" * 64,
    )

    with pytest.raises(MappingCompileError) as caught:
        compile_edge(_compile_input(source, target, RenameIntent("label_a", "label_a")))

    assert caught.value.code == "LABEL_MAPPING_SEMANTIC_HASH_CHANGED"


def test_valid_rename_compiles_to_exact_and_comparable() -> None:
    source = _version("lv_source", (_item("label_a", canonical_name="旧名称"),))
    target = _version(
        "lv_target",
        (_item("label_a", canonical_name="新名称"),),
        content_sha256="b" * 64,
    )

    result = compile_edge(_compile_input(source, target, RenameIntent("label_a", "label_a")))

    assert result.items[0].compatibility is MappingCompatibility.EXACT
    assert result.items[0].comparability_status.value == "comparable"


def test_one_to_one_replace_defaults_to_structural_break() -> None:
    source = _version("lv_source", (_item("label_old"),))
    target = _version("lv_target", (_item("label_new"),), content_sha256="b" * 64)

    result = compile_edge(_compile_input(source, target, ReplaceIntent("label_old", "label_new")))

    assert result.items[0].compatibility is MappingCompatibility.STRUCTURAL_BREAK
    assert result.items[0].comparability_status.value == "structural-break"


def test_replace_cannot_claim_exact_without_immutable_evidence() -> None:
    source = _version("lv_source", (_item("label_old"),))
    target = _version("lv_target", (_item("label_new"),), content_sha256="b" * 64)

    with pytest.raises(MappingCompileError) as caught:
        compile_edge(
            _compile_input(
                source,
                target,
                ReplaceIntent(
                    "label_old",
                    "label_new",
                    compatibility=MappingCompatibility.EXACT,
                ),
            )
        )

    assert caught.value.code == "LABEL_MAPPING_COMPATIBILITY_EVIDENCE_REQUIRED"


@pytest.mark.parametrize(
    ("metric_grain", "lineage_key", "reducer"),
    [
        (None, "event_id", "presence-any"),
        ("business-event", None, "presence-any"),
        ("business-event", "event_id", None),
    ],
)
def test_merge_requires_explicit_metric_grain_lineage_and_reducer(
    metric_grain: str | None,
    lineage_key: str | None,
    reducer: str | None,
) -> None:
    source = _version("lv_source", (_item("label_a"), _item("label_b")))
    target = _version("lv_target", (_item("label_merged"),), content_sha256="b" * 64)

    with pytest.raises(MappingCompileError) as caught:
        compile_edge(
            _compile_input(
                source,
                target,
                MergeIntent(
                    source_label_ids=("label_a", "label_b"),
                    target_label_id="label_merged",
                    allowed_metric_families=("presence",),
                    metric_grain=metric_grain,
                    lineage_key=lineage_key,
                    reducer=reducer,
                ),
            )
        )

    assert caught.value.code == "LABEL_MAPPING_REDUCER_REQUIRED"


def test_merge_rejects_subject_only_or_unregistered_reducer_rules() -> None:
    source = _version("lv_source", (_item("label_a"), _item("label_b")))
    target = _version("lv_target", (_item("label_merged"),), content_sha256="b" * 64)

    for lineage_key, reducer in (("subject_id", "presence-any"), ("event_id", "sum")):
        with pytest.raises(MappingCompileError) as caught:
            compile_edge(
                _compile_input(
                    source,
                    target,
                    MergeIntent(
                        source_label_ids=("label_a", "label_b"),
                        target_label_id="label_merged",
                        allowed_metric_families=("presence",),
                        metric_grain="business-event",
                        lineage_key=lineage_key,
                        reducer=reducer,
                    ),
                )
            )
        assert caught.value.code == "LABEL_MAPPING_REDUCER_REQUIRED"


def test_valid_merge_expands_to_one_metric_dependent_item_per_source() -> None:
    source = _version("lv_source", (_item("label_b"), _item("label_a")))
    target = _version("lv_target", (_item("label_merged"),), content_sha256="b" * 64)

    result = compile_edge(
        _compile_input(
            source,
            target,
            MergeIntent(
                source_label_ids=("label_b", "label_a"),
                target_label_id="label_merged",
                allowed_metric_families=("presence", "distinct-count"),
                metric_grain="business-event",
                lineage_key="event_id",
                reducer="presence-any",
            ),
        )
    )

    assert [item.source_label_id for item in result.items] == ["label_a", "label_b"]
    assert all(item.compatibility is MappingCompatibility.METRIC_DEPENDENT for item in result.items)
    assert result.items[0].merge_group_sha256 == result.items[1].merge_group_sha256
    assert result.coverage.metric_dependent_count == 2


@pytest.mark.parametrize(
    "intent",
    [
        SplitRecomputeIntent(
            "label_old",
            target_label_ids=("label_a", "label_b"),
            requires_recompute=False,
        ),
        SplitRecomputeIntent(
            "label_old",
            target_label_ids=("label_a",),
            requires_recompute=True,
        ),
        SplitRecomputeIntent(
            "label_old",
            target_label_ids=("label_a", "label_b"),
            requires_recompute=True,
            allocation_weights=(50, 50),
        ),
        SplitRecomputeIntent(
            "label_old",
            target_label_ids=("label_a", "label_b"),
            requires_recompute=True,
            copy_existing_facts=True,
        ),
    ],
)
def test_split_requires_recompute_and_forbids_fact_distribution(
    intent: SplitRecomputeIntent,
) -> None:
    source = _version("lv_source", (_item("label_old"),))
    target = _version(
        "lv_target",
        (_item("label_a"), _item("label_b")),
        content_sha256="b" * 64,
    )

    with pytest.raises(MappingCompileError) as caught:
        compile_edge(_compile_input(source, target, intent))

    assert caught.value.code == "LABEL_MAPPING_RECOMPUTE_REQUIRED"


def test_valid_split_is_structural_break_and_not_normalizable() -> None:
    source = _version("lv_source", (_item("label_old"),))
    target = _version(
        "lv_target",
        (_item("label_b"), _item("label_a")),
        content_sha256="b" * 64,
    )

    result = compile_edge(
        _compile_input(
            source,
            target,
            SplitRecomputeIntent(
                "label_old",
                target_label_ids=("label_b", "label_a"),
                requires_recompute=True,
            ),
        )
    )

    assert result.items[0].requires_recompute is True
    assert result.items[0].target_semantic_sha256 is None
    assert result.coverage.normalizable_count == 0
    assert result.coverage.recompute_required_source_label_ids == ("label_old",)


def test_retire_rejects_target_and_reports_coverage_gap() -> None:
    source = _version("lv_source", (_item("label_old"),))
    target = _version("lv_target", (_item("label_new"),), content_sha256="b" * 64)

    with pytest.raises(MappingCompileError) as caught:
        compile_edge(
            _compile_input(source, target, RetireIntent("label_old", target_label_id="label_new"))
        )
    assert caught.value.code == "LABEL_MAPPING_RETIRE_TARGET_FORBIDDEN"

    result = compile_edge(_compile_input(source, target, RetireIntent("label_old")))
    assert result.items[0].targets == ()
    assert result.coverage.disposition_count == 1
    assert result.coverage.normalizable_count == 0
    assert result.coverage.coverage_gap_source_label_ids == ("label_old",)


@pytest.mark.parametrize(
    ("source_change", "target_change", "expected_source", "expected_target", "code"),
    [
        ({"tenant_id": "tenant_b"}, {}, None, None, "LABEL_MAPPING_SCOPE_MISMATCH"),
        ({}, {"project_id": "project_b"}, None, None, "LABEL_MAPPING_SCOPE_MISMATCH"),
        ({}, {"taxonomy_id": "taxonomy_b"}, None, None, "LABEL_MAPPING_TAXONOMY_MISMATCH"),
        ({}, {}, 99, None, "LABEL_MAPPING_RESOURCE_VERSION_CONFLICT"),
        ({}, {}, None, 99, "LABEL_MAPPING_RESOURCE_VERSION_CONFLICT"),
    ],
)
def test_scope_taxonomy_and_resource_version_drift_are_rejected(
    source_change: dict[str, str],
    target_change: dict[str, str],
    expected_source: int | None,
    expected_target: int | None,
    code: str,
) -> None:
    source = _version(
        "lv_source",
        (_item("label_a"),),
        tenant_id=source_change.get("tenant_id", "tenant_a"),
        project_id=source_change.get("project_id", "project_a"),
        taxonomy_id=source_change.get("taxonomy_id", "taxonomy_a"),
    )
    target = _version(
        "lv_target",
        (_item("label_a"),),
        tenant_id=target_change.get("tenant_id", "tenant_a"),
        project_id=target_change.get("project_id", "project_a"),
        taxonomy_id=target_change.get("taxonomy_id", "taxonomy_a"),
        content_sha256="b" * 64,
    )

    with pytest.raises(MappingCompileError) as caught:
        compile_edge(
            _compile_input(
                source,
                target,
                IdentityIntent("label_a", "label_a"),
                expected_source_resource_version=expected_source,
                expected_target_resource_version=expected_target,
            )
        )

    assert caught.value.code == code


def test_non_finite_label_definition_is_not_canonical_json() -> None:
    source = _version(
        "lv_source",
        (_item("label_a", aggregation_rule={"threshold": float("nan")}),),
    )
    target = _version("lv_target", (_item("label_a"),), content_sha256="b" * 64)

    with pytest.raises(MappingCompileError) as caught:
        compile_edge(_compile_input(source, target, IdentityIntent("label_a", "label_a")))

    assert caught.value.code == "LABEL_MAPPING_CANONICAL_JSON_INVALID"
