from __future__ import annotations

from typing import Any

from app.core.context import RequestContext


def envelope(
    data: Any,
    ctx: RequestContext,
    *,
    meta: dict[str, Any] | None = None,
    links: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "data": data,
        "meta": {"trace_id": ctx.trace_id, "request_id": ctx.request_id, **(meta or {})},
    }
    if links:
        payload["links"] = links
    return payload


def collection_envelope(
    items: list[Any],
    ctx: RequestContext,
    *,
    total: int | None = None,
    limit: int | None = None,
    next_cursor: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return envelope(
        {"items": items},
        ctx,
        meta={
            "total": len(items) if total is None else total,
            "limit": limit or len(items),
            "next_cursor": next_cursor,
            **(meta or {}),
        },
    )
