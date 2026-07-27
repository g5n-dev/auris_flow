from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import ImportBatch, JsonResource
from app.repositories.json_resources import JsonResourceRepository
from app.schemas.scene_profiles import SceneProfileManifest
from app.services.task_version_bundle import build_task_version_bundle

NON_PRODUCTION_MODES = {"diagnostic", "shadow", "experiment"}
LEGACY_HOTWORD_KEYS = {"hotwords_ref", "legacy_hotwords_ref"}
HOTWORD_RUN_OVERRIDE_KEYS = {
    "hotword_pack_version_id",
    "provider",
    "provider_ref",
    "model_version",
    "language",
}
HOTWORD_PUBLISH_LINEAGE_FIELDS = frozenset(
    {
        "task_version_id",
        "hotword_pack_version_id",
        "provider",
        "model_version",
        "language",
        "execution_mode",
        "source",
        "source_publish_run_id",
        "source_hotword_pack_version_id",
        "root_trace_id",
        "trace_id",
    }
)
AUDIO_PLATFORM_IMPORT_TASK_TYPE_ID = "audio-platform-import"
AUDIO_PLATFORM_IMPORT_EXECUTION_CONTRACT = "auris-flow-audio-import-v1"
IMPORT_TASK_SERVER_FIELDS = frozenset(
    {
        "connector_snapshot",
        "connector_snapshot_sha256",
        "execution_contract",
        "import_target",
    }
)


def _assert_task_type_in_scene_profile(
    session: Session,
    ctx: RequestContext,
    data: dict[str, Any],
    *,
    scene_profile_version_id: str,
) -> None:
    task_type_id = str(data.get("task_type_id") or "").strip()
    if not task_type_id:
        raise ApiError(
            "TASK_TYPE_BINDING_REQUIRED",
            "TaskVersion 必须声明 task_type_id",
            409,
        )
    # Platform ingestion is foundational project infrastructure shared by all
    # scene profiles. It is still bound to the active scene snapshot for audit,
    # but does not require every business-scene manifest to duplicate this
    # server-owned task type.
    if task_type_id == AUDIO_PLATFORM_IMPORT_TASK_TYPE_ID:
        return
    from app.services.scene_profile_service import get_scene_profile_version

    version = get_scene_profile_version(session, ctx, scene_profile_version_id)
    manifest = SceneProfileManifest.model_validate(version.manifest)
    if task_type_id not in set(manifest.task_type_refs):
        raise ApiError(
            "TASK_TYPE_NOT_IN_SCENE_PROFILE",
            "TaskVersion 的任务类型未被当前 SceneProfile 声明",
            409,
            details=[
                {
                    "task_type_id": task_type_id,
                    "scene_profile_version_id": scene_profile_version_id,
                    "allowed_task_type_refs": list(manifest.task_type_refs),
                }
            ],
        )


def _walk_fields(value: Any) -> list[tuple[str, Any]]:
    fields: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            fields.append((str(key), nested))
            fields.extend(_walk_fields(nested))
    elif isinstance(value, list):
        for nested in value:
            fields.extend(_walk_fields(nested))
    return fields


def reject_legacy_hotword_writes(payload: dict[str, Any]) -> None:
    found = sorted({key for key, _ in _walk_fields(payload) if key in LEGACY_HOTWORD_KEYS})
    if found:
        raise ApiError(
            "LEGACY_HOTWORDS_REF_READ_ONLY",
            "hotwords_ref 仅用于历史只读兼容；新写入必须使用 hotword_pack_version_id",
            422,
            details=[{"fields": found}],
        )


def _hotword_version_ids(data: dict[str, Any]) -> set[str]:
    return {
        str(value).strip()
        for key, value in _walk_fields(data)
        if key == "hotword_pack_version_id" and isinstance(value, str) and value.strip()
    }


