from __future__ import annotations

import hmac
import ipaddress
import json
import re
import socket
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request as UrlRequest

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.http_transport import open_url_no_redirect
from app.models import ImportBatch, JsonResource, PlatformConnection
from app.services.audit_service import record_audit
from app.services.outbox_service import enqueue_event

AUDIO_IMPORT_SOURCE_TYPE = "platform_audio_url_api"
AUDIO_IMPORT_EXECUTION_CONTRACT = "auris-flow-audio-import-v1"
MAX_CONNECTOR_RESPONSE_BYTES = 256 * 1024
CONNECTOR_HTTP_TIMEOUT_SECONDS = 5.0
_REFERENCE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._:/-]{2,511}$")
_CREDENTIAL_REFERENCE_PATTERN = re.compile(
    r"^secret://[A-Za-z][A-Za-z0-9._-]{0,63}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127}){1,4}$"
)
_FIELD_PATH_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,255}$")
_REQUEST_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,1023}$")
_SAFE_CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key", "x-auth-token"})
_SENSITIVE_PREVIEW_FIELD = re.compile(
    r"(?:authorization|credential|password|secret|signature|token)",
    re.IGNORECASE,
)
_URL_PREVIEW_FIELD = re.compile(r"(?:^|[._-])url(?:$|[._-])", re.IGNORECASE)
_IMPORT_MAPPING_REQUIRED = frozenset({"external_record_id", "audio_url", "started_at"})
_IMPORT_MAPPING_OPTIONAL = frozenset({"duration_ms", "store_ref", "agent_ref", "device_ref"})
_IMPORT_MAPPING_ALLOWED = _IMPORT_MAPPING_REQUIRED | _IMPORT_MAPPING_OPTIONAL
_IMPORT_CONNECTOR_SEMANTIC_FIELDS = frozenset(
    {
        "connector_id",
        "source_type",
        "platform_connection_id",
        "credential_ref",
        "base_url",
        "request_path",
        "platform_scope",
        "pagination",
        "field_mapping",
        "cursor_policy",
        "target_asset_key",
        "dedupe_policy",
    }
)
_IMPORT_CONNECTOR_CLIENT_FIELDS = frozenset(
    {
        "connector_id",
        "name",
        "description",
        "source_type",
        "platform_connection_id",
        "credential_ref",
        "base_url",
        "request_path",
        "platform_scope",
        "pagination",
        "field_mapping",
        "cursor_policy",
        "target_asset_key",
        "dedupe_policy",
        "status",
        "scene_profile_id",
        "scene_profile_version_id",
        "scene_profile_snapshot_sha256",
    }
)
_CONNECTOR_RUNTIME_CURSOR_FIELDS = (
    "sync_cursor",
    "sync_cursor_connector_version",
    "sync_cursor_import_batch_id",
    "sync_cursor_trace_id",
)


class PlatformScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_ref: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._:/-]+$")
    store_refs: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("store_refs")
    @classmethod
    def validate_store_refs(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            item = value.strip()
            if not item or len(item) > 256 or not _REFERENCE_PATTERN.fullmatch(item):
                raise ValueError("store_refs must contain bounded opaque references")
            normalized.append(item)
        if len(set(normalized)) != len(normalized):
            raise ValueError("store_refs must be unique")
        return normalized


class CursorPagination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["cursor"] = "cursor"
    page_size: int = Field(default=100, ge=1, le=250)
    cursor_param: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    next_cursor_path: str = Field(min_length=1, max_length=256)

    @field_validator("next_cursor_path")
    @classmethod
    def validate_next_cursor_path(cls, value: str) -> str:
        normalized = value.strip()
        if not _FIELD_PATH_PATTERN.fullmatch(normalized):
            raise ValueError("next_cursor_path must be a dotted field path")
        return normalized


class CursorPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=256)
    initial_window_start: str = Field(min_length=1, max_length=64)
    cursor_value: str | None = Field(default=None, max_length=1024)

    @field_validator("field")
    @classmethod
    def validate_field(cls, value: str) -> str:
        normalized = value.strip()
        if not _FIELD_PATH_PATTERN.fullmatch(normalized):
            raise ValueError("field must be a dotted field path")
        return normalized

    @field_validator("initial_window_start")
    @classmethod
    def validate_initial_window_start(cls, value: str) -> str:
        normalized = value.strip()
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("initial_window_start must be ISO 8601") from exc
        if parsed.tzinfo is None:
            raise ValueError("initial_window_start must include a timezone")
        return normalized

    @field_validator("cursor_value")
    @classmethod
    def validate_cursor_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(ord(character) < 0x20 for character in value):
            raise ValueError("cursor_value must not contain control characters")
        return value


class PlatformAudioConnectorDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    source_type: Literal["platform_audio_url_api"]
    platform_connection_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )
    credential_ref: str = Field(min_length=3, max_length=512)
    base_url: str = Field(min_length=1, max_length=2048)
    request_path: str = Field(min_length=1, max_length=1024)
    platform_scope: PlatformScope
    pagination: CursorPagination
    field_mapping: dict[str, str]
    cursor_policy: CursorPolicy
    target_asset_key: str = Field(min_length=1, max_length=512)
    dedupe_policy: Literal["external_id_checksum"] = "external_id_checksum"
    status: Literal["draft", "active", "disabled"] = "draft"

    @field_validator("credential_ref")
    @classmethod
    def validate_credential_ref(cls, value: str) -> str:
        normalized = value.strip()
        if not _CREDENTIAL_REFERENCE_PATTERN.fullmatch(normalized):
            raise ValueError(
                "credential_ref must use a secret:// namespace and server-side reference path"
            )
        return normalized

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        validate_endpoint_syntax(normalized)
        return normalized

    @field_validator("request_path")
    @classmethod
    def validate_request_path(cls, value: str) -> str:
        normalized = value.strip()
        if (
            not _REQUEST_PATH_PATTERN.fullmatch(normalized)
            or normalized.startswith("//")
            or "\\" in normalized
            or any(part in {".", ".."} for part in normalized.split("/"))
        ):
            raise ValueError("request_path must be a safe absolute path")
        return normalized

    @field_validator("field_mapping")
    @classmethod
    def validate_field_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        keys = set(value)
        if not _IMPORT_MAPPING_REQUIRED.issubset(keys) or keys - _IMPORT_MAPPING_ALLOWED:
            raise ValueError("field_mapping requires external_record_id, audio_url and started_at")
        normalized: dict[str, str] = {}
        for target, source in value.items():
            source_path = source.strip() if isinstance(source, str) else ""
            if not _FIELD_PATH_PATTERN.fullmatch(source_path):
                raise ValueError(f"field_mapping.{target} must be a dotted field path")
            normalized[target] = source_path
        return normalized

    @model_validator(mode="after")
    def require_store_mapping_for_scoped_import(
        self,
    ) -> PlatformAudioConnectorDefinition:
        if self.platform_scope.store_refs and "store_ref" not in self.field_mapping:
            raise ValueError(
                "field_mapping.store_ref is required when platform_scope.store_refs is configured"
            )
        return self


class ConnectorProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=3, ge=1, le=3)


def _validation_details(error: ValidationError) -> list[dict[str, Any]]:
    return [
        {
            "field": ".".join(str(part) for part in item["loc"]),
            "message": str(item["msg"]),
            "code": str(item["type"]),
        }
        for item in error.errors()
    ]


