from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from app.core.database import SessionLocal
from app.models import OutboxEvent, RunCompletionReceipt, RunRecord, StorageObject

ROOT = Path(__file__).resolve().parents[3]
COMPLETION_BODY_SHA256 = "b" * 64
COMPLETION_REQUEST_SHA256 = "c" * 64
COMPLETION_RECEIPT_SHA256 = "d" * 64


def _load_helper() -> ModuleType:
    path = ROOT / "scripts" / "process_e2e_outbox_run.py"
    spec = importlib.util.spec_from_file_location("auris_process_e2e_outbox_run", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _insert_dispatch(
    *,
    event_status: str = "processed",
    processed_event_id_offset: int = 0,
    dispatch: dict | None = None,
) -> tuple[str, int, dict]:
    run_id = "label_opt_e2e_dispatch_oracle"
    dispatch = dispatch or {
        "adapter": "dagster",
        "operation": "run_request",
        "status": "success",
        "details": {
            "external_run_id": "fake_dagster_run_e2e_oracle",
            "run_id": run_id,
        },
    }
    with SessionLocal.begin() as session:
        event = OutboxEvent(
            tenant_id="aurora_auto",
            project_id="sales_qa",
            event_type="agent_run.requested",
            aggregate_type="label_optimization",
            aggregate_id=run_id,
            status=event_status,
            payload={"adapter_dispatch": dispatch},
            dispatch_idempotency_key="outbox_v1_e2e_dispatch_oracle",
            attempt_count=1,
        )
        session.add(event)
        session.flush()
        event_id = event.event_id
        session.add(
            RunRecord(
                run_id=run_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                run_type="label_optimization",
                status="submitted",
                trace_id="trace_e2e_dispatch_oracle",
                payload={
                    "business_status": "awaiting_completion",
                    "business_completion_required": True,
                    "processed_event_id": event_id + processed_event_id_offset,
                    "dispatch": dispatch,
                },
            )
        )
    return run_id, event_id, dispatch


def test_read_only_dispatch_evidence_is_scoped_and_does_not_process(monkeypatch) -> None:
    helper = _load_helper()
    run_id, event_id, dispatch = _insert_dispatch()
    monkeypatch.setattr(
        helper,
        "process_aggregate_events",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("read-only evidence must not compete with the managed worker")
        ),
    )

    evidence = helper.read_run_dispatch_evidence(
        run_id,
        tenant_id="aurora_auto",
        project_id="sales_qa",
    )

    assert evidence == {
        "run_id": run_id,
        "run_type": "label_optimization",
        "run_status": "submitted",
        "business_status": "awaiting_completion",
        "business_completion_required": True,
        "event_id": event_id,
        "event_status": "processed",
        "adapter": "dagster",
        "external_id": "fake_dagster_run_e2e_oracle",
        "dispatch": dispatch,
    }


@pytest.mark.parametrize(
    ("tenant_id", "project_id"),
    [("other_tenant", "sales_qa"), ("aurora_auto", "other_project")],
)
def test_read_only_dispatch_evidence_rejects_scope_mismatch(
    tenant_id: str,
    project_id: str,
) -> None:
    helper = _load_helper()
    run_id, _event_id, _dispatch = _insert_dispatch()

    with pytest.raises(SystemExit, match="scoped run not found"):
        helper.read_run_dispatch_evidence(
            run_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )


def test_read_only_dispatch_evidence_rejects_unprocessed_event() -> None:
    helper = _load_helper()
    run_id, _event_id, _dispatch = _insert_dispatch(event_status="pending")

    with pytest.raises(SystemExit, match="not processed"):
        helper.read_run_dispatch_evidence(
            run_id,
            tenant_id="aurora_auto",
            project_id="sales_qa",
        )


def test_read_only_dispatch_evidence_rejects_processed_event_drift() -> None:
    helper = _load_helper()
    run_id, _event_id, _dispatch = _insert_dispatch(processed_event_id_offset=1000)

    with pytest.raises(SystemExit, match="processed outbox event not found"):
        helper.read_run_dispatch_evidence(
            run_id,
            tenant_id="aurora_auto",
            project_id="sales_qa",
        )


def test_read_only_dispatch_evidence_requires_adapter_external_identity() -> None:
    helper = _load_helper()
    run_id, _event_id, _dispatch = _insert_dispatch(
        dispatch={
            "adapter": "dagster",
            "operation": "run_request",
            "status": "success",
            "details": {},
        }
    )

    with pytest.raises(SystemExit, match="external identity"):
        helper.read_run_dispatch_evidence(
            run_id,
            tenant_id="aurora_auto",
            project_id="sales_qa",
        )


