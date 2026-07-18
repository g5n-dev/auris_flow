from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.models import (
    Base,
    LabelMappingBundle,
    LabelMappingBundleMember,
    LabelMappingBundlePath,
    LabelMappingBundleSource,
    LabelMappingItem,
    LabelMappingItemTarget,
    LabelMappingVersion,
    ReleaseBundleHeadEvent,
)


def _constraint_names(model: type[object], kind: type[object]) -> set[str]:
    return {
        constraint.name
        for constraint in model.__table__.constraints
        if isinstance(constraint, kind) and constraint.name is not None
    }


def test_lifecycle_mapping_models_cover_expand_migration_tables() -> None:
    expected = {
        "label_mapping_versions",
        "label_mapping_items",
        "label_mapping_item_targets",
        "label_mapping_bundles",
        "label_mapping_bundle_sources",
        "label_mapping_bundle_members",
        "label_mapping_bundle_paths",
        "release_bundle_head_events",
    }
    assert expected <= set(Base.metadata.tables)
    assert {
        model.__tablename__
        for model in (
            LabelMappingVersion,
            LabelMappingItem,
            LabelMappingItemTarget,
            LabelMappingBundle,
            LabelMappingBundleSource,
            LabelMappingBundleMember,
            LabelMappingBundlePath,
            ReleaseBundleHeadEvent,
        )
    } == expected


def test_mapping_models_keep_scope_hash_and_edge_binding_constraints() -> None:
    assert "uq_label_mapping_versions_scope_edge_binding" in _constraint_names(
        LabelMappingVersion,
        UniqueConstraint,
    )
    assert "fk_label_mapping_bundle_members_scope_edge" in _constraint_names(
        LabelMappingBundleMember,
        ForeignKeyConstraint,
    )
    assert "fk_label_mapping_bundle_paths_scope_bundle_target" in _constraint_names(
        LabelMappingBundlePath,
        ForeignKeyConstraint,
    )
    assert "ck_label_mapping_items_split_recompute" in _constraint_names(
        LabelMappingItem,
        CheckConstraint,
    )


def test_activation_ledger_model_binds_predecessor_and_completion_receipt() -> None:
    columns = ReleaseBundleHeadEvent.__table__.columns
    assert {
        "generation",
        "previous_generation",
        "activation_status",
        "effective_from",
        "effective_to",
        "command_id",
        "completion_receipt_id",
        "content_sha256",
        "root_trace_id",
        "trace_id",
    } <= set(columns.keys())
    assert "ck_release_bundle_head_events_previous_generation" in _constraint_names(
        ReleaseBundleHeadEvent,
        CheckConstraint,
    )
    assert "fk_release_head_events_scope_receipt" in _constraint_names(
        ReleaseBundleHeadEvent,
        ForeignKeyConstraint,
    )
