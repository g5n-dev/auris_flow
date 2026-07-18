from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import HotwordPackVersion, HotwordVersionItem, RunRecord, StorageObject
from app.services import audio_intelligence_service
from app.services.audio_intelligence_service import validate_audio_intelligence_result
from app.services.hotword_service import (
    _per_term_occurrences_gate_blocked,
    _recalculate_content_hash,
    calculate_hotword_metrics,
    classify_hotword_candidate,
    ensure_hotword_is_not_sensitive,
    evaluate_release_gate,
    normalize_hotword,
    priority_score,
)


def test_normalize_hotword_uses_nfkc_edge_punctuation_and_casefold() -> None:
    assert normalize_hotword("　（Ａｕｒｉｓ）　") == "auris"
    assert normalize_hotword("··星越L··") == "星越l"


def test_content_hash_uses_normalized_terms_but_preserves_display_text() -> None:
    with SessionLocal() as session:
        version = session.get(HotwordPackVersion, "hwpv-auto-sales-v1-8")
        item = session.get(HotwordVersionItem, "hotword_item_xingyue_l")
        assert version is not None and item is not None

        _recalculate_content_hash(session, version)
        normalized_hash = version.content_sha256
        item.canonical_term = "　（星越Ｌ）　"
        item.aliases = ["　星越　Ｌ　"]
        _recalculate_content_hash(session, version)

        assert version.content_sha256 == normalized_hash
        assert item.canonical_term == "　（星越Ｌ）　"
        assert item.aliases == ["　星越　Ｌ　"]
        session.rollback()


def test_metrics_candidate_threshold_and_priority_are_deterministic() -> None:
    metrics = calculate_hotword_metrics(
        expected_count=10,
        correct_count=7,
        weighted_error_count=3,
        false_insert_count=1,
        recognized_hotword_count=20,
    )
    assert metrics == {"recall_rate": 0.7, "error_rate": 0.3, "false_boost_rate": 0.05}
    assert (
        classify_hotword_candidate(expected_count=3, error_rate=0.2, manual_corrections=0)
        == "confirmed"
    )
    assert (
        classify_hotword_candidate(expected_count=2, error_rate=0.9, manual_corrections=1)
        == "suspected"
    )
    assert (
        classify_hotword_candidate(expected_count=1, error_rate=0.0, manual_corrections=2)
        == "confirmed"
    )
    assert priority_score(10, 0.3, 1.0, 1.0) == pytest.approx(71.94, abs=0.01)


@pytest.mark.parametrize(
    "term,category",
    [
        ("13800138000", "product"),
        ("京A12345", "product"),
        ("LHGCM82633A123456", "product"),
        ("张先生", "customer_name"),
        # 明显的姓名敬称不能通过伪造业务类别绕过阻断。
        ("张先生", "vehicle-model"),
    ],
)
def test_sensitive_terms_are_rejected(term: str, category: str) -> None:
    with pytest.raises(ApiError) as exc:
        ensure_hotword_is_not_sensitive(term, category)
    assert exc.value.code == "HOTWORD_SENSITIVE_TERM_FORBIDDEN"


def test_release_gate_applies_quality_latency_and_cost_thresholds() -> None:
    passed = evaluate_release_gate(
        baseline={
            "error_rate": 0.30,
            "recall_rate": 0.60,
            "false_boost_rate": 0.01,
            "cer": 0.10,
            "wer": 0.20,
            "downstream_f1": 0.80,
            "p95_latency_ms": 1000,
            "cost_per_minute": 0.10,
        },
        candidate={
            "trusted_occurrences": 40,
            "unique_terms": 4,
            "error_rate": 0.20,
            "recall_rate": 0.70,
            "false_boost_rate": 0.014,
            "cer": 0.101,
            "wer": 0.201,
            "downstream_f1": 0.798,
            "p95_latency_ms": 1040,
            "cost_per_minute": 0.104,
        },
    )
    assert passed["passed"] is True
    assert passed["blocked_reasons"] == []

    blocked = evaluate_release_gate(
        baseline={
            "error_rate": 0.30,
            "recall_rate": 0.60,
            "false_boost_rate": 0.01,
            "cer": 0.10,
            "wer": 0.20,
            "downstream_f1": 0.80,
            "p95_latency_ms": 1000,
            "cost_per_minute": 0.10,
        },
        candidate={
            "trusted_occurrences": 20,
            "unique_terms": 2,
            "error_rate": 0.29,
            "recall_rate": 0.61,
            "false_boost_rate": 0.02,
            "cer": 0.104,
            "wer": 0.204,
            "downstream_f1": 0.79,
            "p95_latency_ms": 1080,
            "cost_per_minute": 0.11,
        },
    )
    assert blocked["passed"] is False
    assert {
        "minimum_sample_size",
        "minimum_unique_terms",
        "hotword_improvement",
        "false_boost_regression",
        "cer_regression",
        "wer_regression",
        "downstream_f1_regression",
        "latency_regression",
        "cost_regression",
    }.issubset(blocked["blocked_reasons"])


