from __future__ import annotations

import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import PlatformConnection
from app.services import connector_import_service

_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_CREDENTIAL_REFERENCE_PATTERN = re.compile(
    r"^secret://[A-Za-z][A-Za-z0-9._-]{0,63}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127}){1,4}$"
)
_ENUM_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_REQUEST_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,1023}$")
_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "id_token",
        "password",
        "passwd",
        "refresh_token",
        "secret",
        "secret_ref",
        "token",
    }
)
_SENSITIVE_SUFFIXES = (
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
)


class PlatformConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    provider_type: str = Field(min_length=2, max_length=64)
    auth_mode: str = Field(min_length=2, max_length=64)
    origin: str = Field(min_length=1, max_length=2048)
    credential_ref: str = Field(min_length=3, max_length=512)
    external_tenant_ref: str = Field(min_length=1, max_length=256)
    store_refs: list[str] = Field(default_factory=list, max_length=100)
    test_path: str = Field(default="/", min_length=1, max_length=1024)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @field_validator("provider_type", "auth_mode")
    @classmethod
    def validate_enum(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not _ENUM_PATTERN.fullmatch(normalized):
            raise ValueError("value must be a lowercase bounded identifier")
        return normalized

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        connector_import_service.validate_endpoint_syntax(normalized)
        return normalized

    @field_validator("credential_ref")
    @classmethod
    def validate_credential_ref(cls, value: str) -> str:
        normalized = value.strip()
        if not _CREDENTIAL_REFERENCE_PATTERN.fullmatch(normalized):
            raise ValueError("credential_ref must be a server-side secret:// reference")
        return normalized

    @field_validator("external_tenant_ref")
    @classmethod
    def validate_external_tenant_ref(cls, value: str) -> str:
        normalized = value.strip()
        if not _REFERENCE_PATTERN.fullmatch(normalized):
            raise ValueError("external_tenant_ref must be a bounded opaque reference")
        return normalized

    @field_validator("store_refs")
    @classmethod
    def validate_store_refs(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            item = value.strip() if isinstance(value, str) else ""
            if not item or not _REFERENCE_PATTERN.fullmatch(item):
                raise ValueError("store_refs must contain bounded opaque references")
            normalized.append(item)
        if len(normalized) != len(set(normalized)):
            raise ValueError("store_refs must be unique")
        return normalized

    @field_validator("test_path")
    @classmethod
    def validate_test_path(cls, value: str) -> str:
        normalized = value.strip()
        if (
            not _REQUEST_PATH_PATTERN.fullmatch(normalized)
            or normalized.startswith("//")
            or "\\" in normalized
            or any(part in {".", ".."} for part in normalized.split("/"))
        ):
            raise ValueError("test_path must be a safe absolute path")
        return normalized


class PlatformConnectionTestRequest(BaseModel):
    """Explicit empty command so runtime OpenAPI documents the request body."""

    model_config = ConfigDict(extra="forbid")


def reject_plaintext_credentials(
    payload: object,
    *,
    path: tuple[str, ...] = (),
) -> None:
    if isinstance(payload, list):
        for index, value in enumerate(payload):
            reject_plaintext_credentials(value, path=(*path, str(index)))
        return
    if not isinstance(payload, dict):
        return
    for raw_key, value in payload.items():
        key = str(raw_key)
        normalized = key.casefold().replace("-", "_")
        compact = "".join(character for character in normalized if character.isalnum())
        if compact == "credentialref":
            if not isinstance(value, str) or not _CREDENTIAL_REFERENCE_PATTERN.fullmatch(
                value.strip()
            ):
                raise ApiError(
                    "CREDENTIAL_REFERENCE_INVALID",
                    "credential_ref 必须引用服务端 secret:// 路径，不能承载凭证值",
                    422,
                    details=[
                        {
                            "field": ".".join((*path, key)),
                            "message": "use a server-side secret:// reference",
                            "code": "credential_reference_invalid",
                        }
                    ],
                )
            continue
        sensitive = normalized in _SENSITIVE_EXACT_KEYS or compact.endswith(_SENSITIVE_SUFFIXES)
        if sensitive:
            raise ApiError(
                "PLAINTEXT_CREDENTIAL_FORBIDDEN",
                "连接配置只能保存 credential_ref，不能保存明文凭证",
                422,
                details=[
                    {
                        "field": ".".join((*path, key)),
                        "message": "use credential_ref with a server-side secret:// reference",
                        "code": "plaintext_credential_forbidden",
                    }
                ],
            )
        reject_plaintext_credentials(value, path=(*path, key))


def parse_platform_connection_create(payload: object) -> PlatformConnectionCreate:
    if not isinstance(payload, dict):
        raise ApiError("PLATFORM_CONNECTION_INVALID", "平台连接配置必须是 JSON 对象", 422)
    reject_plaintext_credentials(payload)
    try:
        return PlatformConnectionCreate.model_validate(payload)
    except ValidationError as error:
        raise ApiError(
            "PLATFORM_CONNECTION_INVALID",
            "平台连接配置不完整或不合法",
            422,
            details=[
                {
                    "field": ".".join(str(part) for part in item["loc"]),
                    "message": str(item["msg"]),
                    "code": str(item["type"]),
                }
                for item in error.errors()
            ],
        ) from error


def platform_connection_payload(connection: PlatformConnection) -> dict[str, Any]:
    return {
        "platform_connection_id": connection.platform_connection_id,
        "name": connection.name,
        "provider_type": connection.provider_type,
        "auth_mode": connection.auth_mode,
        "origin": connection.origin,
        "credential_ref": connection.credential_ref,
        "external_tenant_ref": connection.external_tenant_ref,
        "store_refs": list(connection.store_refs or []),
        "test_path": connection.test_path,
        "status": connection.status,
        "resource_version": connection.resource_version,
        "last_test_status": connection.last_test_status,
        "last_tested_at": (
            connection.last_tested_at.isoformat() if connection.last_tested_at is not None else None
        ),
        "root_trace_id": connection.root_trace_id,
        "current_trace_id": connection.current_trace_id,
    }


def get_platform_connection(
    session: Session,
    ctx: RequestContext,
    platform_connection_id: str,
    *,
    for_update: bool = False,
) -> PlatformConnection:
    statement = select(PlatformConnection).where(
        PlatformConnection.platform_connection_id == platform_connection_id,
        PlatformConnection.tenant_id == ctx.tenant_id,
        PlatformConnection.project_id == ctx.project_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    connection = session.scalar(statement)
    if connection is None:
        raise ApiError(
            "NOT_FOUND",
            f"平台连接不存在：{platform_connection_id}",
            404,
        )
    return connection


def _encode_cursor(offset: int) -> str:
    return (
        urlsafe_b64encode(f"platform_connection:{offset}".encode("ascii"))
        .decode("ascii")
        .rstrip("=")
    )


def _decode_cursor(cursor: str | int | None) -> int:
    if cursor in (None, "", 0):
        return 0
    if isinstance(cursor, int):
        if cursor < 0:
            raise ApiError("INVALID_CURSOR", "cursor 不能为负数", 400)
        return cursor
    try:
        text = str(cursor)
        padded = text + "=" * (-len(text) % 4)
        decoded = urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        raise ApiError("INVALID_CURSOR", "cursor 格式无效", 400) from None
    prefix, _, raw_offset = decoded.partition(":")
    if prefix != "platform_connection" or not raw_offset.isdigit():
        raise ApiError("INVALID_CURSOR", "cursor 格式无效", 400)
    return int(raw_offset)


def list_platform_connections(
    session: Session,
    ctx: RequestContext,
    *,
    cursor: str | int | None,
    limit: int,
) -> tuple[list[PlatformConnection], int, str | None]:
    offset = _decode_cursor(cursor)
    scope = (
        PlatformConnection.tenant_id == ctx.tenant_id,
        PlatformConnection.project_id == ctx.project_id,
    )
    total = int(
        session.scalar(select(func.count()).select_from(PlatformConnection).where(*scope)) or 0
    )
    records = list(
        session.scalars(
            select(PlatformConnection)
            .where(*scope)
            .order_by(
                PlatformConnection.created_at.desc(),
                PlatformConnection.platform_connection_id.desc(),
            )
            .offset(offset)
            .limit(limit + 1)
        )
    )
    visible = records[:limit]
    next_cursor = _encode_cursor(offset + limit) if len(records) > limit else None
    return visible, total, next_cursor


def probe_platform_connection(connection: PlatformConnection) -> dict[str, object]:
    response_status, _ = connector_import_service.fetch_connector_json(
        {
            "base_url": connection.origin,
            "request_path": connection.test_path,
            "credential_ref": connection.credential_ref,
            "platform_connection_id": connection.platform_connection_id,
            "platform_scope": {
                "tenant_ref": connection.external_tenant_ref,
                "store_refs": list(connection.store_refs or []),
            },
        },
        limit=1,
        tenant_id=connection.tenant_id,
        project_id=connection.project_id,
    )
    return {"response_status": response_status}


def mark_connection_test_success(
    connection: PlatformConnection,
    ctx: RequestContext,
    *,
    tested_at: datetime,
) -> None:
    connection.status = "active"
    connection.last_test_status = "success"
    connection.last_tested_at = tested_at
    connection.resource_version += 1
    connection.current_trace_id = ctx.trace_id


def mark_connection_test_failure(
    connection: PlatformConnection,
    ctx: RequestContext,
    *,
    tested_at: datetime,
) -> None:
    connection.status = "error"
    connection.last_test_status = "failed"
    connection.last_tested_at = tested_at
    connection.resource_version += 1
    connection.current_trace_id = ctx.trace_id


def utc_now() -> datetime:
    return datetime.now(UTC)