def validate_endpoint_syntax(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.port is not None
        and not 1 <= parsed.port <= 65535
    ):
        raise ValueError("base_url must be an HTTPS origin without credentials, query or fragment")
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise ValueError("base_url host is not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("base_url must not target a non-public IP address")


def _resolved_addresses(
    hostname: str,
    port: int,
    *,
    resolver: Any = socket.getaddrinfo,
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        rows = resolver(hostname, port, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror):
        raise ApiError(
            "CONNECTOR_ENDPOINT_UNREACHABLE",
            "连接器地址无法解析或访问",
            503,
            retryable=True,
        ) from None
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for row in rows:
        try:
            addresses.add(ipaddress.ip_address(row[4][0]))
        except (IndexError, TypeError, ValueError):
            raise ApiError(
                "CONNECTOR_ENDPOINT_UNSAFE",
                "连接器地址未通过网络安全校验",
                422,
            ) from None
    if not addresses or any(not address.is_global for address in addresses):
        raise ApiError(
            "CONNECTOR_ENDPOINT_UNSAFE",
            "连接器地址未通过网络安全校验",
            422,
        )
    return addresses


def validate_public_endpoint(
    url: str,
    *,
    resolver: Any = socket.getaddrinfo,
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        validate_endpoint_syntax(url)
    except (ValueError, TypeError):
        raise ApiError(
            "CONNECTOR_ENDPOINT_UNSAFE",
            "连接器地址未通过网络安全校验",
            422,
        ) from None
    parsed = urlsplit(url)
    return _resolved_addresses(
        parsed.hostname or "",
        parsed.port or 443,
        resolver=resolver,
    )


def build_connector_endpoint(base_url: str, request_path: str) -> str:
    try:
        validate_endpoint_syntax(base_url)
    except (ValueError, TypeError):
        raise ApiError(
            "CONNECTOR_ENDPOINT_UNSAFE",
            "连接器地址未通过网络安全校验",
            422,
        ) from None
    if (
        not _REQUEST_PATH_PATTERN.fullmatch(request_path)
        or request_path.startswith("//")
        or "\\" in request_path
        or any(part in {".", ".."} for part in request_path.split("/"))
    ):
        raise ApiError(
            "CONNECTOR_REQUEST_PATH_UNSAFE",
            "连接器请求路径未通过安全校验",
            422,
        )
    parsed = urlsplit(base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, request_path, "", ""))


def resolve_credential_headers(
    credential_ref: str,
    *,
    serialized_bindings: str | None = None,
    tenant_id: str,
    project_id: str,
    platform_connection_id: str,
    platform_tenant_ref: str,
    base_url: str,
) -> dict[str, str]:
    serialized = (
        get_settings().platform_credential_bindings
        if serialized_bindings is None
        else serialized_bindings
    )
    binding: Any = None
    try:
        bindings = json.loads(serialized or "{}")
        binding = bindings.get(credential_ref) if isinstance(bindings, dict) else None
        raw_headers = binding.get("headers") if isinstance(binding, dict) else None
    except (TypeError, ValueError):
        bindings = None
        raw_headers = None
    if not isinstance(bindings, dict):
        raise ApiError(
            "CONNECTOR_CREDENTIAL_CONFIGURATION_INVALID",
            "连接器凭证配置不可用",
            503,
        )
    if binding is None:
        raise ApiError(
            "CONNECTOR_CREDENTIAL_UNAVAILABLE",
            "连接器凭证引用未配置",
            409,
        )
    if not isinstance(binding, dict):
        raise ApiError(
            "CONNECTOR_CREDENTIAL_CONFIGURATION_INVALID",
            "连接器凭证配置不可用",
            503,
        )
    required_binding_keys = {
        "tenant_id",
        "project_id",
        "platform_connection_id",
        "platform_tenant_ref",
        "base_url",
        "headers",
    }
    if set(binding) != required_binding_keys:
        raise ApiError(
            "CONNECTOR_CREDENTIAL_CONFIGURATION_INVALID",
            "连接器凭证配置不可用",
            503,
        )
    expected_binding = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "platform_connection_id": platform_connection_id,
        "platform_tenant_ref": platform_tenant_ref,
        "base_url": base_url.rstrip("/"),
    }
    configured_binding = {key: binding.get(key) for key in expected_binding}
    if any(not isinstance(value, str) for value in configured_binding.values()):
        raise ApiError(
            "CONNECTOR_CREDENTIAL_CONFIGURATION_INVALID",
            "连接器凭证配置不可用",
            503,
        )
    if any(
        not hmac.compare_digest(
            (
                str(configured_binding[key]).rstrip("/")
                if key == "base_url"
                else str(configured_binding[key])
            ),
            expected,
        )
        for key, expected in expected_binding.items()
    ):
        raise ApiError(
            "CONNECTOR_CREDENTIAL_SCOPE_MISMATCH",
            "连接器凭证引用未绑定当前租户、项目、平台连接或平台范围",
            409,
        )
    if not isinstance(raw_headers, dict) or not 1 <= len(raw_headers) <= 3:
        raise ApiError(
            "CONNECTOR_CREDENTIAL_CONFIGURATION_INVALID",
            "连接器凭证配置不可用",
            503,
        )
    headers: dict[str, str] = {}
    for raw_name, raw_value in raw_headers.items():
        if not isinstance(raw_name, str) or raw_name.casefold() not in _SAFE_CREDENTIAL_HEADERS:
            raise ApiError(
                "CONNECTOR_CREDENTIAL_CONFIGURATION_INVALID",
                "连接器凭证配置不可用",
                503,
            )
        if (
            not isinstance(raw_value, str)
            or not raw_value
            or len(raw_value.encode("utf-8")) > 16 * 1024
            or "\r" in raw_value
            or "\n" in raw_value
        ):
            raise ApiError(
                "CONNECTOR_CREDENTIAL_CONFIGURATION_INVALID",
                "连接器凭证配置不可用",
                503,
            )
        headers[raw_name] = raw_value
    return headers


def read_bounded_json_response(response: Any) -> dict[str, Any]:
    status = int(getattr(response, "status", 0) or getattr(response, "code", 0) or 0)
    if 300 <= status < 400:
        raise ApiError(
            "CONNECTOR_REDIRECT_FORBIDDEN",
            "连接器响应包含不允许的重定向",
            422,
        )
    if status and not 200 <= status < 300:
        raise ApiError(
            "CONNECTOR_UPSTREAM_REJECTED",
            "外部平台拒绝了连接器请求",
            502,
            retryable=status >= 500,
        )
    content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
    if content_type != "application/json" and not content_type.endswith("+json"):
        raise ApiError(
            "CONNECTOR_RESPONSE_NOT_JSON",
            "外部平台未返回 JSON 数据",
            422,
        )
    raw = response.read(MAX_CONNECTOR_RESPONSE_BYTES + 1)
    if len(raw) > MAX_CONNECTOR_RESPONSE_BYTES:
        raise ApiError(
            "CONNECTOR_RESPONSE_TOO_LARGE",
            "外部平台响应超过预览大小限制",
            413,
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError(
            "CONNECTOR_RESPONSE_INVALID_JSON",
            "外部平台返回的 JSON 无法解析",
            422,
        ) from None
    if not isinstance(payload, dict):
        raise ApiError(
            "CONNECTOR_RESPONSE_INVALID_SHAPE",
            "外部平台返回的数据结构不受支持",
            422,
        )
    return payload


def fetch_connector_json(
    connector: dict[str, object],
    *,
    limit: int,
    tenant_id: str,
    project_id: str,
) -> tuple[int, dict[str, Any]]:
    endpoint = build_connector_endpoint(
        str(connector.get("base_url") or ""),
        str(connector.get("request_path") or ""),
    )
    validate_public_endpoint(str(connector.get("base_url") or ""))
    query: dict[str, str | int] = {"limit": limit}
    pagination = connector.get("pagination")
    cursor_policy = connector.get("cursor_policy")
    if isinstance(pagination, dict) and isinstance(cursor_policy, dict):
        cursor_value = cursor_policy.get("cursor_value") or cursor_policy.get(
            "initial_window_start"
        )
        cursor_param = pagination.get("cursor_param")
        if isinstance(cursor_value, str) and cursor_value and isinstance(cursor_param, str):
            query[cursor_param] = cursor_value
    request_url = f"{endpoint}?{urlencode(query)}"
    platform_scope = connector.get("platform_scope")
    platform_tenant_ref = (
        platform_scope.get("tenant_ref") if isinstance(platform_scope, dict) else ""
    )
    headers = {
        "Accept": "application/json",
        "User-Agent": "auris-flow-connector-probe/1.0",
        **resolve_credential_headers(
            str(connector.get("credential_ref") or ""),
            tenant_id=tenant_id,
            project_id=project_id,
            platform_connection_id=str(connector.get("platform_connection_id") or ""),
            platform_tenant_ref=str(platform_tenant_ref or ""),
            base_url=str(connector.get("base_url") or ""),
        ),
    }
    request = UrlRequest(request_url, method="GET", headers=headers)
    try:
        with open_url_no_redirect(request, CONNECTOR_HTTP_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 200))
            return status, read_bounded_json_response(response)
    except HTTPError as error:
        return int(error.code), read_bounded_json_response(error)
    except (TimeoutError, URLError, OSError):
        raise ApiError(
            "CONNECTOR_ENDPOINT_UNREACHABLE",
            "连接器地址无法解析或访问",
            503,
            retryable=True,
        ) from None


def _resource(
    session: Session,
    ctx: RequestContext,
    connector_id: str,
    *,
    for_update: bool = False,
) -> JsonResource:
    statement = select(JsonResource).where(
        JsonResource.tenant_id == ctx.tenant_id,
        JsonResource.project_id == ctx.project_id,
        JsonResource.collection == "connectors",
        JsonResource.resource_key == connector_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    connector = session.scalar(statement)
    if connector is None:
        raise ApiError("NOT_FOUND", f"connectors 不存在：{connector_id}", 404)
    return connector


def _strong_definition_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        field: data[field]
        for field in PlatformAudioConnectorDefinition.model_fields
        if field in data
    }


def validate_platform_audio_connector(
    session: Session,
    ctx: RequestContext,
    data: dict[str, Any],
) -> PlatformAudioConnectorDefinition:
    try:
        definition = PlatformAudioConnectorDefinition.model_validate(
            _strong_definition_payload(data)
        )
    except ValidationError as error:
        raise ApiError(
            "CONNECTOR_CONFIGURATION_INVALID",
            "平台音频连接器配置不完整或不合法",
            422,
            details=_validation_details(error),
        ) from error
    platform_connection = session.scalar(
        select(PlatformConnection).where(
            PlatformConnection.platform_connection_id == definition.platform_connection_id,
            PlatformConnection.tenant_id == ctx.tenant_id,
            PlatformConnection.project_id == ctx.project_id,
        )
    )
    if platform_connection is None or platform_connection.status != "active":
        raise ApiError(
            "CONNECTOR_PLATFORM_CONNECTION_INVALID",
            "平台音频连接器必须绑定当前项目已验证的平台连接",
            422,
        )
    if definition.platform_connection_id == definition.connector_id:
        raise ApiError(
            "CONNECTOR_PLATFORM_CONNECTION_INVALID",
            "平台音频连接器不能绑定自身",
            422,
        )
    binding_matches = (
        hmac.compare_digest(platform_connection.origin.rstrip("/"), definition.base_url.rstrip("/"))
        and hmac.compare_digest(
            platform_connection.credential_ref,
            definition.credential_ref,
        )
        and hmac.compare_digest(
            platform_connection.external_tenant_ref,
            definition.platform_scope.tenant_ref,
        )
    )
    allowed_store_refs = set(platform_connection.store_refs or [])
    requested_store_refs = set(definition.platform_scope.store_refs)
    if not binding_matches or (
        allowed_store_refs and not requested_store_refs.issubset(allowed_store_refs)
    ):
        raise ApiError(
            "CONNECTOR_PLATFORM_CONNECTION_SCOPE_MISMATCH",
            "连接器的地址、凭证引用或平台范围与所选平台连接不一致",
            409,
        )
    target = _resource_for_asset(session, ctx, definition.target_asset_key)
    if target.data.get("asset_key") != definition.target_asset_key:
        raise ApiError("NOT_FOUND", f"data_assets 不存在：{definition.target_asset_key}", 404)
    return definition


def _resource_for_asset(
    session: Session,
    ctx: RequestContext,
    asset_key: str,
) -> JsonResource:
    asset = session.scalar(
        select(JsonResource).where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "data_assets",
            JsonResource.resource_key == asset_key,
        )
    )
    if asset is None:
        raise ApiError("NOT_FOUND", f"data_assets 不存在：{asset_key}", 404)
    return asset


