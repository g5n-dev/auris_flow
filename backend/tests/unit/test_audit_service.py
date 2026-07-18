from __future__ import annotations

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.models import AuditLog
from app.services.audit_service import record_audit, redact_audit_value


def test_record_audit_redacts_sensitive_fields_and_truncates_long_text():
    ctx = RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="u_admin_001",
        roles=("project_admin",),
        request_id="pytest-audit",
        trace_id="trace_audit_redaction",
        idempotency_key="audit-redaction",
    )
    with SessionLocal() as session:
        record_audit(
            session,
            ctx,
            action="settings.update",
            object_type="settings",
            object_id="provider",
            before={"api_token": "plain-token", "description": "old"},
            after={
                "Authorization": "Bearer secret",
                "nested": {"access_token": "raw-access-token"},
                "transcript_preview": "a" * 360,
            },
        )
        session.commit()

        audit = session.query(AuditLog).filter(AuditLog.trace_id == ctx.trace_id).one()
        assert audit.before_json == {"api_token": "[REDACTED]", "description": "old"}
        assert audit.after_json["Authorization"] == "[REDACTED]"
        assert audit.after_json["nested"]["access_token"] == "[REDACTED]"
        assert audit.after_json["transcript_preview"] == "[REDACTED_TEXT length=360]"


def test_redact_audit_value_removes_nested_pii_and_preserves_governed_references():
    payload = {
        "customer": {
            "name": "陈先生",
            "phone": "13800138000",
            "email": "customer@example.com",
        },
        "vehicle": {
            "plate_number": "京A12345",
            "vin": "LSVAA4187C2184847",
        },
        "notes": ("联系 13900139000 或 owner@example.cn，证件 11010519491231002X，车辆京B12345。"),
        "transcript": "客户说我的手机号是 13800138000",
        "raw_audio": b"RIFF-audio-bytes",
        "trace_id": "trace_audit_reference_001",
        "asset_key": "auris/audio/asset_001",
        "storage_object_id": "sto_audio_001",
        "object_key": "tenant-a/audio/13800138000.wav",
    }

    redacted = redact_audit_value(payload)

    assert redacted["customer"] == {
        "name": "[REDACTED_PII]",
        "phone": "[REDACTED_PII]",
        "email": "[REDACTED_PII]",
    }
    assert redacted["vehicle"] == {
        "plate_number": "[REDACTED_PII]",
        "vin": "[REDACTED_PII]",
    }
    assert redacted["notes"] == (
        "联系 [REDACTED_PHONE] 或 [REDACTED_EMAIL]，"
        "证件 [REDACTED_IDENTITY]，车辆[REDACTED_PLATE]。"
    )
    assert redacted["transcript"] == "[REDACTED_TEXT length=21]"
    assert redacted["raw_audio"] == "[REDACTED_TEXT length=16]"
    assert redacted["trace_id"] == payload["trace_id"]
    assert redacted["asset_key"] == payload["asset_key"]
    assert redacted["storage_object_id"] == payload["storage_object_id"]
    assert redacted["object_key"] == "tenant-a/audio/[REDACTED_PHONE].wav"


def test_redact_audit_value_bounds_nested_collections_and_binary_values():
    payload = {
        "items": list(range(55)),
        "binary_blob": b"0123456789",
        "wide": {f"field_{index}": index for index in range(105)},
    }

    redacted = redact_audit_value(payload)

    assert len(redacted["items"]) == 51
    assert redacted["items"][-1] == {"__truncated_items__": 5}
    assert redacted["binary_blob"] == "[REDACTED_BINARY length=10]"
    assert redacted["wide"]["__truncated_fields__"] == 5