@pytest.mark.parametrize("dispatch_status", [None, "failed"])
def test_read_only_dispatch_evidence_requires_explicit_success_status(
    dispatch_status: str | None,
) -> None:
    helper = _load_helper()
    dispatch = {
        "adapter": "dagster",
        "operation": "run_request",
        "details": {"external_run_id": "fake_dagster_run_e2e_oracle"},
    }
    if dispatch_status is not None:
        dispatch["status"] = dispatch_status
    run_id, _event_id, _dispatch = _insert_dispatch(dispatch=dispatch)

    with pytest.raises(SystemExit, match="external identity"):
        helper.read_run_dispatch_evidence(
            run_id,
            tenant_id="aurora_auto",
            project_id="sales_qa",
        )


def _insert_completion_evidence(
    *,
    include_storage: bool = True,
    business_status: str = "completed",
    request_status: str = "success",
    run_type: str = "hotword_build",
    run_status: str = "success",
) -> tuple[str, str]:
    run_id = "hotword_build_e2e_completion_oracle"
    receipt_id = "e2e_complete_hotword_build_oracle"
    external_id = "fake_dagster_run_completion_oracle"
    nonce = "e2e-completion-oracle-nonce"
    root_trace_id = "trace_e2e_completion_root"
    descriptor = {
        "storage_object_id": "storage_e2e_completion_oracle",
        "role": "manifest",
        "provider": "minio",
        "bucket": "auris-flow-local",
        "object_key": (f"tenants/aurora_auto/projects/sales_qa/runs/{run_id}/manifest.json"),
        "content_type": "application/json",
        "size_bytes": 128,
        "content_sha256": "e" * 64,
        "etag": None,
    }
    result_ref = {"storage_objects": [descriptor]} if include_storage else {}
    request_body = {
        "adapter": "dagster",
        "status": request_status,
        "completion_receipt_id": receipt_id,
        "external_id": external_id,
        "result_ref": result_ref,
    }
    auth = {
        "auth_mode": "signed_external_completion",
        "signature_key_id": "auris-e2e-completion",
        "authenticated_source": "dagster",
        "authenticated_tenant_id": "aurora_auto",
        "authenticated_project_id": "sales_qa",
        "signature_binding_mode": "scoped_key_map",
        "signature_mode": "hmac-sha256",
        "nonce": nonce,
        "request_sha256": COMPLETION_REQUEST_SHA256,
        "body_sha256": COMPLETION_BODY_SHA256,
        "signed_at": "1784690000",
    }
    completion_receipt = {
        "completion_receipt_id": receipt_id,
        "receipt_hash": COMPLETION_RECEIPT_SHA256,
        "adapter": "dagster",
        "external_id": external_id,
        "source": "dagster",
        "status": run_status,
        "result_ref": result_ref,
        "auth": auth,
    }
    public_response = {
        "data": {
            "run_id": run_id,
            "tenant_id": "aurora_auto",
            "project_id": "sales_qa",
            "run_type": run_type,
            "status": run_status,
            "business_status": business_status,
            "business_completion_required": False,
            "completion_receipt": {
                "completion_receipt_id": receipt_id,
                "status": run_status,
            },
        },
        "meta": {"trace_id": "trace_e2e_completion_request"},
    }
    with SessionLocal.begin() as session:
        session.add(
            RunRecord(
                run_id=run_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                run_type=run_type,
                status=run_status,
                trace_id=root_trace_id,
                payload={
                    "business_status": business_status,
                    "business_completion_required": False,
                    "root_trace_id": root_trace_id,
                    "completion_receipt": completion_receipt,
                },
            )
        )
        session.flush()
        session.add(
            RunCompletionReceipt(
                tenant_id="aurora_auto",
                project_id="sales_qa",
                completion_receipt_id=receipt_id,
                run_id=run_id,
                receipt_hash=COMPLETION_RECEIPT_SHA256,
                processing_state="completed",
                processing_token="processing-token-completion-oracle",
                completion_status=run_status,
                status_code=200,
                adapter="dagster",
                source="dagster",
                external_id=external_id,
                request_body=request_body,
                response_json=public_response,
                signature_key_id="auris-e2e-completion",
                authenticated_source="dagster",
                signature_nonce=nonce,
                signature_request_hash=COMPLETION_REQUEST_SHA256,
                signature_body_hash=COMPLETION_BODY_SHA256,
                signature_mode="hmac-sha256",
                signed_at="1784690000",
                request_id="request-e2e-completion-oracle",
                request_trace_id="trace_e2e_completion_request",
                run_trace_id=root_trace_id,
                completed_at=datetime.now(UTC),
            )
        )
        if include_storage:
            session.add(
                StorageObject(
                    storage_object_id=descriptor["storage_object_id"],
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    provider=descriptor["provider"],
                    bucket=descriptor["bucket"],
                    object_key=descriptor["object_key"],
                    object_key_sha256="f" * 64,
                    source_type=run_type,
                    source_id=run_id,
                    content_type=descriptor["content_type"],
                    size_bytes=descriptor["size_bytes"],
                    content_sha256=descriptor["content_sha256"],
                    etag=descriptor["etag"],
                    status="verified",
                    trace_id=root_trace_id,
                    payload={
                        "role": "manifest",
                        "root_trace_id": root_trace_id,
                        "run_id": run_id,
                        "run_type": run_type,
                    },
                )
            )
    return run_id, receipt_id


