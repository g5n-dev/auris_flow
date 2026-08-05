from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.request_identifiers import public_id_from_hex
from app.models import (
    AudioRecording,
    JsonResource,
    ProjectSceneProfileBinding,
    RunRecord,
    SceneProfileVersion,
    StorageObject,
)
from app.services.audio_intelligence_service import audio_intelligence_output_assets
from app.services.audit_service import record_audit
from app.services.execution_contract_registry import (
    AUDIO_INTELLIGENCE_EXECUTION_CONTRACT,
    execution_contract_registry,
)
from app.services.outbox_service import enqueue_event

_AUTO_CAPABILITIES = ["vad", "asr", "diarization", "voiceprint", "quality"]


def _rooted_context(ctx: RequestContext, root_trace_id: str) -> RequestContext:
    if ctx.trace_id == root_trace_id:
        return ctx
    return replace(
        ctx,
        trace_id=root_trace_id,
        parent_trace_id=ctx.trace_id,
        correlation_id=root_trace_id,
    )


def _skip(
    session: Session,
    ctx: RequestContext,
    *,
    audio_session_id: str,
    reason: str,
) -> dict[str, Any]:
    resource = session.scalar(
        select(JsonResource).where(
            JsonResource.collection == "audio_sessions",
            JsonResource.resource_key == audio_session_id,
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
        )
    )
    if resource is not None:
        resource.data = {
            **resource.data,
            "intelligence_status": "not_scheduled",
            "intelligence_status_reason": reason,
            "root_trace_id": ctx.trace_id,
        }
        resource.trace_id = ctx.trace_id
    result = {
        "scheduled": False,
        "audio_session_id": audio_session_id,
        "reason": reason,
        "root_trace_id": ctx.trace_id,
    }
    record_audit(
        session,
        ctx,
        action="audio_intelligence.auto_trigger_skipped",
        object_type="audio_session",
        object_id=audio_session_id,
        result="skipped",
        after=result,
        trace_id=ctx.trace_id,
    )
    return result


def _published_audio_scene(
    session: Session,
    ctx: RequestContext,
) -> tuple[ProjectSceneProfileBinding, SceneProfileVersion] | None:
    binding = session.scalar(
        select(ProjectSceneProfileBinding).where(
            ProjectSceneProfileBinding.tenant_id == ctx.tenant_id,
            ProjectSceneProfileBinding.project_id == ctx.project_id,
            ProjectSceneProfileBinding.environment == "production",
            ProjectSceneProfileBinding.status == "active",
        )
    )
    if binding is None:
        return None
    version = session.scalar(
        select(SceneProfileVersion).where(
            SceneProfileVersion.tenant_id == ctx.tenant_id,
            SceneProfileVersion.project_id == ctx.project_id,
            SceneProfileVersion.scene_profile_id == binding.scene_profile_id,
            SceneProfileVersion.scene_profile_version_id == binding.scene_profile_version_id,
            SceneProfileVersion.status == "published",
        )
    )
    if (
        version is None
        or version.manifest_sha256 != binding.manifest_sha256
        or "audio-intelligence" not in set(version.manifest.get("capabilities") or [])
    ):
        return None
    return binding, version


