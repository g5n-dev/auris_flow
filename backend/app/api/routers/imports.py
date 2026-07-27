from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from app.api.deps import ContextDep, SessionDep
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.schemas import parse_payload
from app.services import connector_import_service
from app.services.audit_service import record_audit
from app.services.connector_import_service import (
    ConnectorProbeRequest,
    connector_probe_snapshot,
    lock_connector_after_probe,
    preview_mapping_status,
    preview_records,
    validate_platform_audio_connector,
)
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.import_batch_service import (
    get_import_batch,
    import_batch_item_payload,
    import_batch_payload,
    list_import_batch_items,
)
from app.services.outbox_service import enqueue_event
from app.services.resource_service import get_resource

router = APIRouter(tags=["imports"])
CONNECTOR_PROBE_ROLES = ("project_admin", "asset_manager")


async def _probe_connector(
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
    connector_id: str,
    *,
    preview: bool,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        CONNECTOR_PROBE_ROLES,
        action="connectors.record_preview" if preview else "connectors.connection_test",
    )
    probe = parse_payload(ConnectorProbeRequest, await request.json())
    body_hash = await request_hash(request)
    operation = (
        f"connectors.record_preview:{connector_id}"
        if preview
        else f"connectors.connection_test:{connector_id}"
    )
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay

    connector = get_resource(session, ctx, "connectors", connector_id)
    definition = validate_platform_audio_connector(session, ctx, connector.data)
    connector_version, semantic_sha256 = connector_probe_snapshot(
        definition,
        connector.data,
    )
    normalized_connector = definition.model_dump(exclude_none=True)
    live_cursor = connector.data.get("sync_cursor")
    if isinstance(live_cursor, str) and live_cursor:
        normalized_connector["cursor_policy"] = {
            **normalized_connector["cursor_policy"],
            "cursor_value": live_cursor,
        }
    # No connector row lock is held while waiting on the external platform.
    # A locking, populate-existing read compares this immutable guard
    # immediately after the network call.
    response_status, source_payload = connector_import_service.fetch_connector_json(
        normalized_connector,
        limit=probe.limit if preview else 1,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
    )
    connector = lock_connector_after_probe(
        session,
        ctx,
        connector_id,
        expected_connector_version=connector_version,
        expected_semantic_sha256=semantic_sha256,
    )
    observed_at = datetime.now(UTC).isoformat()
    before = dict(connector.data)
    if preview:
        records = preview_records(
            normalized_connector,
            source_payload,
            limit=probe.limit,
        )
        mapping_valid, mapping_errors = preview_mapping_status(
            normalized_connector,
            source_payload,
            limit=probe.limit,
        )
        fields = sorted({field for record in records for field in record})
        result: dict[str, Any] = {
            "connector_id": connector_id,
            "status": "success",
            "record_count": len(records),
            "records": records,
            "fields": fields,
            "mapping_valid": mapping_valid,
            "mapping_errors": mapping_errors,
            "previewed_at": observed_at,
        }
        safe_observation = {
            "status": "success",
            "record_count": len(records),
            "mapping_valid": mapping_valid,
            "mapping_errors": mapping_errors,
            "connector_version": connector_version,
            "previewed_at": observed_at,
        }
        connector.data = {
            **connector.data,
            "last_record_preview": safe_observation,
        }
        action = "connectors.record_preview"
        event_type = "connector.records_previewed"
    else:
        result = {
            "connector_id": connector_id,
            "status": "success",
            "response_status": response_status,
            "tested_at": observed_at,
        }
        safe_observation = {
            "status": "success",
            "response_status": response_status,
            "connector_version": connector_version,
            "tested_at": observed_at,
        }
        connector.data = {
            **connector.data,
            "last_connection_test": safe_observation,
        }
        action = "connectors.connection_test"
        event_type = "connector.connection_tested"
    connector.trace_id = ctx.trace_id
    record_audit(
        session,
        ctx,
        action=action,
        object_type="connector",
        object_id=connector_id,
        before=before,
        after=safe_observation,
    )
    enqueue_event(
        session,
        ctx,
        event_type=event_type,
        aggregate_type="connector",
        aggregate_id=connector_id,
        payload={
            "connector_id": connector_id,
            "connector_version": connector.data.get("connector_version"),
            **safe_observation,
        },
    )
    response = envelope(result, ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        response_json=response,
    )
    session.commit()
    return response


@router.post("/connectors/{connector_id}/connection-tests")
async def post_connector_connection_tests(
    connector_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    return await _probe_connector(
        request,
        session,
        ctx,
        connector_id,
        preview=False,
    )


@router.post("/connectors/{connector_id}/record-previews")
async def post_connector_record_previews(
    connector_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    return await _probe_connector(
        request,
        session,
        ctx,
        connector_id,
        preview=True,
    )


@router.get("/import-batches/{import_batch_id}")
def get_import_batches_by_id(
    import_batch_id: str,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    return envelope(
        import_batch_payload(get_import_batch(session, ctx, import_batch_id)),
        ctx,
    )


@router.get("/import-batches/{import_batch_id}/items")
def get_import_batch_items(
    import_batch_id: str,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    items = [
        import_batch_item_payload(item)
        for item in list_import_batch_items(session, ctx, import_batch_id)
    ]
    return collection_envelope(items, ctx, total=len(items), limit=len(items))
