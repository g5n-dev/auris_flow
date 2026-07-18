from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from fastapi import Request
from sqlalchemy import insert, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.logging import get_logger, log_event
from app.models import IdempotencyRecord

logger = get_logger("idempotency")

ReservationScope = tuple[str, str, str, str]
Reservation = tuple[str, str]
IDEMPOTENCY_RESERVATIONS_SESSION_KEY = "idempotency_reservations"


async def request_hash(request: Request) -> str:
    body = await request.body()
    query_items = sorted(request.query_params.multi_items())
    fingerprint = {
        "method": request.method.upper(),
        "path": request.url.path,
        "query": query_items,
        "body_sha256": hashlib.sha256(body or b"{}").hexdigest(),
    }
    encoded = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def require_idempotency(ctx: RequestContext) -> None:
    if not ctx.idempotency_key:
        log_event(logger, "idempotency.missing_key", level=30, ctx=ctx)
        raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "写操作必须提供 Idempotency-Key", 400)


def _idempotency_key(ctx: RequestContext, explicit_key: str | None) -> str:
    key = explicit_key or ctx.idempotency_key
    if not key:
        require_idempotency(ctx)
    return key or ""


def _scope(ctx: RequestContext, operation: str, idempotency_key: str) -> ReservationScope:
    return (ctx.tenant_id, ctx.project_id, operation, idempotency_key)


def _reservations(session: Session) -> dict[ReservationScope, Reservation]:
    reservations = session.info.setdefault(IDEMPOTENCY_RESERVATIONS_SESSION_KEY, {})
    return reservations  # type: ignore[no-any-return]


def _record_for_scope(
    session: Session,
    ctx: RequestContext,
    *,
    operation: str,
    idempotency_key: str,
) -> IdempotencyRecord | None:
    return session.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.tenant_id == ctx.tenant_id,
            IdempotencyRecord.project_id == ctx.project_id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _insert_reservation_if_absent(session: Session, values: dict[str, Any]) -> None:
    bind = session.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        sqlite_statement = sqlite_insert(IdempotencyRecord).values(**values)
        session.execute(
            sqlite_statement.on_conflict_do_nothing(
                index_elements=[
                    "tenant_id",
                    "project_id",
                    "operation",
                    "idempotency_key",
                ]
            )
        )
        return
    if dialect in {"mysql", "mariadb"}:
        mysql_statement = mysql_insert(IdempotencyRecord).values(**values)
        session.execute(
            mysql_statement.on_duplicate_key_update(owner_token=IdempotencyRecord.owner_token)
        )
        return

    connection = session.connection()
    try:
        with connection.begin_nested():
            connection.execute(insert(IdempotencyRecord).values(**values))
    except IntegrityError:
        pass


def _raise_hash_conflict(ctx: RequestContext, *, operation: str) -> None:
    log_event(logger, "idempotency.conflict", level=30, ctx=ctx, operation=operation)
    raise ApiError(
        "IDEMPOTENCY_KEY_CONFLICT",
        "同一个 Idempotency-Key 不能复用到不同请求",
        409,
    )


def replay_or_conflict(
    session: Session,
    ctx: RequestContext,
    *,
    operation: str,
    body_hash: str,
    idempotency_key: str | None = None,
) -> dict[str, Any] | None:
    key = _idempotency_key(ctx, idempotency_key)
    scope = _scope(ctx, operation, key)
    reservations = _reservations(session)
    local_reservation = reservations.get(scope)
    if local_reservation is not None:
        _, reserved_hash = local_reservation
        if reserved_hash != body_hash:
            _raise_hash_conflict(ctx, operation=operation)
        return None

    owner_token = uuid.uuid4().hex
    _insert_reservation_if_absent(
        session,
        {
            "tenant_id": ctx.tenant_id,
            "project_id": ctx.project_id,
            "user_id": ctx.user_id,
            "operation": operation,
            "idempotency_key": key,
            "request_hash": body_hash,
            "status_code": 0,
            "response_json": {},
            "state": "in_progress",
            "owner_token": owner_token,
        },
    )
    record = _record_for_scope(
        session,
        ctx,
        operation=operation,
        idempotency_key=key,
    )
    if record is None:
        raise RuntimeError("idempotency reservation was not persisted")
    if record.owner_token == owner_token:
        reservations[scope] = (owner_token, body_hash)
        log_event(logger, "idempotency.reserved", ctx=ctx, operation=operation)
        return None
    if record.request_hash != body_hash:
        _raise_hash_conflict(ctx, operation=operation)
    if record.state != "completed":
        raise ApiError(
            "IDEMPOTENCY_REQUEST_IN_PROGRESS",
            "同一个 Idempotency-Key 的请求仍在处理中",
            409,
            retryable=True,
        )
    log_event(
        logger, "idempotency.replay", ctx=ctx, operation=operation, status_code=record.status_code
    )
    return json.loads(json.dumps(record.response_json, ensure_ascii=False, default=str))


def save_idempotency_result(
    session: Session,
    ctx: RequestContext,
    *,
    operation: str,
    body_hash: str,
    status_code: int,
    response_json: dict[str, Any],
    idempotency_key: str | None = None,
) -> None:
    key = _idempotency_key(ctx, idempotency_key)
    scope = _scope(ctx, operation, key)
    reservations = _reservations(session)
    reservation = reservations.get(scope)
    record = _record_for_scope(
        session,
        ctx,
        operation=operation,
        idempotency_key=key,
    )
    if record is None:
        replay = replay_or_conflict(
            session,
            ctx,
            operation=operation,
            body_hash=body_hash,
            idempotency_key=key,
        )
        if replay is not None:
            raise ApiError(
                "IDEMPOTENCY_RESULT_ALREADY_FINALIZED",
                "幂等请求结果已经完成，不能被覆盖",
                409,
            )
        reservation = _reservations(session).get(scope)
        record = _record_for_scope(
            session,
            ctx,
            operation=operation,
            idempotency_key=key,
        )
    if record is None:
        raise RuntimeError("idempotency reservation was not found")
    if record.request_hash != body_hash:
        _raise_hash_conflict(ctx, operation=operation)

    serialized_response = json.loads(json.dumps(response_json, ensure_ascii=False, default=str))
    if record.state == "completed":
        if record.status_code == status_code and record.response_json == serialized_response:
            reservations.pop(scope, None)
            return
        raise ApiError(
            "IDEMPOTENCY_RESULT_ALREADY_FINALIZED",
            "幂等请求结果已经完成，不能被覆盖",
            409,
        )
    if reservation is None or record.owner_token != reservation[0]:
        raise ApiError(
            "IDEMPOTENCY_REQUEST_IN_PROGRESS",
            "同一个 Idempotency-Key 的请求仍在处理中",
            409,
            retryable=True,
        )
    record.status_code = status_code
    record.response_json = serialized_response
    record.state = "completed"
    reservations.pop(scope, None)
    log_event(logger, "idempotency.saved", ctx=ctx, operation=operation, status_code=status_code)