def _audio_binding(data: dict[str, Any]) -> dict[str, Any]:
    audio = data.get("audio_intelligence")
    audio_data = audio if isinstance(audio, dict) else {}
    version_ids = _hotword_version_ids(data)
    if len(version_ids) > 1:
        raise ApiError(
            "TASK_HOTWORD_VERSION_CONFLICT",
            "TaskVersion 内存在多个不一致的热词版本绑定",
            409,
            details=[{"version_ids": sorted(version_ids)}],
        )
    version_id = next(iter(version_ids), None)
    legacy_refs = {
        str(value).strip()
        for key, value in _walk_fields(data)
        if key == "hotwords_ref" and isinstance(value, str) and value.strip()
    }
    if not version_id and legacy_refs:
        raise ApiError(
            "LEGACY_HOTWORDS_REF_UNMAPPED",
            "历史 hotwords_ref 没有显式不可变版本；请迁移为 hotword_pack_version_id",
            409,
            details=[{"legacy_refs": sorted(legacy_refs)}],
        )
    model_versions = {
        str(value).strip()
        for value in (data.get("model_version"), audio_data.get("model_version"))
        if isinstance(value, str) and value.strip()
    }
    if len(model_versions) > 1:
        raise ApiError(
            "TASK_MODEL_VERSION_CONFLICT",
            "TaskVersion 内存在多个不一致的模型版本绑定",
            409,
            details=[{"model_versions": sorted(model_versions)}],
        )
    return {
        "hotword_pack_version_id": version_id,
        "provider": data.get("provider")
        or audio_data.get("provider")
        or audio_data.get("provider_ref")
        or "auris-audio-stack",
        "model_version": next(iter(model_versions), None),
        "language": data.get("language") or audio_data.get("language") or "zh-CN",
        "execution_mode": data.get("execution_mode")
        or audio_data.get("execution_mode")
        or "production",
    }


def _hotword_run_override_fields(payload: dict[str, Any]) -> list[str]:
    """Only inspect the documented TaskRun binding locations.

    Arbitrary business payloads may legitimately contain fields named provider or
    language, so recursively scanning the full request would break non-ASR tasks.
    """

    fields = {key for key in HOTWORD_RUN_OVERRIDE_KEYS if key in payload}
    audio = payload.get("audio_intelligence")
    if isinstance(audio, dict):
        fields.update(
            f"audio_intelligence.{key}" for key in HOTWORD_RUN_OVERRIDE_KEYS if key in audio
        )
    return sorted(fields)