def schedule_intelligence_for_materialized_audio_session(
    session: Session,
    ctx: RequestContext,
    *,
    event_payload: dict[str, Any],
) -> dict[str, Any]:
    """Create the downstream intelligence run after the import transaction.

    This is invoked while finalizing `audio_session.materialized`. Import has
    already committed at that point, so a missing SceneProfile or failed
    downstream launch cannot retroactively mark the import batch as failed.
    """

    audio_session_id = str(event_payload.get("audio_session_id") or "").strip()
    recording_id = str(event_payload.get("recording_id") or "").strip()
    storage_object_id = str(event_payload.get("storage_object_id") or "").strip()
    root_trace_id = str(event_payload.get("root_trace_id") or ctx.trace_id).strip()
    rooted_ctx = _rooted_context(ctx, root_trace_id)
    if not audio_session_id or not recording_id or not storage_object_id:
        return _skip(
            session,
            rooted_ctx,
            audio_session_id=audio_session_id or "unknown",
            reason="audio_session_materialization_binding_incomplete",
        )

    audio_session = session.scalar(
        select(JsonResource).where(
            JsonResource.collection == "audio_sessions",
            JsonResource.resource_key == audio_session_id,
            JsonResource.tenant_id == rooted_ctx.tenant_id,
            JsonResource.project_id == rooted_ctx.project_id,
        )
    )
    recording = session.scalar(
        select(AudioRecording).where(
            AudioRecording.recording_id == recording_id,
            AudioRecording.tenant_id == rooted_ctx.tenant_id,
            AudioRecording.project_id == rooted_ctx.project_id,
        )
    )
    storage_object = session.scalar(
        select(StorageObject).where(
            StorageObject.storage_object_id == storage_object_id,
            StorageObject.tenant_id == rooted_ctx.tenant_id,
            StorageObject.project_id == rooted_ctx.project_id,
        )
    )
    if (
        audio_session is None
        or recording is None
        or storage_object is None
        or audio_session.data.get("recording_id") != recording_id
        or recording.payload.get("storage_object_id") != storage_object_id
    ):
        return _skip(
            session,
            rooted_ctx,
            audio_session_id=audio_session_id,
            reason="audio_session_materialization_scope_or_lineage_mismatch",
        )

    storage_payload = storage_object.payload if isinstance(storage_object.payload, dict) else {}
    version_id = storage_payload.get("object_version_id")
    content_sha256 = str(storage_object.content_sha256 or "").strip().casefold()
    if (
        storage_object.status not in {"verified", "active"}
        or not isinstance(version_id, str)
        or not version_id.strip()
        or version_id.strip().casefold() == "null"
        or len(content_sha256) != 64
        or any(character not in "0123456789abcdef" for character in content_sha256)
        or not isinstance(storage_object.size_bytes, int)
        or not 44 <= storage_object.size_bytes <= 5 * 1024**3
        or storage_object.content_type not in {"audio/wav", "audio/x-wav"}
    ):
        return _skip(
            session,
            rooted_ctx,
            audio_session_id=audio_session_id,
            reason="verified_exact_audio_object_required",
        )

    scene = _published_audio_scene(session, rooted_ctx)
    if scene is None:
        return _skip(
            session,
            rooted_ctx,
            audio_session_id=audio_session_id,
            reason="published_audio_scene_profile_not_configured",
        )
    binding, scene_version = scene

    settings = get_settings()
    provider = settings.auris_audio_inference_provider.strip()
    models = [
        value.strip()
        for value in settings.auris_audio_inference_allowed_models.split(",")
        if value.strip()
    ]
    if not provider or not models:
        return _skip(
            session,
            rooted_ctx,
            audio_session_id=audio_session_id,
            reason="audio_inference_policy_not_configured",
        )
    model_version = models[0]
    locales = scene_version.manifest.get("locales")
    language = (
        str(locales[0])
        if isinstance(locales, list) and locales and isinstance(locales[0], str)
        else "zh-CN"
    )
    input_object = {
        "storage_object_id": storage_object.storage_object_id,
        "storage_provider": storage_object.provider,
        "bucket": storage_object.bucket,
        "object_key": storage_object.object_key,
        "version_id": version_id.strip(),
        "content_sha256": content_sha256,
        "content_length": storage_object.size_bytes,
        "content_type": storage_object.content_type,
    }
    identity = "\n".join(
        (
            rooted_ctx.tenant_id,
            rooted_ctx.project_id,
            audio_session_id,
            scene_version.scene_profile_version_id,
            storage_object.storage_object_id,
            version_id.strip(),
        )
    )
    run_id = public_id_from_hex(
        "audio_intelligence",
        hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        suffix_length=12,
    )
    existing = session.get(RunRecord, run_id)
    if existing is not None:
        if (
            existing.tenant_id != rooted_ctx.tenant_id
            or existing.project_id != rooted_ctx.project_id
            or existing.run_type != "audio_intelligence"
            or existing.payload.get("audio_session_id") != audio_session_id
            or existing.payload.get("input_object") != input_object
        ):
            return _skip(
                session,
                rooted_ctx,
                audio_session_id=audio_session_id,
                reason="audio_intelligence_run_identity_conflict",
            )
        audio_session.data = {
            **audio_session.data,
            "intelligence_status": existing.status,
            "intelligence_run_id": existing.run_id,
            "intelligence_scene_profile_version_id": binding.scene_profile_version_id,
            "root_trace_id": root_trace_id,
        }
        audio_session.trace_id = root_trace_id
        return {
            "scheduled": True,
            "deduplicated": True,
            "audio_session_id": audio_session_id,
            "run_id": existing.run_id,
            "status": existing.status,
            "root_trace_id": root_trace_id,
        }

    now = datetime.now(UTC)
    output_assets = audio_intelligence_output_assets(_AUTO_CAPABILITIES)
    output_sink_refs = sorted(
        {
            str(value).strip()
            for value in scene_version.manifest.get("output_sink_refs") or []
            if isinstance(value, str) and value.strip()
        }
    )
    payload = {
        "run_id": run_id,
        "status": "pending",
        "audio_session_id": audio_session_id,
        "recording_id": recording_id,
        "trigger": "audio_session.materialized",
        "execution_mode": "production",
        "execution_contract": AUDIO_INTELLIGENCE_EXECUTION_CONTRACT,
        "execution_deadline_at": (
            now + timedelta(seconds=settings.task_run_default_deadline_seconds)
        ).isoformat(),
        "input_object": input_object,
        "provider": provider,
        "model_version": model_version,
        "language": language,
        "capabilities": list(_AUTO_CAPABILITIES),
        "output_assets": output_assets,
        "scene_profile_id": binding.scene_profile_id,
        "scene_profile_version_id": binding.scene_profile_version_id,
        "scene_profile_snapshot_sha256": binding.manifest_sha256,
        "root_trace_id": root_trace_id,
        "trace_id": root_trace_id,
        "external_outputs_enabled": True,
        "writeback_mode": "configured",
        "callback_mode": "configured",
        # The published SceneProfile dependency closure already validated these
        # references. Freeze them on the business Run so a later review cannot
        # redirect writeback by editing the live SceneProfile.
        "output_sink_refs": output_sink_refs,
        "run_key": (
            f"audio-intelligence:auto:{audio_session_id}:"
            f"{binding.scene_profile_version_id}:{version_id.strip()}"
        ),
        "partition_key": (f"{rooted_ctx.tenant_id}/{rooted_ctx.project_id}/{recording_id}"),
        "affected_objects": [
            {"type": "audio_session", "id": audio_session_id},
            {"type": "recording", "id": recording_id},
            {
                "type": "scene_profile_version",
                "id": binding.scene_profile_version_id,
            },
            *[
                {
                    "type": "data_asset",
                    "id": asset["asset_key"],
                    "capability": asset["capability"],
                }
                for asset in output_assets
            ],
        ],
        "next_actions": [
            {
                "key": "view_audio_session",
                "label": "查看会话",
                "route": f"audio-sessions/{audio_session_id}",
            },
            {
                "key": "view_trace",
                "label": "查看 Trace",
                "route": f"traces/{root_trace_id}",
            },
        ],
    }
    # The registry is the server-owned allowlist. This check prevents an
    # internal scheduler from becoming a second, weaker execution entry point.
    execution_contract_registry.require(
        event_type="audio_intelligence.requested",
        run_type="audio_intelligence",
        payload=payload,
    )
    record = RunRecord(
        run_id=run_id,
        tenant_id=rooted_ctx.tenant_id,
        project_id=rooted_ctx.project_id,
        run_type="audio_intelligence",
        status="pending",
        run_key=payload["run_key"],
        partition_key=payload["partition_key"],
        trace_id=root_trace_id,
        created_at=now,
        updated_at=now,
        payload=payload,
    )
    session.add(record)
    session.flush()
    audio_session.data = {
        **audio_session.data,
        "intelligence_status": "pending",
        "intelligence_run_id": run_id,
        "intelligence_scene_profile_version_id": binding.scene_profile_version_id,
        "root_trace_id": root_trace_id,
    }
    audio_session.trace_id = root_trace_id
    enqueue_event(
        session,
        rooted_ctx,
        event_type="audio_intelligence.requested",
        aggregate_type="audio_intelligence",
        aggregate_id=run_id,
        payload=payload,
    )
    result = {
        "scheduled": True,
        "deduplicated": False,
        "audio_session_id": audio_session_id,
        "run_id": run_id,
        "status": "pending",
        "root_trace_id": root_trace_id,
    }
    record_audit(
        session,
        rooted_ctx,
        action="audio_intelligence.auto_triggered",
        object_type="audio_session",
        object_id=audio_session_id,
        result="pending",
        after={
            **result,
            "scene_profile_version_id": binding.scene_profile_version_id,
            "storage_object_id": storage_object.storage_object_id,
        },
        trace_id=root_trace_id,
    )
    return result
