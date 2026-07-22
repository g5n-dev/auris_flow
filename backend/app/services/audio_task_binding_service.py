from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.services.task_execution_policy import enforce_task_execution_policy


def resolve_audio_hotword_task_binding(
    session: Session,
    ctx: RequestContext,
    *,
    execution_mode: str,
    task_version_id: str | None,
    hotword_pack_version_id: str | None,
    provider: str | None,
    provider_explicit: bool,
    model_version: str | None,
    model_version_explicit: bool,
    language: str,
) -> dict[str, Any] | None:
    """Resolve the immutable TaskVersion binding for a production hotword run.

    Shadow and diagnostic runs may evaluate a candidate hotword version directly.
    Production runs must be launched through a published TaskVersion, so the
    client cannot swap the hotword pack, provider, model, or language per request.
    """

    normalized_task_version_id = str(task_version_id or "").strip()
    normalized_hotword_version_id = str(hotword_pack_version_id or "").strip()
    if execution_mode != "production":
        return None
    if not normalized_task_version_id:
        if not normalized_hotword_version_id:
            return None
        raise ApiError(
            "AUDIO_PRODUCTION_TASK_VERSION_REQUIRED",
            "生产 ASR 热词运行必须绑定已发布 TaskVersion",
            422,
            details=[{"hotword_pack_version_id": normalized_hotword_version_id}],
        )

    binding = enforce_task_execution_policy(
        session,
        ctx,
        {
            "task_version_id": normalized_task_version_id,
            "execution_mode": "production",
        },
    )
    canonical_hotword_version_id = str(binding.get("hotword_pack_version_id") or "").strip()
    if (
        normalized_hotword_version_id
        and normalized_hotword_version_id != canonical_hotword_version_id
    ):
        raise ApiError(
            "AUDIO_TASK_HOTWORD_BINDING_MISMATCH",
            "请求热词版本与 TaskVersion 冻结绑定不一致",
            409,
            details=[
                {
                    "requested_hotword_pack_version_id": normalized_hotword_version_id or None,
                    "task_hotword_pack_version_id": canonical_hotword_version_id or None,
                }
            ],
        )
    canonical_provider = str(binding.get("provider") or "")
    if provider_explicit and str(provider or "") != canonical_provider:
        raise ApiError(
            "AUDIO_TASK_PROVIDER_BINDING_MISMATCH",
            "请求 provider 与 TaskVersion 冻结绑定不一致",
            409,
            details=[
                {
                    "requested_provider": provider,
                    "task_provider": canonical_provider,
                }
            ],
        )
    task_version_snapshot = binding.get("task_version_snapshot")
    if not isinstance(task_version_snapshot, dict):
        raise ApiError(
            "AUDIO_TASK_MODEL_BINDING_REQUIRED",
            "生产 TaskVersion 缺少不可变模型快照",
            409,
        )
    canonical_model_version = str(task_version_snapshot.get("model_version") or "").strip()
    if not canonical_model_version:
        raise ApiError(
            "AUDIO_TASK_MODEL_BINDING_REQUIRED",
            "生产 TaskVersion 必须冻结模型版本",
            409,
        )
    if model_version_explicit and str(model_version or "").strip() != canonical_model_version:
        raise ApiError(
            "AUDIO_TASK_MODEL_BINDING_MISMATCH",
            "请求模型版本与 TaskVersion 冻结绑定不一致",
            409,
            details=[
                {
                    "requested_model_version": model_version,
                    "task_model_version": canonical_model_version,
                }
            ],
        )
    canonical_language = str(binding.get("language") or "")
    if language != canonical_language:
        raise ApiError(
            "AUDIO_TASK_LANGUAGE_BINDING_MISMATCH",
            "请求语言与 TaskVersion 冻结绑定不一致",
            409,
            details=[
                {
                    "requested_language": language,
                    "task_language": canonical_language,
                }
            ],
        )
    return {
        "task_version_id": normalized_task_version_id,
        "hotword_pack_version_id": canonical_hotword_version_id,
        "provider": canonical_provider,
        "model_version": canonical_model_version,
        "language": canonical_language,
        "root_trace_id": binding["root_trace_id"],
        "task_version_snapshot": task_version_snapshot,
        "external_outputs_enabled": binding["external_outputs_enabled"],
        "writeback_mode": binding["writeback_mode"],
        "callback_mode": binding["callback_mode"],
    }
