from __future__ import annotations

import hashlib
import json
from typing import Any

TASK_VERSION_BUNDLE_SCHEMA_VERSION = "task-version-bundle/1"
TASK_VERSION_VARIANT_DIMENSIONS = ("workflow", "model", "prompt", "label_policy", "bundle")

_NON_BEHAVIOR_FIELDS = {
    "task_version_id",
    "version",
    "status",
    "name",
    "description",
    "source",
    "created_at",
    "updated_at",
    "published_at",
    "published_by",
    "publish_run_id",
    "deprecated_at",
    "deprecated_by",
    "replaced_by_task_version_id",
    "replacement_run_id",
    "trace_id",
    "root_trace_id",
    "scene_profile_id",
    "scene_profile_version_id",
    "scene_profile_snapshot_sha256",
}

_COMPONENT_FIELDS: dict[str, tuple[str, ...]] = {
    "workflow": (
        "canvas_variant",
        "canvas_version_id",
        "workflow_version",
        "workflow_compiler_version",
        "task_definition_ref",
        "execution_plan_sha256",
        "graph",
        "execution",
    ),
    "model": (
        "model_version",
        "model_service_ref",
        "model_bindings",
        "audio_intelligence",
        "hotwords_ref",
        "hotword_pack_version_id",
        "hotword_binding_mode",
    ),
    "prompt": (
        "prompt_version_id",
        "prompt_version",
        "prompt_bindings",
    ),
    # label_version describes the taxonomy/schema and is deliberately separate
    # from executable policy bindings. A schema-only change is a bundle change,
    # not proof of a label-policy experiment.
    "label_schema": (
        "label_version",
        "label_version_id",
    ),
    "label_policy": (
        "policy_version_id",
        "label_policy_version_id",
        "aggregation_policy_version_id",
        "calibration_policy_version_id",
        "threshold_policy_version_id",
        "label_policy_bindings",
    ),
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_document(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _component_document(data: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: data[field] for field in fields if field in data and data[field] is not None}


def _component_summary(component: str, document: dict[str, Any]) -> list[str]:
    if not document:
        return []
    summary: list[str] = []
    for field, value in document.items():
        if isinstance(value, (str, int, float, bool)):
            summary.append(f"{field}={value}")
        elif isinstance(value, dict):
            refs = [
                f"{nested_key}={nested_value}"
                for nested_key, nested_value in value.items()
                if isinstance(nested_value, (str, int, float, bool))
                and any(
                    token in nested_key for token in ("id", "ref", "version", "sha256", "job_name")
                )
            ]
            summary.extend(f"{field}.{item}" for item in refs[:4])
    if not summary:
        summary.append(f"{component}:{len(document)} fields")
    return summary[:6]


def build_task_version_bundle(
    task_version_id: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    consumed_fields = set().union(*_COMPONENT_FIELDS.values())
    component_documents = {
        component: _component_document(data, fields)
        for component, fields in _COMPONENT_FIELDS.items()
    }
    other_document = {
        key: value
        for key, value in data.items()
        if key not in _NON_BEHAVIOR_FIELDS
        and key not in consumed_fields
        and key != "task_type_id"
        and value is not None
    }
    component_documents["other"] = other_document
    component_fingerprints = {
        component: {
            "present": bool(document),
            "sha256": sha256_document(document) if document else None,
            "summary": _component_summary(component, document),
            **({"field_names": sorted(document)} if component == "other" and document else {}),
        }
        for component, document in component_documents.items()
    }
    behavior_document = {
        "schema_version": TASK_VERSION_BUNDLE_SCHEMA_VERSION,
        "task_type_id": data.get("task_type_id"),
        "components": {
            component: fingerprint["sha256"]
            for component, fingerprint in component_fingerprints.items()
            if fingerprint["present"]
        },
    }
    behavior_sha256 = sha256_document(behavior_document)
    binding_document = {
        "schema_version": TASK_VERSION_BUNDLE_SCHEMA_VERSION,
        "task_version_id": task_version_id,
        "task_type_id": data.get("task_type_id"),
        "scene_profile_id": data.get("scene_profile_id"),
        "scene_profile_version_id": data.get("scene_profile_version_id"),
        "scene_profile_snapshot_sha256": data.get("scene_profile_snapshot_sha256"),
        "behavior_sha256": behavior_sha256,
    }
    return {
        "schema_version": TASK_VERSION_BUNDLE_SCHEMA_VERSION,
        "task_version_id": task_version_id,
        "task_type_id": data.get("task_type_id"),
        "scene_profile_id": data.get("scene_profile_id"),
        "scene_profile_version_id": data.get("scene_profile_version_id"),
        "scene_profile_snapshot_sha256": data.get("scene_profile_snapshot_sha256"),
        "component_fingerprints": component_fingerprints,
        "behavior_sha256": behavior_sha256,
        "binding_sha256": sha256_document(binding_document),
    }


def compare_task_version_bundles(
    control: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    component_keys = sorted(
        set(control["component_fingerprints"]) | set(candidate["component_fingerprints"])
    )
    changed_dimensions = [
        component
        for component in component_keys
        if control["component_fingerprints"].get(component, {}).get("sha256")
        != candidate["component_fingerprints"].get(component, {}).get("sha256")
    ]
    return {
        "changed_dimensions": changed_dimensions,
        "diff_sha256": sha256_document(
            {
                "control_behavior_sha256": control["behavior_sha256"],
                "candidate_behavior_sha256": candidate["behavior_sha256"],
                "changed_dimensions": changed_dimensions,
            }
        ),
    }
