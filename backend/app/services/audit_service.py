from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.redaction import redact_structured_value
from app.models import AuditLog

redact_audit_value = redact_structured_value


def record_audit(
    session: Session,
    ctx: RequestContext,
    *,
    action: str,
    object_type: str,
    object_id: str,
    result: str = "success",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> AuditLog:
    audit = AuditLog(
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        actor_id=ctx.user_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        result=result,
        trace_id=trace_id or ctx.trace_id,
        idempotency_key=ctx.idempotency_key,
        before_json=redact_audit_value(before) if before is not None else None,
        after_json=redact_audit_value(after) if after is not None else None,
    )
    session.add(audit)
    return audit