def test_release_gate_blocks_missing_or_under_sampled_per_term_counts() -> None:
    assert _per_term_occurrences_gate_blocked(None, unique_terms=3) is True
    assert _per_term_occurrences_gate_blocked({"星越L": 3, "银河E8": 3}, unique_terms=3) is True
    assert (
        _per_term_occurrences_gate_blocked({"星越L": 3, "银河E8": 2, "领克08": 3}, unique_terms=3)
        is True
    )
    assert (
        _per_term_occurrences_gate_blocked({"星越L": 3, "银河E8": 4, "领克08": 5}, unique_terms=3)
        is False
    )


def test_hotword_completion_requires_diagnostics_but_legacy_run_remains_compatible() -> None:
    base_result = {
        "audio_session_id": "A-1001",
        "recording_id": "rec-1",
        "capability_statuses": {"asr": {"status": "success"}},
        "asr_segments": [{"start_ms": 0, "end_ms": 100, "text": "星越L"}],
    }
    legacy = RunRecord(
        run_id="run-legacy",
        tenant_id="aurora_auto",
        project_id="sales_qa",
        run_type="audio_intelligence",
        status="running",
        trace_id="trace-legacy",
        payload={
            "audio_session_id": "A-1001",
            "recording_id": "rec-1",
            "capabilities": ["asr"],
        },
    )
    assert validate_audio_intelligence_result(legacy, base_result) == base_result

    governed = RunRecord(
        run_id="run-hotword",
        tenant_id="aurora_auto",
        project_id="sales_qa",
        run_type="audio_intelligence",
        status="running",
        trace_id="trace-hotword",
        payload={
            "audio_session_id": "A-1001",
            "recording_id": "rec-1",
            "capabilities": ["asr"],
            "hotword_pack_version_id": "hwpv-1",
            "return_word_timestamps": True,
        },
    )
    with pytest.raises(ApiError) as exc:
        validate_audio_intelligence_result(governed, base_result)
    assert exc.value.code == "HOTWORD_DIAGNOSTICS_REQUIRED"

    governed_result = {
        **base_result,
        "word_timestamps_storage_object_id": "sto-word-ts-1",
        "diagnostics_storage_object_id": "sto-hotword-diagnostics-1",
        "hotword_diagnostics": {
            "hotword_pack_version_id": "hwpv-1",
            "matched_terms": ["星越L"],
            "missed_terms": [],
            "false_boosted_terms": [],
        },
    }
    with SessionLocal() as session:
        session.add(governed)
        for storage_object_id, content_sha256 in (
            ("sto-word-ts-1", "a" * 64),
            ("sto-hotword-diagnostics-1", "b" * 64),
        ):
            object_key = f"tenants/aurora_auto/projects/sales_qa/tests/{storage_object_id}.json"
            session.add(
                StorageObject(
                    storage_object_id=storage_object_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    provider="minio",
                    bucket="auris-flow-local",
                    object_key=object_key,
                    object_key_sha256=hashlib.sha256(object_key.encode("utf-8")).hexdigest(),
                    source_type="test_artifact",
                    source_id=governed.run_id,
                    content_type="application/json",
                    size_bytes=128,
                    content_sha256=content_sha256,
                    etag=f"etag-{storage_object_id}",
                    status="registered",
                    trace_id=governed.trace_id,
                    payload={"status": "registered"},
                )
            )
        session.flush()
        assert validate_audio_intelligence_result(governed, governed_result) == governed_result


