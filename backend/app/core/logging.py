from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.context import RequestContext
from app.core.redaction import redact_structured_value

LOGGER_NAME = "auris_flow"
_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(level.upper())
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.handlers = [handler]
        root.propagate = False
        _CONFIGURED = True


def get_logger(component: str) -> logging.Logger:
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(f"{LOGGER_NAME}.{component}")


def _context_payload(ctx: RequestContext | None) -> dict[str, Any]:
    if not ctx:
        return {}
    return {
        "tenant_id": ctx.tenant_id,
        "project_id": ctx.project_id,
        "user_id": ctx.user_id,
        "request_id": ctx.request_id,
        "trace_id": ctx.trace_id,
        "idempotency_key": ctx.idempotency_key,
    }


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    ctx: RequestContext | None = None,
    **fields: Any,
) -> None:
    # Imported lazily to keep logging usable during early configuration failures.
    from app.core.observability import current_trace_context

    safe_fields = redact_structured_value(
        {**_context_payload(ctx), **fields},
        field_name="log_fields",
    )
    if not isinstance(safe_fields, dict):
        safe_fields = {"fields": safe_fields}
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "level": logging.getLevelName(level),
        "event": event,
        "component": logger.name.removeprefix(f"{LOGGER_NAME}."),
        **safe_fields,
        # Active W3C identifiers are authoritative and cannot be forged by a caller field.
        **current_trace_context(),
    }
    logger.log(level, json.dumps(payload, ensure_ascii=False, default=str))