def prepare_task_version_write(
    session: Session,
    ctx: RequestContext,
    payload: dict[str, Any],
    *,
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reject_legacy_hotword_writes(payload)
    task_type_id = str(
        payload.get("task_type_id") or (current or {}).get("task_type_id") or ""
    ).strip()
    if task_type_id == AUDIO_PLATFORM_IMPORT_TASK_TYPE_ID:
        forged_fields = sorted(IMPORT_TASK_SERVER_FIELDS.intersection(payload))
        if forged_fields:
            raise ApiError(
                "TASK_IMPORT_SERVER_FIELDS_FORBIDDEN",
                "导入任务的连接器快照与执行契约只能由服务端冻结",
                422,
                details=[{"fields": forged_fields}],
            )
    if current and str(current.get("status") or "") in {
        "published",
        "validated",
        "experiment_ready",
        "deprecated",
    }:
        raise ApiError(
            "TASK_VERSION_IMMUTABLE",
            "已发布或已冻结的 TaskVersion 不允许通用 PATCH 原地修改",
            409,
        )
    if current and current.get("source") == "hotword_pack_publish":
        attempted_changes = sorted(
            field
            for field in HOTWORD_PUBLISH_LINEAGE_FIELDS
            if field in payload and payload.get(field) != current.get(field)
        )
        current_audio = current.get("audio_intelligence")
        payload_audio = payload.get("audio_intelligence")
        if isinstance(current_audio, dict) and isinstance(payload_audio, dict):
            attempted_changes.extend(
                f"audio_intelligence.{field}"
                for field in (
                    "hotword_pack_version_id",
                    "provider",
                    "model_version",
                    "language",
                    "execution_mode",
                )
                if field in payload_audio and payload_audio.get(field) != current_audio.get(field)
            )
        if attempted_changes:
            raise ApiError(
                "HOTWORD_TASK_VERSION_LINEAGE_IMMUTABLE",
                "热词发布生成的 TaskVersion 不允许修改服务端冻结的生产绑定与根血缘",
                409,
                details=[{"fields": sorted(set(attempted_changes))}],
            )
    merged = {**(current or {}), **payload}
    if task_type_id == AUDIO_PLATFORM_IMPORT_TASK_TYPE_ID:
        connector_id = str(merged.get("connector_id") or "").strip()
        if not connector_id:
            raise ApiError(
                "TASK_IMPORT_CONNECTOR_REQUIRED",
                "平台音频导入 TaskVersion 必须绑定 connector_id",
                422,
            )
        from app.services.connector_import_service import (
            validate_platform_audio_connector,
        )
        from app.services.resource_service import get_resource

        connector = get_resource(session, ctx, "connectors", connector_id)
        validate_platform_audio_connector(session, ctx, connector.data)
        merged = {
            **merged,
            "connector_id": connector_id,
            "execution_contract": AUDIO_PLATFORM_IMPORT_EXECUTION_CONTRACT,
        }
    from app.services.scene_profile_service import get_project_scene_binding

    scene_binding = get_project_scene_binding(session, ctx, "production")
    if scene_binding is not None and scene_binding.status == "active":
        supplied_scene_id = str(merged.get("scene_profile_id") or "").strip()
        supplied_scene_version = str(merged.get("scene_profile_version_id") or "").strip()
        supplied_scene_hash = str(merged.get("scene_profile_snapshot_sha256") or "").strip()
        if supplied_scene_id and supplied_scene_id != scene_binding.scene_profile_id:
            raise ApiError(
                "TASK_SCENE_PROFILE_ID_MISMATCH",
                "TaskVersion 指定的场景配置与当前项目绑定不一致",
                409,
            )
        if (
            supplied_scene_version
            and supplied_scene_version != scene_binding.scene_profile_version_id
        ):
            raise ApiError(
                "TASK_SCENE_PROFILE_VERSION_MISMATCH",
                "TaskVersion 指定的场景版本与当前项目绑定不一致",
                409,
            )
        if supplied_scene_hash and supplied_scene_hash != scene_binding.manifest_sha256:
            raise ApiError(
                "TASK_SCENE_PROFILE_SNAPSHOT_MISMATCH",
                "TaskVersion 指定的场景快照与当前项目绑定不一致",
                409,
            )
        merged = {
            **merged,
            "scene_profile_id": scene_binding.scene_profile_id,
            "scene_profile_version_id": scene_binding.scene_profile_version_id,
            "scene_profile_snapshot_sha256": scene_binding.manifest_sha256,
        }
    binding = _audio_binding(merged)
    version_id = binding["hotword_pack_version_id"]
    if not version_id:
        return {**merged, "status": "draft"}
    from app.services.hotword_service import validate_hotword_execution

    provider = validate_hotword_execution(
        session,
        ctx,
        version_id=str(version_id),
        execution_mode=str(binding["execution_mode"]),
        provider=str(binding["provider"]),
        language=str(binding["language"]),
    )
    audio = merged.get("audio_intelligence")
    audio_data = dict(audio) if isinstance(audio, dict) else {}
    normalized_binding = {
        "hotword_pack_version_id": version_id,
        "provider": provider,
        "language": binding["language"],
        "execution_mode": binding["execution_mode"],
    }
    return {
        **merged,
        **normalized_binding,
        "audio_intelligence": {**audio_data, **normalized_binding},
        "status": "draft",
    }


def validate_task_version_publish_binding(
    session: Session,
    ctx: RequestContext,
    data: dict[str, Any],
    *,
    task_version_id: str | None = None,
) -> dict[str, Any]:
    from app.services.scene_profile_service import assert_active_scene_profile_binding

    scene_profile_id = str(data.get("scene_profile_id") or "").strip()
    scene_profile_version_id = str(data.get("scene_profile_version_id") or "").strip()
    scene_profile_snapshot_sha256 = str(data.get("scene_profile_snapshot_sha256") or "").strip()
    if not scene_profile_id or not scene_profile_version_id or not scene_profile_snapshot_sha256:
        raise ApiError(
            "TASK_SCENE_PROFILE_SNAPSHOT_REQUIRED",
            "发布 TaskVersion 必须锁定 SceneProfile、版本和内容哈希",
            409,
        )
    assert_active_scene_profile_binding(
        session,
        ctx,
        scene_profile_id=scene_profile_id,
        scene_profile_version_id=scene_profile_version_id,
        scene_profile_snapshot_sha256=scene_profile_snapshot_sha256,
    )
    _assert_task_type_in_scene_profile(
        session,
        ctx,
        data,
        scene_profile_version_id=scene_profile_version_id,
    )
    binding = _audio_binding(data)
    version_id = binding["hotword_pack_version_id"]
    if not version_id:
        return binding
    from app.services.hotword_service import get_hotword_version, validate_hotword_execution

    binding["provider"] = validate_hotword_execution(
        session,
        ctx,
        version_id=str(version_id),
        execution_mode=str(binding["execution_mode"]),
        provider=str(binding["provider"]),
        language=str(binding["language"]),
    )
    version = get_hotword_version(session, ctx, str(version_id))
    canonical_task_version_id = (version.payload or {}).get("task_version_id")
    is_declared_hotword_release = data.get("source") == "hotword_pack_publish"
    is_canonical_hotword_release = (
        not bool((version.payload or {}).get("legacy_import"))
        and task_version_id is not None
        and canonical_task_version_id == task_version_id
    )
    if is_declared_hotword_release or is_canonical_hotword_release:
        expected_lineage = {
            "source": "hotword_pack_publish",
            "source_hotword_pack_version_id": version.version_id,
            "source_publish_run_id": (version.payload or {}).get("publish_run_id"),
            "root_trace_id": version.root_trace_id,
            "hotword_pack_version_id": version.version_id,
            "provider": version.compiled_provider,
            "model_version": (version.payload or {}).get("task_model_version"),
            "language": binding["language"],
            "execution_mode": "production",
        }
        if task_version_id is not None:
            expected_lineage["task_version_id"] = canonical_task_version_id
        mismatches = [
            {
                "field": field,
                "expected": expected,
                "actual": (
                    task_version_id
                    if field == "task_version_id"
                    else binding.get(field)
                    if field
                    in {
                        "hotword_pack_version_id",
                        "provider",
                        "model_version",
                        "language",
                        "execution_mode",
                    }
                    else data.get(field)
                ),
            }
            for field, expected in expected_lineage.items()
            if expected is None
            or (
                task_version_id
                if field == "task_version_id"
                else binding.get(field)
                if field
                in {
                    "hotword_pack_version_id",
                    "provider",
                    "model_version",
                    "language",
                    "execution_mode",
                }
                else data.get(field)
            )
            != expected
        ]
        if mismatches:
            raise ApiError(
                "HOTWORD_TASK_VERSION_LINEAGE_INVALID",
                "TaskVersion 的热词发布来源、生产绑定或根 Trace 与权威词包版本不一致",
                409,
                details=mismatches,
            )
    return binding


def freeze_import_task_version_connector(
    session: Session,
    ctx: RequestContext,
    data: dict[str, Any],
) -> dict[str, Any]:
    if str(data.get("task_type_id") or "") != AUDIO_PLATFORM_IMPORT_TASK_TYPE_ID:
        return data
    connector_id = str(data.get("connector_id") or "").strip()
    if not connector_id:
        raise ApiError(
            "TASK_IMPORT_CONNECTOR_REQUIRED",
            "平台音频导入 TaskVersion 必须绑定 connector_id",
            422,
        )
    from app.services.connector_import_service import freeze_connector_snapshot

    snapshot, snapshot_sha256, dedupe_policy, target_asset_key = freeze_connector_snapshot(
        session, ctx, connector_id
    )
    return {
        **data,
        "connector_id": connector_id,
        "connector_snapshot": snapshot,
        "connector_snapshot_sha256": snapshot_sha256,
        "execution_contract": AUDIO_PLATFORM_IMPORT_EXECUTION_CONTRACT,
        "import_target": {
            "target_asset_key": target_asset_key,
            "dedupe_policy": dedupe_policy,
        },
    }


def _version_snapshot(resource_key: str, data: dict[str, Any], status: str) -> dict[str, Any]:
    binding = _audio_binding(data)
    execution_contract = data.get("execution_contract")
    version_document = {
        "task_version_id": resource_key,
        "task_type_id": data.get("task_type_id"),
        "version": data.get("version"),
        "canvas_variant": data.get("canvas_variant"),
        "label_version": data.get("label_version"),
        "model_version": data.get("model_version"),
        "hotword_pack_version_id": binding["hotword_pack_version_id"],
        "provider": binding["provider"],
        "language": binding["language"],
        "execution_mode": binding["execution_mode"],
        "scene_profile_id": data.get("scene_profile_id"),
        "scene_profile_version_id": data.get("scene_profile_version_id"),
        "scene_profile_snapshot_sha256": data.get("scene_profile_snapshot_sha256"),
        **({"execution_contract": execution_contract} if execution_contract is not None else {}),
        "connector_snapshot_sha256": data.get("connector_snapshot_sha256"),
        "status": status,
    }
    canonical = json.dumps(
        version_document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    bundle = build_task_version_bundle(resource_key, data)
    return {
        **version_document,
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "bundle_schema_version": bundle["schema_version"],
        "behavior_sha256": bundle["behavior_sha256"],
        "binding_sha256": bundle["binding_sha256"],
        "component_fingerprints": bundle["component_fingerprints"],
    }


def _connector_version(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        normalized = int(value)
        return normalized if normalized > 0 else None
    return None


def _prepare_audio_import_run(
    session: Session,
    ctx: RequestContext,
    payload: dict[str, Any],
    task_version: dict[str, Any],
) -> dict[str, Any]:
    if str(payload.get("execution_mode") or "") != "production":
        raise ApiError(
            "TASK_IMPORT_PRODUCTION_MODE_REQUIRED",
            "平台音频拉取必须使用 production 执行模式",
            409,
        )
    snapshot = task_version.get("connector_snapshot")
    snapshot_sha256 = str(task_version.get("connector_snapshot_sha256") or "").strip()
    import_target = task_version.get("import_target")
    if (
        not isinstance(snapshot, dict)
        or len(snapshot_sha256) != 64
        or not isinstance(import_target, dict)
    ):
        raise ApiError(
            "TASK_IMPORT_CONNECTOR_SNAPSHOT_REQUIRED",
            "导入任务必须先发布并冻结连接器快照",
            409,
        )
    canonical_snapshot = json.dumps(
        snapshot,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(canonical_snapshot).hexdigest() != snapshot_sha256:
        raise ApiError(
            "TASK_IMPORT_CONNECTOR_SNAPSHOT_INVALID",
            "导入任务的连接器快照校验失败",
            409,
        )
    target_asset_key = str(import_target.get("target_asset_key") or "").strip()
    dedupe_policy = str(import_target.get("dedupe_policy") or "").strip()
    if not target_asset_key or dedupe_policy != "external_id_checksum":
        raise ApiError(
            "TASK_IMPORT_TARGET_INVALID",
            "导入任务的目标资产或去重策略不合法",
            409,
        )
    connector_id = str(snapshot.get("connector_id") or "").strip()
    connector = session.scalar(
        select(JsonResource)
        .where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "connectors",
            JsonResource.resource_key == connector_id,
        )
        .with_for_update()
    )
    if connector is None:
        raise ApiError(
            "TASK_IMPORT_CONNECTOR_NOT_FOUND",
            "已发布导入任务绑定的连接器不存在",
            409,
        )
    if str(connector.data.get("status") or connector.status or "") == "disabled":
        raise ApiError(
            "CONNECTOR_DISABLED",
            "已停用的连接器不能执行导入任务",
            409,
        )
    frozen_connector_version = _connector_version(snapshot.get("connector_version"))
    live_connector_version = _connector_version(connector.data.get("connector_version"))
    if (
        frozen_connector_version is None
        or live_connector_version is None
        or live_connector_version != frozen_connector_version
    ):
        raise ApiError(
            "TASK_IMPORT_CONNECTOR_VERSION_MISMATCH",
            "已发布任务冻结的连接器版本与当前连接器不一致",
            409,
            details=[
                {
                    "connector_id": connector_id,
                    "task_connector_version": snapshot.get("connector_version"),
                    "live_connector_version": connector.data.get("connector_version"),
                }
            ],
        )
    active_batch_id = session.scalar(
        select(ImportBatch.import_batch_id)
        .where(
            ImportBatch.tenant_id == ctx.tenant_id,
            ImportBatch.project_id == ctx.project_id,
            ImportBatch.connector_id == connector_id,
            ImportBatch.status.in_(("queued", "running")),
        )
        .order_by(ImportBatch.created_at)
        .limit(1)
    )
    if active_batch_id is not None:
        raise ApiError(
            "CONNECTOR_IMPORT_ALREADY_ACTIVE",
            "该平台连接当前已有导入批次在执行，请等待完成后重试",
            409,
            details=[{"connector_id": connector_id, "import_batch_id": active_batch_id}],
        )
    live_cursor = connector.data.get("sync_cursor")
    if live_cursor is not None and (
        not isinstance(live_cursor, str)
        or len(live_cursor) > 1024
        or any(ord(character) < 0x20 for character in live_cursor)
    ):
        raise ApiError(
            "CONNECTOR_CURSOR_STATE_INVALID",
            "连接器同步游标状态不合法",
            409,
        )
    cursor_connector_version = connector.data.get("sync_cursor_connector_version")
    if live_cursor is None:
        if cursor_connector_version is not None:
            raise ApiError(
                "CONNECTOR_CURSOR_STATE_INVALID",
                "连接器同步游标缺失但仍包含游标版本",
                409,
            )
    elif _connector_version(cursor_connector_version) != frozen_connector_version:
        raise ApiError(
            "CONNECTOR_CURSOR_VERSION_MISMATCH",
            "连接器同步游标不属于已发布任务冻结的连接器版本",
            409,
            details=[
                {
                    "connector_id": connector_id,
                    "task_connector_version": frozen_connector_version,
                    "cursor_connector_version": cursor_connector_version,
                }
            ],
        )
    runtime_snapshot = json.loads(json.dumps(snapshot, ensure_ascii=True))
    runtime_cursor_policy = runtime_snapshot.get("cursor_policy")
    if not isinstance(runtime_cursor_policy, dict):
        raise ApiError(
            "TASK_IMPORT_CONNECTOR_SNAPSHOT_INVALID",
            "导入任务的连接器游标策略不合法",
            409,
        )
    runtime_cursor_policy.pop("cursor_value", None)
    if live_cursor:
        runtime_cursor_policy["cursor_value"] = live_cursor
    task_run_id = f"task_run_{uuid.uuid4().hex[:12]}"
    import_batch_id = f"import_batch_{uuid.uuid4().hex[:12]}"
    root_trace_id = ctx.trace_id
    deadline_at = datetime.now(UTC) + timedelta(
        seconds=get_settings().task_run_default_deadline_seconds
    )
    object_prefix = (
        f"tenants/{ctx.tenant_id}/projects/{ctx.project_id}/runs/{task_run_id}/audio-import/"
    )
    target = {
        "storage_provider": get_settings().object_storage_provider,
        "bucket": get_settings().object_storage_bucket,
        "object_prefix": object_prefix,
        "target_asset_key": target_asset_key,
        "dedupe_policy": dedupe_policy,
    }
    return {
        **payload,
        "task_run_id": task_run_id,
        "import_batch_id": import_batch_id,
        "root_trace_id": root_trace_id,
        "execution_contract": AUDIO_PLATFORM_IMPORT_EXECUTION_CONTRACT,
        "execution_deadline_at": deadline_at.isoformat(),
        "connector_snapshot": runtime_snapshot,
        "connector_snapshot_sha256": snapshot_sha256,
        "target": target,
        "target_object_prefix": object_prefix,
        "affected_objects": [
            {"type": "task_version", "id": payload.get("task_version_id")},
            {"type": "connector", "id": snapshot.get("connector_id")},
            {"type": "import_batch", "id": import_batch_id},
            {"type": "data_asset", "id": target_asset_key},
        ],
        "next_actions": [
            {
                "key": "view_import_batch",
                "label": "查看同步批次",
                "route": f"import-batches/{import_batch_id}",
            },
            {"key": "view_trace", "label": "查看 Trace", "route": f"traces/{root_trace_id}"},
        ],
    }


def enforce_task_execution_policy(
    session: Session,
    ctx: RequestContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    task_version_id = str(payload.get("task_version_id") or "").strip()
    resource = JsonResourceRepository(session).find(
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        collection="task_versions",
        resource_key=task_version_id,
    )
    if resource is None:
        raise ApiError(
            "TASK_VERSION_NOT_FOUND",
            f"当前项目不存在任务版本：{task_version_id}",
            404,
        )

    data = resource.data if isinstance(resource.data, dict) else {}
    status = str(data.get("status") or resource.status or "unknown")
    execution_mode = str(payload.get("execution_mode") or "production")
    if execution_mode == "production" and status != "published":
        raise ApiError(
            "TASK_VERSION_NOT_PUBLISHED",
            f"任务版本 {task_version_id} 当前为 {status}，不能执行生产运行",
            409,
            details=[
                {
                    "task_version_id": task_version_id,
                    "status": status,
                    "allowed_status": "published",
                    "allowed_non_production_modes": sorted(NON_PRODUCTION_MODES),
                }
            ],
        )
    if execution_mode == "experiment" and status not in {
        "published",
        "validated",
        "experiment_ready",
    }:
        raise ApiError(
            "TASK_VERSION_NOT_EXPERIMENT_READY",
            f"任务版本 {task_version_id} 当前为 {status}，不能执行受控实验运行",
            409,
        )
    if execution_mode not in {"production", *NON_PRODUCTION_MODES}:
        raise ApiError(
            "TASK_EXECUTION_MODE_INVALID",
            f"不支持的任务执行模式：{execution_mode}",
            400,
        )

    scene_profile_id = str(data.get("scene_profile_id") or "").strip()
    scene_profile_version_id = str(data.get("scene_profile_version_id") or "").strip()
    scene_profile_snapshot_sha256 = str(data.get("scene_profile_snapshot_sha256") or "").strip()
    if execution_mode in {"production", "experiment"}:
        from app.services.scene_profile_service import assert_active_scene_profile_binding

        if (
            not scene_profile_id
            or not scene_profile_version_id
            or not scene_profile_snapshot_sha256
        ):
            raise ApiError(
                "TASK_SCENE_PROFILE_SNAPSHOT_REQUIRED",
                "生产或受控实验 TaskVersion 必须锁定 SceneProfile、版本和内容哈希",
                409,
            )
        assert_active_scene_profile_binding(
            session,
            ctx,
            scene_profile_id=scene_profile_id,
            scene_profile_version_id=scene_profile_version_id,
            scene_profile_snapshot_sha256=scene_profile_snapshot_sha256,
        )
        _assert_task_type_in_scene_profile(
            session,
            ctx,
            data,
            scene_profile_version_id=scene_profile_version_id,
        )

    binding = _audio_binding(data)
    version_id = binding["hotword_pack_version_id"]
    reject_legacy_hotword_writes(payload)
    override_fields = _hotword_run_override_fields(payload)
    if version_id and override_fields:
        raise ApiError(
            "TASK_HOTWORD_BINDING_OVERRIDE_FORBIDDEN",
            "TaskRun 必须使用 TaskVersion 冻结的热词版本、provider 和语言",
            409,
            details=[{"fields": override_fields}],
        )
    root_trace_id = str(data.get("root_trace_id") or data.get("trace_id") or ctx.trace_id)
    if version_id:
        from app.services.hotword_service import (
            get_hotword_version,
            validate_hotword_execution,
        )

        binding["provider"] = validate_hotword_execution(
            session,
            ctx,
            version_id=str(version_id),
            execution_mode=execution_mode,
            provider=str(binding["provider"]),
            language=str(binding["language"]),
            require_production_active=execution_mode in {"production", "experiment"},
        )
        root_trace_id = get_hotword_version(session, ctx, str(version_id)).root_trace_id

    non_production = execution_mode in NON_PRODUCTION_MODES
    prepared_run = {
        **payload,
        **binding,
        "root_trace_id": root_trace_id,
        "execution_mode": execution_mode,
        "external_outputs_enabled": not non_production,
        "writeback_mode": "disabled" if non_production else "configured",
        "callback_mode": "disabled" if non_production else "configured",
        "scene_profile_id": data.get("scene_profile_id"),
        "scene_profile_version_id": scene_profile_version_id or None,
        "scene_profile_snapshot_sha256": scene_profile_snapshot_sha256 or None,
        "task_version_snapshot": _version_snapshot(task_version_id, data, status),
    }
    if str(data.get("task_type_id") or "") == AUDIO_PLATFORM_IMPORT_TASK_TYPE_ID:
        return _prepare_audio_import_run(session, ctx, prepared_run, data)
    return prepared_run