@pytest.mark.parametrize("provider", ("minio", "s3", "obs", "oss"))
def test_real_storage_reference_uses_provider_head_and_matches_metadata(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_object_id = f"sto-real-head-{provider}"
    object_key = f"tenants/aurora_auto/projects/sales_qa/tests/{storage_object_id}.json"
    observed: list[tuple[str, str, str]] = []

    class HeadClient:
        def allows_bucket(self, bucket: str) -> bool:
            return bucket == "auris-flow-local"

        def head_object(self, bucket: str, remote_object_key: str) -> dict[str, object]:
            observed.append((provider, bucket, remote_object_key))
            return {"content_length": "128", "etag": '"artifact-etag"'}

    monkeypatch.setattr(
        audio_intelligence_service,
        "get_settings",
        lambda: SimpleNamespace(auris_object_storage_adapter="real"),
    )
    monkeypatch.setattr(
        audio_intelligence_service,
        "object_storage_client_for_provider",
        lambda selected_provider: (
            HeadClient()
            if selected_provider == provider
            else pytest.fail(f"unexpected provider: {selected_provider}")
        ),
    )
    with SessionLocal() as session:
        storage_object = StorageObject(
            storage_object_id=storage_object_id,
            tenant_id="aurora_auto",
            project_id="sales_qa",
            provider=provider,
            bucket="auris-flow-local",
            object_key=object_key,
            object_key_sha256=hashlib.sha256(object_key.encode("utf-8")).hexdigest(),
            source_type="test_artifact",
            source_id=storage_object_id,
            content_type="application/json",
            size_bytes=128,
            content_sha256="c" * 64,
            etag="artifact-etag",
            status="verified",
            trace_id="trace-real-head",
            payload={},
        )
        session.add(storage_object)
        session.flush()

        validated = audio_intelligence_service.validate_scoped_storage_object_reference(
            session,
            tenant_id="aurora_auto",
            project_id="sales_qa",
            storage_object_id=storage_object_id,
            purpose="远端测试产物",
        )

        assert validated is storage_object
        assert observed == [(provider, "auris-flow-local", object_key)]
        session.rollback()


@pytest.mark.parametrize(
    "remote,error_code",
    [
        ({"content_length": "127", "etag": "artifact-etag"}, "STORAGE_OBJECT_REMOTE_SIZE_MISMATCH"),
        ({"content_length": "128", "etag": "other-etag"}, "STORAGE_OBJECT_REMOTE_ETAG_MISMATCH"),
        (
            {"content_length": None, "etag": "artifact-etag"},
            "STORAGE_OBJECT_REMOTE_METADATA_INCOMPLETE",
        ),
    ],
)
def test_real_storage_reference_rejects_remote_metadata_mismatch(
    remote: dict[str, object], error_code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_object_id = f"sto-real-mismatch-{error_code.lower()}"
    object_key = f"tenants/aurora_auto/projects/sales_qa/tests/{storage_object_id}.json"

    class HeadClient:
        def allows_bucket(self, _bucket: str) -> bool:
            return True

        def head_object(self, _bucket: str, _object_key: str) -> dict[str, object]:
            return remote

    monkeypatch.setattr(audio_intelligence_service, "_real_object_storage_enabled", lambda: True)
    monkeypatch.setattr(
        audio_intelligence_service,
        "object_storage_client_for_provider",
        lambda _provider: HeadClient(),
    )
    with SessionLocal() as session:
        session.add(
            StorageObject(
                storage_object_id=storage_object_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                provider="minio",
                bucket="auris-flow-local",
                object_key=object_key,
                object_key_sha256=hashlib.sha256(object_key.encode("utf-8")).hexdigest(),
                source_type="test_artifact",
                source_id=storage_object_id,
                content_type="application/json",
                size_bytes=128,
                content_sha256="d" * 64,
                etag="artifact-etag",
                status="verified",
                trace_id="trace-real-mismatch",
                payload={},
            )
        )
        session.flush()

        with pytest.raises(ApiError) as exc:
            audio_intelligence_service.validate_scoped_storage_object_reference(
                session,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                storage_object_id=storage_object_id,
                purpose="远端测试产物",
            )

        assert exc.value.code == error_code
        session.rollback()
