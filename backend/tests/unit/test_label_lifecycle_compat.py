from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models import LabelVersion
from app.services.label_lifecycle_compat_service import (
    LabelLifecycleDriftError,
    apply_label_version_lifecycle_fields,
    compatible_label_version_data,
    derive_label_version_lifecycle_fields,
    label_version_lifecycle_shadow_compare,
    transition_label_version_artifact,
)


def test_derive_lifecycle_fields_maps_only_deterministic_legacy_values() -> None:
    derived = derive_label_version_lifecycle_fields(
        {
            "taxonomy_id": "taxonomy_sales",
            "version": "v2.1.0",
            "parent_label_version_id": "lv_v2_0_0",
            "status": "gray_releasing",
            "published_at": "2026-07-18T04:30:00Z",
            "content_sha256": "a" * 64,
        }
    )

    assert derived.values == {
        "taxonomy_id": "taxonomy_sales",
        "semantic_version": "v2.1.0",
        "base_label_version_id": "lv_v2_0_0",
        "artifact_status": "published",
        "artifact_published_at": datetime(2026, 7, 18, 4, 30, tzinfo=UTC),
        "content_sha256": "a" * 64,
    }
    assert derived.migration_required == ()


def test_derive_lifecycle_fields_does_not_guess_ambiguous_shadow_state() -> None:
    derived = derive_label_version_lifecycle_fields(
        {"label_version_id": "lv_shadow", "version": "v3-rc1", "status": "shadow"}
    )

    assert derived.values == {"semantic_version": "v3-rc1"}
    assert set(derived.migration_required) == {
        "artifact_status",
        "content_sha256",
        "taxonomy_id",
    }


def test_dual_write_rejects_conflicting_strong_field_instead_of_overwriting() -> None:
    record = LabelVersion(
        label_version_id="lv_conflict",
        tenant_id="tenant_a",
        project_id="project_a",
        status="draft",
        resource_version=1,
        taxonomy_id="taxonomy_a",
        trace_id="trace_conflict",
        payload={},
    )

    with pytest.raises(LabelLifecycleDriftError, match="taxonomy_id"):
        apply_label_version_lifecycle_fields(
            record,
            {
                "taxonomy_id": "taxonomy_b",
                "version": "v1.0.0",
                "status": "draft",
                "content_sha256": "b" * 64,
            },
            conflict_policy="raise",
        )

    assert record.taxonomy_id == "taxonomy_a"


def test_authorized_artifact_transition_sets_timestamp_and_blocks_regression() -> None:
    record = LabelVersion(
        label_version_id="lv_transition",
        tenant_id="tenant_a",
        project_id="project_a",
        status="draft",
        resource_version=1,
        artifact_status="draft",
        trace_id="trace_transition",
        payload={},
    )
    published_at = datetime(2026, 7, 18, 8, 0, tzinfo=UTC)

    transition_label_version_artifact(record, "locked")
    transition_label_version_artifact(record, "published", occurred_at=published_at)

    assert record.artifact_status == "published"
    assert record.artifact_published_at == published_at
    with pytest.raises(LabelLifecycleDriftError, match="published.*locked"):
        transition_label_version_artifact(record, "locked")
    assert record.artifact_status == "published"


def test_compatible_reader_preserves_legacy_shape_and_shadow_detects_drift() -> None:
    record = LabelVersion(
        label_version_id="lv_shadow_read",
        tenant_id="tenant_a",
        project_id="project_a",
        status="draft",
        resource_version=2,
        taxonomy_id="taxonomy_strong",
        semantic_version="v2.0.0",
        artifact_status="published",
        content_sha256="d" * 64,
        trace_id="trace_shadow_read",
        payload={},
    )
    legacy = {
        "label_version_id": "lv_shadow_read",
        "taxonomy_id": "taxonomy_legacy",
        "version": "v2.0.0",
        "status": "gray_releasing",
        "content_sha256": "d" * 64,
    }

    data = compatible_label_version_data(record, legacy, prefer_strong=False)
    comparison = label_version_lifecycle_shadow_compare(record, legacy)

    assert data["status"] == "gray_releasing"
    assert data["semantic_version"] == "v2.0.0"
    assert data["artifact_status"] == "published"
    assert data["taxonomy_id"] == "taxonomy_legacy"
    assert comparison.status == "drift"
    assert comparison.mismatched_fields == ("taxonomy_id",)