def _published_import_task_version_ids(
    session: Session,
    ctx: RequestContext,
    connector_id: str,
) -> list[str]:
    """Return every TaskVersion that has ever frozen this connector.

    A frozen server-owned snapshot is the durable publication marker.  Looking
    only at the current status would incorrectly unlock a connector after a
    future deprecation transition.
    """

    versions = session.scalars(
        select(JsonResource).where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "task_versions",
        )
    )
    task_version_ids: list[str] = []
    for version in versions:
        data = version.data if isinstance(version.data, dict) else {}
        if str(data.get("task_type_id") or "") != "audio-platform-import":
            continue
        snapshot = data.get("connector_snapshot")
        snapshot_connector_id = (
            str(snapshot.get("connector_id") or "").strip() if isinstance(snapshot, dict) else ""
        )
        has_publication_marker = isinstance(snapshot, dict) and bool(
            str(data.get("connector_snapshot_sha256") or "").strip()
        )
        current_or_historic_publication = (
            version.status == "published"
            or data.get("status") == "published"
            or bool(data.get("published_at"))
        )
        bound_connector_id = snapshot_connector_id or str(data.get("connector_id") or "").strip()
        if (
            bound_connector_id == connector_id
            and has_publication_marker
            and current_or_historic_publication
        ):
            task_version_ids.append(version.resource_key)
    return sorted(task_version_ids)