def _read_completion(helper: ModuleType, run_id: str, receipt_id: str) -> dict:
    return helper.read_completion_receipt_evidence(
        run_id,
        tenant_id="aurora_auto",
        project_id="sales_qa",
        completion_receipt_id=receipt_id,
        expected_adapter="dagster",
        expected_external_id="fake_dagster_run_completion_oracle",
        expected_signature_key_id="auris-e2e-completion",
        expected_source="dagster",
        expected_body_sha256=COMPLETION_BODY_SHA256,
        expected_nonce="e2e-completion-oracle-nonce",
    )


def test_read_completion_evidence_is_scoped_minimal_and_storage_bound() -> None:
    helper = _load_helper()
    run_id, receipt_id = _insert_completion_evidence()

    evidence = _read_completion(helper, run_id, receipt_id)

    assert evidence == {
        "verified": True,
        "run_id": run_id,
        "run_type": "hotword_build",
        "run_status": "success",
        "business_status": "completed",
        "business_completion_required": False,
        "completion_receipt_id": receipt_id,
        "completion_status": "success",
        "receipt_state": "completed",
        "status_code": 200,
        "auth": {
            "auth_mode": "signed_external_completion",
            "binding_mode": "scoped_key_map",
            "signature_mode": "hmac-sha256",
            "key_id": "auris-e2e-completion",
            "source": "dagster",
            "tenant_id": "aurora_auto",
            "project_id": "sales_qa",
            "body_sha256": COMPLETION_BODY_SHA256,
        },
        "storage_objects": [
            {
                "ordinal": 0,
                "role": "manifest",
                "content_sha256": "e" * 64,
                "source_type": "hotword_build",
                "source_id": run_id,
                "status": "verified",
                "trace_id": "trace_e2e_completion_root",
            }
        ],
    }
    encoded = str(evidence)
    assert "fake_dagster_run_completion_oracle" not in encoded
    assert "e2e-completion-oracle-nonce" not in encoded
    assert COMPLETION_REQUEST_SHA256 not in encoded


def test_read_completion_evidence_allows_terminal_execution_awaiting_business_review() -> None:
    helper = _load_helper()
    run_id, receipt_id = _insert_completion_evidence(
        include_storage=False,
        business_status="awaiting-review",
        run_type="label_optimization",
        run_status="success",
    )

    evidence = _read_completion(helper, run_id, receipt_id)

    assert evidence["run_type"] == "label_optimization"
    assert evidence["run_status"] == "success"
    assert evidence["business_status"] == "awaiting-review"
    assert evidence["business_completion_required"] is False


@pytest.mark.parametrize(
    ("run_type", "run_status", "business_status"),
    [
        ("hotword_build", "success", "completed"),
        ("hotword_build", "failed", "failed"),
        ("eval_run", "blocked", "blocked"),
        ("release_command", "blocked", "blocked"),
        ("label_optimization", "success", "awaiting-review"),
    ],
)
def test_terminal_business_state_policy_accepts_only_real_domain_terminal_states(
    run_type: str,
    run_status: str,
    business_status: str,
) -> None:
    helper = _load_helper()

    assert helper.is_valid_terminal_business_state(
        run_type,
        run_status,
        business_status,
    )


