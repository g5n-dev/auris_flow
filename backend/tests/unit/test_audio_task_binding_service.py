from __future__ import annotations

import pytest

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.services.audio_task_binding_service import resolve_audio_hotword_task_binding


def _context() -> RequestContext:
    return RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="u_admin_001",
        roles=("project_admin",),
        request_id="req-audio-task-binding",
        trace_id="trace-audio-task-binding",
    )


def test_production_hotword_requires_published_task_version() -> None:
    with SessionLocal() as session:
        with pytest.raises(ApiError) as exc_info:
            resolve_audio_hotword_task_binding(
                session,
                _context(),
                execution_mode="production",
                task_version_id=None,
                hotword_pack_version_id="hwpv-auto-sales-v1-8",
                provider="auris-audio-stack",
                provider_explicit=True,
                model_version="asr_v2.3.1",
                model_version_explicit=True,
                language="zh-CN",
            )
    assert exc_info.value.code == "AUDIO_PRODUCTION_TASK_VERSION_REQUIRED"


def test_production_hotword_recovers_immutable_task_binding() -> None:
    with SessionLocal() as session:
        binding = resolve_audio_hotword_task_binding(
            session,
            _context(),
            execution_mode="production",
            task_version_id="task_version_v3_2_1",
            hotword_pack_version_id="hwpv-auto-sales-v1-8",
            provider=None,
            provider_explicit=False,
            model_version=None,
            model_version_explicit=False,
            language="zh-CN",
        )
    assert binding is not None
    assert binding["task_version_id"] == "task_version_v3_2_1"
    assert binding["hotword_pack_version_id"] == "hwpv-auto-sales-v1-8"
    assert binding["provider"] == "auris-audio-stack"
    assert binding["model_version"] == "asr_v2.3.1"
    assert binding["task_version_snapshot"]["status"] == "published"
    assert binding["root_trace_id"] == "trace_hotword_pack_auto_sales"


@pytest.mark.parametrize(
    ("hotword_version_id", "provider", "language", "expected_code"),
    [
        (
            "hwpv-other",
            None,
            "zh-CN",
            "AUDIO_TASK_HOTWORD_BINDING_MISMATCH",
        ),
        (
            "hwpv-auto-sales-v1-8",
            "other-provider",
            "zh-CN",
            "AUDIO_TASK_PROVIDER_BINDING_MISMATCH",
        ),
        (
            "hwpv-auto-sales-v1-8",
            None,
            "en-US",
            "AUDIO_TASK_LANGUAGE_BINDING_MISMATCH",
        ),
    ],
)
def test_production_hotword_rejects_per_run_binding_override(
    hotword_version_id: str,
    provider: str | None,
    language: str,
    expected_code: str,
) -> None:
    with SessionLocal() as session:
        with pytest.raises(ApiError) as exc_info:
            resolve_audio_hotword_task_binding(
                session,
                _context(),
                execution_mode="production",
                task_version_id="task_version_v3_2_1",
                hotword_pack_version_id=hotword_version_id,
                provider=provider,
                provider_explicit=provider is not None,
                model_version=None,
                model_version_explicit=False,
                language=language,
            )
    assert exc_info.value.code == expected_code


def test_shadow_candidate_can_run_without_task_version() -> None:
    with SessionLocal() as session:
        binding = resolve_audio_hotword_task_binding(
            session,
            _context(),
            execution_mode="shadow",
            task_version_id=None,
            hotword_pack_version_id="hwpv-auto-sales-v1-8",
            provider="auris-audio-stack",
            provider_explicit=True,
            model_version="candidate-model",
            model_version_explicit=True,
            language="zh-CN",
        )
    assert binding is None


def test_production_task_rejects_explicit_model_override() -> None:
    with SessionLocal() as session:
        with pytest.raises(ApiError) as exc_info:
            resolve_audio_hotword_task_binding(
                session,
                _context(),
                execution_mode="production",
                task_version_id="task_version_v3_2_1",
                hotword_pack_version_id="hwpv-auto-sales-v1-8",
                provider=None,
                provider_explicit=False,
                model_version="audio-v2.3.1",
                model_version_explicit=True,
                language="zh-CN",
            )

    assert exc_info.value.code == "AUDIO_TASK_MODEL_BINDING_MISMATCH"