def _semantic_connector_payload(
    definition: PlatformAudioConnectorDefinition,
) -> dict[str, Any]:
    normalized = definition.model_dump(exclude_none=True)
    return {field: normalized.get(field) for field in sorted(_IMPORT_CONNECTOR_SEMANTIC_FIELDS)}


def connector_probe_snapshot(
    definition: PlatformAudioConnectorDefinition,
    data: dict[str, Any],
) -> tuple[int, str]:
    connector_version = data.get("connector_version")
    if (
        isinstance(connector_version, bool)
        or not isinstance(connector_version, int)
        or connector_version < 1
    ):
        raise ApiError(
            "CONNECTOR_VERSION_INVALID",
            "连接器版本状态不合法，不能执行连接测试或记录预览",
            409,
        )
    canonical_semantics = json.dumps(
        _semantic_connector_payload(definition),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    import hashlib

    return connector_version, hashlib.sha256(canonical_semantics).hexdigest()


def lock_connector_after_probe(
    session: Session,
    ctx: RequestContext,
    connector_id: str,
    *,
    expected_connector_version: int,
    expected_semantic_sha256: str,
) -> JsonResource:
    connector = _resource(session, ctx, connector_id, for_update=True)
    definition = validate_platform_audio_connector(session, ctx, connector.data)
    connector_version, semantic_sha256 = connector_probe_snapshot(
        definition,
        connector.data,
    )
    if connector_version != expected_connector_version or not hmac.compare_digest(
        semantic_sha256, expected_semantic_sha256
    ):
        raise ApiError(
            "CONNECTOR_CHANGED_DURING_PROBE",
            "连接器在测试或预览期间已被修改，请基于最新配置重新执行",
            409,
            details=[
                {
                    "connector_id": connector_id,
                    "expected_connector_version": expected_connector_version,
                    "current_connector_version": connector_version,
                }
            ],
        )
    return connector


def prepare_platform_audio_connector_payload(
    session: Session,
    ctx: RequestContext,
    payload: dict[str, Any],
    *,
    existing_payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    existing_source_type = (existing_payload or {}).get("source_type")
    source_type = existing_source_type or payload.get("source_type")
    if source_type != AUDIO_IMPORT_SOURCE_TYPE:
        return None
    if existing_payload is not None:
        existing_resource_id = str(
            existing_payload.get("id") or existing_payload.get("connector_id") or ""
        ).strip()
        existing_connector_id = str(existing_payload.get("connector_id") or "").strip()
        requested_connector_id = payload.get("connector_id")
        if (
            not existing_resource_id
            or not existing_connector_id
            or existing_resource_id != existing_connector_id
        ):
            raise ApiError(
                "CONNECTOR_ID_BINDING_INVALID",
                "连接器资源 ID 与配置 ID 不一致，不能继续修改",
                409,
            )
        if requested_connector_id is not None and (
            not isinstance(requested_connector_id, str)
            or not hmac.compare_digest(
                requested_connector_id.strip(),
                existing_connector_id,
            )
        ):
            raise ApiError(
                "CONNECTOR_ID_IMMUTABLE",
                "连接器 ID 创建后不可修改",
                409,
                details=[
                    {
                        "field": "connector_id",
                        "expected": existing_connector_id,
                    }
                ],
            )
    supplied_cursor_policy = payload.get("cursor_policy")
    if isinstance(supplied_cursor_policy, dict) and "cursor_value" in supplied_cursor_policy:
        raise ApiError(
            "CONNECTOR_CURSOR_SERVER_OWNED",
            "同步游标由平台在成功导入后推进，客户端不能写入",
            422,
            details=[{"field": "cursor_policy.cursor_value"}],
        )
    unknown_fields = sorted(set(payload) - _IMPORT_CONNECTOR_CLIENT_FIELDS)
    if unknown_fields:
        raise ApiError(
            "VALIDATION_ERROR",
            "请求参数校验失败",
            422,
            details=[
                {
                    "field": field,
                    "message": "Extra inputs are not permitted",
                    "code": "extra_forbidden",
                }
                for field in unknown_fields
            ],
        )
    if existing_payload is not None:
        connector_id = str(existing_payload.get("connector_id") or "").strip()
        locked_connector = _resource(
            session,
            ctx,
            connector_id,
            for_update=True,
        )
        existing_payload = locked_connector.data
        active_batch_id = session.scalar(
            select(ImportBatch.import_batch_id)
            .where(
                ImportBatch.tenant_id == ctx.tenant_id,
                ImportBatch.project_id == ctx.project_id,
                ImportBatch.connector_id == connector_id,
                ImportBatch.status.in_(("queued", "running")),
            )
            .order_by(ImportBatch.created_at)
            .limit(1)
        )
        if active_batch_id is not None:
            raise ApiError(
                "CONNECTOR_IMPORT_ALREADY_ACTIVE",
                "连接器存在执行中的导入批次，完成前不能修改配置",
                409,
                details=[
                    {
                        "connector_id": connector_id,
                        "import_batch_id": active_batch_id,
                    }
                ],
            )
    merged = {**(existing_payload or {}), **payload}
    requested_id = merged.get("connector_id") or merged.get("id")
    if requested_id is not None:
        merged["connector_id"] = requested_id
    definition = validate_platform_audio_connector(session, ctx, merged)
    normalized = definition.model_dump(exclude_none=True)
    current_version = int((existing_payload or {}).get("connector_version") or 0)
    semantic_changes: list[str] = []
    if existing_payload is not None:
        existing_definition = validate_platform_audio_connector(
            session,
            ctx,
            existing_payload,
        )
        previous_semantics = _semantic_connector_payload(existing_definition)
        requested_semantics = _semantic_connector_payload(definition)
        semantic_changes = sorted(
            field
            for field in _IMPORT_CONNECTOR_SEMANTIC_FIELDS
            if previous_semantics.get(field) != requested_semantics.get(field)
        )
        published_task_version_ids = (
            _published_import_task_version_ids(
                session,
                ctx,
                existing_definition.connector_id,
            )
            if semantic_changes
            else []
        )
        if published_task_version_ids:
            raise ApiError(
                "CONNECTOR_PUBLISHED_SEMANTICS_IMMUTABLE",
                "连接器已被发布的导入任务冻结；请新建连接器配置后发布新任务版本",
                409,
                details=[
                    {
                        "connector_id": existing_definition.connector_id,
                        "fields": semantic_changes,
                        "task_version_ids": published_task_version_ids,
                    }
                ],
            )
    next_version = (
        1
        if existing_payload is None
        else current_version + 1
        if semantic_changes
        else current_version
    )
    return {
        **normalized,
        "connector_version": next_version,
        **{
            field: existing_payload[field]
            for field in _CONNECTOR_RUNTIME_CURSOR_FIELDS
            if existing_payload is not None and field in existing_payload
        },
    }


def freeze_connector_snapshot(
    session: Session,
    ctx: RequestContext,
    connector_id: str,
) -> tuple[dict[str, Any], str, str, str]:
    connector = _resource(session, ctx, connector_id, for_update=True)
    definition = validate_platform_audio_connector(session, ctx, connector.data)
    if definition.status == "disabled":
        raise ApiError(
            "CONNECTOR_DISABLED",
            "已停用的连接器不能发布或执行导入任务",
            409,
        )
    connector_version = int(connector.data.get("connector_version") or 0)
    connection_test = connector.data.get("last_connection_test")
    record_preview = connector.data.get("last_record_preview")
    verified_connection = (
        isinstance(connection_test, dict)
        and connection_test.get("status") == "success"
        and connection_test.get("connector_version") == connector_version
    )
    verified_preview = (
        isinstance(record_preview, dict)
        and record_preview.get("status") == "success"
        and record_preview.get("mapping_valid") is True
        and record_preview.get("connector_version") == connector_version
        and isinstance(record_preview.get("record_count"), int)
        and not isinstance(record_preview.get("record_count"), bool)
        and int(record_preview["record_count"]) > 0
    )
    if not verified_connection or not verified_preview:
        raise ApiError(
            "TASK_IMPORT_CONNECTOR_NOT_VERIFIED",
            "发布前必须对当前连接器版本完成真实连通性测试和非空记录预览",
            409,
            details=[
                {
                    "connector_id": connector_id,
                    "connector_version": connector_version,
                    "connection_tested": verified_connection,
                    "record_previewed": verified_preview,
                }
            ],
        )
    snapshot = {
        "connector_id": definition.connector_id,
        "connector_version": str(connector_version),
        "platform_connection_id": definition.platform_connection_id,
        "platform_scope": definition.platform_scope.model_dump(),
        "source_type": definition.source_type,
        "base_url": definition.base_url,
        "request_path": definition.request_path,
        "credential_ref": definition.credential_ref,
        "pagination": definition.pagination.model_dump(),
        "field_mapping": definition.field_mapping,
        "cursor_policy": definition.cursor_policy.model_dump(
            exclude={"cursor_value"},
            exclude_none=True,
        ),
    }
    canonical = json.dumps(
        snapshot,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    import hashlib

    return (
        snapshot,
        hashlib.sha256(canonical).hexdigest(),
        definition.dedupe_policy,
        definition.target_asset_key,
    )


def advance_connector_sync_cursor(
    session: Session,
    ctx: RequestContext,
    connector_id: str,
    *,
    expected_cursor: str | None,
    next_cursor: str | None,
    import_batch_id: str,
    trace_id: str,
) -> None:
    """Advance live cursor state with compare-and-set semantics.

    The published TaskVersion intentionally contains only cursor policy. The
    mutable value lives at `connectors.data.sync_cursor` and may advance only in
    the same transaction that fully materializes a successful import batch.
    """

    if next_cursor is not None and (
        not isinstance(next_cursor, str)
        or len(next_cursor) > 1024
        or any(ord(character) < 0x20 for character in next_cursor)
    ):
        raise ApiError(
            "CONNECTOR_CURSOR_STATE_INVALID",
            "待推进的连接器同步游标不合法",
            409,
        )
    connector = session.scalar(
        select(JsonResource)
        .where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "connectors",
            JsonResource.resource_key == connector_id,
        )
        .with_for_update()
    )
    if connector is None:
        raise ApiError(
            "TASK_IMPORT_CONNECTOR_NOT_FOUND",
            "导入批次绑定的连接器不存在",
            409,
        )
    current_cursor = connector.data.get("sync_cursor")
    normalized_current = current_cursor if isinstance(current_cursor, str) else None
    if current_cursor is not None and normalized_current is None:
        raise ApiError(
            "CONNECTOR_CURSOR_STATE_INVALID",
            "连接器同步游标状态不合法",
            409,
        )
    connector_version = connector.data.get("connector_version")
    if (
        not isinstance(connector_version, int)
        or isinstance(connector_version, bool)
        or connector_version < 1
    ):
        raise ApiError(
            "CONNECTOR_CURSOR_STATE_INVALID",
            "连接器版本状态不合法",
            409,
        )
    cursor_connector_version = connector.data.get("sync_cursor_connector_version")
    if normalized_current is None:
        if cursor_connector_version is not None:
            raise ApiError(
                "CONNECTOR_CURSOR_STATE_INVALID",
                "连接器同步游标缺失但仍包含游标版本",
                409,
            )
    elif (
        not isinstance(cursor_connector_version, int)
        or isinstance(cursor_connector_version, bool)
        or cursor_connector_version != connector_version
    ):
        raise ApiError(
            "CONNECTOR_CURSOR_VERSION_MISMATCH",
            "连接器同步游标不属于当前连接器版本",
            409,
            details=[
                {
                    "connector_id": connector_id,
                    "connector_version": connector_version,
                    "cursor_connector_version": cursor_connector_version,
                }
            ],
        )
    if normalized_current != expected_cursor:
        raise ApiError(
            "CONNECTOR_CURSOR_CONFLICT",
            "连接器同步游标已被其他导入批次推进",
            409,
            details=[
                {
                    "connector_id": connector_id,
                    "import_batch_id": import_batch_id,
                }
            ],
        )
    if next_cursor == normalized_current:
        return
    before = {
        "sync_cursor": normalized_current,
        "sync_cursor_connector_version": cursor_connector_version,
        "sync_cursor_import_batch_id": connector.data.get("sync_cursor_import_batch_id"),
    }
    connector.data = {
        **connector.data,
        "sync_cursor": next_cursor,
        "sync_cursor_connector_version": connector_version if next_cursor is not None else None,
        "sync_cursor_import_batch_id": import_batch_id,
        "sync_cursor_trace_id": trace_id,
    }
    connector.trace_id = trace_id
    after = {
        "sync_cursor": next_cursor,
        "sync_cursor_connector_version": (connector_version if next_cursor is not None else None),
        "sync_cursor_import_batch_id": import_batch_id,
    }
    record_audit(
        session,
        ctx,
        action="connectors.sync_cursor_advance",
        object_type="connector",
        object_id=connector_id,
        before=before,
        after=after,
        trace_id=trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="connector.sync_cursor_advanced",
        aggregate_type="connector",
        aggregate_id=connector_id,
        payload={
            "connector_id": connector_id,
            "import_batch_id": import_batch_id,
            "cursor_advanced": True,
            "connector_version": connector_version,
            "trace_id": trace_id,
        },
    )


def _extract_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: Any = payload.get("records")
    if candidates is None:
        candidates = payload.get("items")
    if candidates is None:
        data = payload.get("data")
        candidates = data.get("records") if isinstance(data, dict) else data
    if not isinstance(candidates, list):
        raise ApiError(
            "CONNECTOR_RESPONSE_INVALID_SHAPE",
            "外部平台返回的数据结构不受支持",
            422,
        )
    return [item for item in candidates if isinstance(item, dict)]


def _field_value(record: dict[str, Any], path: str) -> Any:
    current: Any = record
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _public_scalar(value: Any, *, maximum: int = 512) -> str | int | float | bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value[:maximum]
    return None


def _public_source_record(
    source_record: dict[str, Any],
    *,
    maximum_fields: int = 50,
) -> dict[str, Any]:
    flattened: dict[str, Any] = {}

    def visit(value: Any, path: str, depth: int) -> None:
        if len(flattened) >= maximum_fields or depth > 4:
            return
        if isinstance(value, dict):
            for raw_key, nested in value.items():
                if not isinstance(raw_key, str):
                    continue
                next_path = f"{path}.{raw_key}" if path else raw_key
                if _SENSITIVE_PREVIEW_FIELD.search(next_path):
                    continue
                visit(nested, next_path, depth + 1)
            return
        if not path:
            return
        if _URL_PREVIEW_FIELD.search(path):
            flattened[path] = isinstance(value, str) and bool(value.strip())
            return
        public_value = _public_scalar(value)
        if public_value is not None:
            flattened[path] = public_value

    visit(source_record, "", 0)
    return flattened


def _comparable_incremental_cursor(
    value: Any,
) -> tuple[Literal["datetime", "integer", "text"], int | str, str] | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if not -(2**63) <= value <= 2**63 - 1:
            return None
        return ("integer", value, str(value))
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 1024
        or any(ord(character) < 0x20 for character in normalized)
    ):
        return None
    if re.fullmatch(r"-?(?:0|[1-9][0-9]{0,18})", normalized):
        parsed_integer = int(normalized)
        if -(2**63) <= parsed_integer <= 2**63 - 1:
            return ("integer", parsed_integer, str(parsed_integer))
    try:
        parsed_datetime = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        parsed_datetime = None
    if parsed_datetime is not None and parsed_datetime.tzinfo is not None:
        canonical = parsed_datetime.astimezone(UTC).isoformat()
        return ("datetime", canonical, canonical)
    return ("text", normalized, normalized)


def _incremental_cursor_strictly_after(
    candidate: tuple[Literal["datetime", "integer", "text"], int | str, str],
    current: tuple[Literal["datetime", "integer", "text"], int | str, str],
) -> bool:
    if candidate[0] != current[0]:
        return False
    candidate_order = candidate[1]
    current_order = current[1]
    if isinstance(candidate_order, int) and isinstance(current_order, int):
        return candidate_order > current_order
    if isinstance(candidate_order, str) and isinstance(current_order, str):
        return candidate_order > current_order
    return False


def preview_mapping_status(
    connector: dict[str, Any],
    payload: dict[str, Any],
    *,
    limit: int,
) -> tuple[bool, list[str]]:
    mapping = connector["field_mapping"]
    platform_scope = connector.get("platform_scope")
    raw_configured_store_refs = (
        platform_scope.get("store_refs") if isinstance(platform_scope, dict) else []
    )
    configured_store_refs = (
        raw_configured_store_refs if isinstance(raw_configured_store_refs, list) else []
    )
    allowed_store_refs = {str(value) for value in configured_store_refs if isinstance(value, str)}
    errors: set[str] = set()
    source_records = _extract_records(payload)[:limit]
    cursor_policy = connector.get("cursor_policy")
    pagination = connector.get("pagination")
    cursor_field = cursor_policy.get("field") if isinstance(cursor_policy, dict) else None
    initial_cursor = (
        cursor_policy.get("cursor_value") or cursor_policy.get("initial_window_start")
        if isinstance(cursor_policy, dict)
        else None
    )
    previous_cursor = _comparable_incremental_cursor(initial_cursor)
    last_cursor: (
        tuple[
            Literal["datetime", "integer", "text"],
            int | str,
            str,
        ]
        | None
    ) = None
    for source_record in source_records:
        external_record_id = _field_value(
            source_record,
            mapping["external_record_id"],
        )
        audio_url = _field_value(source_record, mapping["audio_url"])
        started_at = _field_value(source_record, mapping["started_at"])
        if (
            isinstance(external_record_id, bool)
            or not isinstance(external_record_id, str | int)
            or not str(external_record_id).strip()
        ):
            errors.add("external_record_id")
        if not isinstance(audio_url, str) or not audio_url.strip():
            errors.add("audio_url")
        if not isinstance(started_at, str):
            errors.add("started_at")
        else:
            try:
                parsed_started_at = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            except ValueError:
                parsed_started_at = None
            if parsed_started_at is None or parsed_started_at.tzinfo is None:
                errors.add("started_at")
        store_ref = (
            _field_value(source_record, mapping["store_ref"]) if mapping.get("store_ref") else None
        )
        if allowed_store_refs and store_ref is None:
            errors.add("store_ref")
        elif allowed_store_refs and (
            not isinstance(store_ref, str) or store_ref not in allowed_store_refs
        ):
            raise ApiError(
                "CONNECTOR_RECORD_SCOPE_MISMATCH",
                "预览记录超出配置的平台门店范围",
                422,
            )
        if isinstance(cursor_field, str):
            candidate_cursor = _comparable_incremental_cursor(
                _field_value(source_record, cursor_field)
            )
            if (
                candidate_cursor is None
                or previous_cursor is None
                or not _incremental_cursor_strictly_after(
                    candidate_cursor,
                    previous_cursor,
                )
            ):
                errors.add("cursor_policy.field")
            if candidate_cursor is not None:
                previous_cursor = candidate_cursor
                last_cursor = candidate_cursor
    next_cursor_path = pagination.get("next_cursor_path") if isinstance(pagination, dict) else None
    if isinstance(next_cursor_path, str):
        next_cursor_raw = _field_value(payload, next_cursor_path)
        if next_cursor_raw is not None:
            next_cursor = _comparable_incremental_cursor(next_cursor_raw)
            if (
                next_cursor is None
                or last_cursor is None
                or next_cursor[0] != last_cursor[0]
                or next_cursor[2] != last_cursor[2]
            ):
                errors.add("pagination.next_cursor_path")
    return not errors, sorted(errors)


def preview_records(
    connector: dict[str, Any],
    payload: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    preview_mapping_status(connector, payload, limit=limit)
    return [
        _public_source_record(source_record) for source_record in _extract_records(payload)[:limit]
    ]