@pytest.mark.parametrize(
    ("run_type", "run_status", "business_status"),
    [
        ("hotword_build", "success", "evaluating"),
        ("hotword_build", "success", "future-terminal-state"),
        ("hotword_build", "failed", "completed"),
        ("hotword_build", "blocked", "awaiting-review"),
        ("label_optimization", "success", "evaluating"),
        ("unknown_external_run", "success", "completed"),
    ],
)
def test_read_completion_evidence_rejects_unrecognized_terminal_business_state(
    run_type: str,
    run_status: str,
    business_status: str,
) -> None:
    helper = _load_helper()
    run_id, receipt_id = _insert_completion_evidence(
        include_storage=False,
        business_status=business_status,
        request_status="failed" if run_status == "failed" else "success",
        run_type=run_type,
        run_status=run_status,
    )

    with pytest.raises(SystemExit, match="terminal business state"):
        _read_completion(helper, run_id, receipt_id)


@pytest.mark.parametrize(
    ("tenant_id", "project_id"),
    [("other_tenant", "sales_qa"), ("aurora_auto", "other_project")],
)
def test_read_completion_evidence_rejects_scope_drift(
    tenant_id: str,
    project_id: str,
) -> None:
    helper = _load_helper()
    run_id, receipt_id = _insert_completion_evidence(include_storage=False)

    with pytest.raises(SystemExit, match="scoped completed run not found"):
        helper.read_completion_receipt_evidence(
            run_id,
            tenant_id=tenant_id,
            project_id=project_id,
            completion_receipt_id=receipt_id,
            expected_adapter="dagster",
            expected_external_id="fake_dagster_run_completion_oracle",
            expected_signature_key_id="auris-e2e-completion",
            expected_source="dagster",
            expected_body_sha256=COMPLETION_BODY_SHA256,
            expected_nonce="e2e-completion-oracle-nonce",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("signature_body_hash", "0" * 64, "signed evidence drift"),
        ("processing_state", "processing", "not finalized"),
        ("external_id", "other-external-run", "identity drift"),
    ],
)
def test_read_completion_evidence_fails_closed_on_receipt_drift(
    field: str,
    value: str,
    message: str,
) -> None:
    helper = _load_helper()
    run_id, receipt_id = _insert_completion_evidence(include_storage=False)
    with SessionLocal.begin() as session:
        receipt = session.query(RunCompletionReceipt).filter_by(run_id=run_id).one()
        setattr(receipt, field, value)

    with pytest.raises(SystemExit, match=message):
        _read_completion(helper, run_id, receipt_id)


def test_read_completion_evidence_rejects_storage_registration_drift() -> None:
    helper = _load_helper()
    run_id, receipt_id = _insert_completion_evidence()
    with SessionLocal.begin() as session:
        storage = session.get(StorageObject, "storage_e2e_completion_oracle")
        assert storage is not None
        storage.source_id = "other-run"

    with pytest.raises(SystemExit, match="trusted registration evidence"):
        _read_completion(helper, run_id, receipt_id)


def test_read_completion_evidence_rejects_receipt_hash_drift() -> None:
    helper = _load_helper()
    run_id, receipt_id = _insert_completion_evidence(include_storage=False)
    with SessionLocal.begin() as session:
        receipt = session.query(RunCompletionReceipt).filter_by(run_id=run_id).one()
        receipt.receipt_hash = "0" * 64

    with pytest.raises(SystemExit, match="signed evidence drift"):
        _read_completion(helper, run_id, receipt_id)


def test_read_completion_evidence_rejects_public_business_state_drift() -> None:
    helper = _load_helper()
    run_id, receipt_id = _insert_completion_evidence(include_storage=False)
    with SessionLocal.begin() as session:
        receipt = session.query(RunCompletionReceipt).filter_by(run_id=run_id).one()
        response_json = dict(receipt.response_json)
        response_json["data"] = {
            **response_json["data"],
            "business_status": "evaluating",
        }
        receipt.response_json = response_json

    with pytest.raises(SystemExit, match="public response drift"):
        _read_completion(helper, run_id, receipt_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [("trace_id", "other-trace"), ("object_version_id", "other-version")],
)
def test_read_completion_evidence_rejects_storage_trace_or_version_drift(
    field: str,
    value: str,
) -> None:
    helper = _load_helper()
    run_id, receipt_id = _insert_completion_evidence()
    with SessionLocal.begin() as session:
        storage = session.get(StorageObject, "storage_e2e_completion_oracle")
        assert storage is not None
        if field == "trace_id":
            storage.trace_id = value
        else:
            storage.payload = {**storage.payload, "object_version_id": value}

    with pytest.raises(SystemExit, match="trusted registration evidence"):
        _read_completion(helper, run_id, receipt_id)
