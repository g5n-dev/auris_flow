from __future__ import annotations

from app.services.task_version_bundle import (
    build_task_version_bundle,
    compare_task_version_bundles,
)


def _task_version(**overrides):
    value = {
        "task_type_id": "task_generic_quality",
        "version": "v1",
        "status": "published",
        "canvas_variant": "stable-v1",
        "model_version": "asr-v1",
        "label_version": "labels-v1",
        "scene_profile_id": "scene-generic",
        "scene_profile_version_id": "scene-generic-v1",
        "scene_profile_snapshot_sha256": "a" * 64,
        "trace_id": "trace-one",
    }
    value.update(overrides)
    return value


def test_bundle_behavior_ignores_identity_and_lifecycle_fields():
    control = build_task_version_bundle("task-v1", _task_version())
    renamed = build_task_version_bundle(
        "task-v2",
        _task_version(version="v2", status="experiment_ready", trace_id="trace-two"),
    )

    assert control["behavior_sha256"] == renamed["behavior_sha256"]
    assert control["binding_sha256"] != renamed["binding_sha256"]
    assert compare_task_version_bundles(control, renamed)["changed_dimensions"] == []


def test_bundle_diff_separates_workflow_from_model_and_unknown_fields():
    control = build_task_version_bundle("task-v1", _task_version())
    workflow = build_task_version_bundle("task-v2", _task_version(canvas_variant="candidate-v2"))
    mixed = build_task_version_bundle(
        "task-v3",
        _task_version(
            canvas_variant="candidate-v2",
            model_version="asr-v2",
            untyped_runtime_switch="candidate",
        ),
    )

    assert compare_task_version_bundles(control, workflow)["changed_dimensions"] == ["workflow"]
    assert compare_task_version_bundles(control, mixed)["changed_dimensions"] == [
        "model",
        "other",
        "workflow",
    ]


def test_label_schema_is_not_misreported_as_label_policy():
    control = build_task_version_bundle("task-v1", _task_version())
    candidate = build_task_version_bundle("task-v2", _task_version(label_version="labels-v2"))

    assert compare_task_version_bundles(control, candidate)["changed_dimensions"] == [
        "label_schema"
    ]
