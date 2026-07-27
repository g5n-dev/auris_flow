from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.client import HTTPException
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request as UrlRequest

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response
from redis import Redis
from sqlalchemy import text
from starlette.middleware.base import RequestResponseEndpoint

from app.api.routers import (
    audio_sessions,
    auth,
    calibrations,
    data_assets,
    evaluation,
    experiments,
    generic,
    hotwords,
    human_review,
    imports,
    insights,
    label_closed_loop,
    label_fact_sets,
    label_lifecycle,
    label_mappings,
    label_optimization_orchestrator,
    label_recomputations,
    labels,
    ops,
    prompt_candidate_reviews,
    prompt_release,
    quality_appeals,
    scene_profiles,
    task_runs,
    traces,
    workspace_context,
)
from app.core.auth import get_auth_provider
from app.core.config import _csv_items, get_settings, is_production_environment
from app.core.database import SessionLocal, engine
from app.core.errors import ApiError
from app.core.http_transport import open_url_no_redirect as urlopen
from app.core.logging import configure_logging, get_logger, log_event
from app.core.metrics import is_metrics_client_allowed, metrics
from app.core.observability import annotate_current_span, configure_observability
from app.core.oidc import OIDCError
from app.core.oidc_transaction import clear_authorization_transaction_cookie
from app.core.rate_limit import build_rate_limiter
from app.core.request_identifiers import (
    is_safe_request_identifier,
    safe_idempotency_key_for_response,
    sanitized_request_id,
    server_generated_public_id,
)
from app.schemas import ApiErrorEnvelope
from app.services.adapters import object_storage_client_for_provider

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger("app")

DAGSTER_READINESS_QUERY = """
query AurisReadinessWorkspace {
  instance {
    daemonHealth {
      allDaemonStatuses {
        daemonType
        required
        healthy
        lastHeartbeatTime
      }
    }
  }
  repositoriesOrError {
    __typename
    ... on RepositoryConnection {
      nodes {
        name
        location { name }
        pipelines { name }
      }
    }
  }
}
""".strip()
DAGSTER_READINESS_MAX_BYTES = 1_048_576
DAGSTER_HEARTBEAT_MAX_AGE_SECONDS = 90.0
DAGSTER_HEARTBEAT_FUTURE_SKEW_SECONDS = 5.0
OBJECT_STORAGE_READINESS_TIMEOUT_SECONDS = 0.25
READINESS_MARKER_MAX_BYTES = 2 * 1024 * 1024
QDRANT_READINESS_MAX_BYTES = 256 * 1024
OBSERVABILITY_READINESS_MAX_BYTES = 16 * 1024
QDRANT_COLLECTION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    yield
    application.state.observability.shutdown()


app = FastAPI(
    title="Auris Flow BFF",
    version="1.0.0",
    lifespan=lifespan,
    responses={
        413: {
            "model": ApiErrorEnvelope,
            "description": "请求体超过服务端允许的上限",
        },
        422: {
            "model": ApiErrorEnvelope,
            "description": "请求路径、查询、请求头或请求体校验失败",
        },
    },
)
app.state.rate_limiter = build_rate_limiter(settings)
trusted_hosts = list(_csv_items(settings.trusted_hosts))
if trusted_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_csv_items(settings.cors_allowed_origins)),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def apply_security_headers(response: Response) -> None:
    if not settings.security_headers_enabled:
        return
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; frame-ancestors 'none'; base-uri 'self'",
    )
    if is_production_environment(settings.app_env):
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )


def _request_body_too_large() -> ApiError:
    return ApiError(
        "REQUEST_BODY_TOO_LARGE",
        "请求体超过服务端允许的上限",
        413,
        details=[{"max_bytes": settings.max_request_body_bytes}],
    )


def _invalid_idempotency_key(*, duplicate: bool) -> ApiError:
    return ApiError(
        "VALIDATION_ERROR",
        "请求参数校验失败",
        422,
        details=[
            {
                "field": "header.Idempotency-Key",
                "message": (
                    "Idempotency-Key 不能重复"
                    if duplicate
                    else "Idempotency-Key 必须是 1–128 个受限 ASCII 字符"
                ),
                "code": "header_duplicate" if duplicate else "string_pattern_mismatch",
            }
        ],
    )


def _single_request_id_header(request: Request) -> str | None:
    values = request.headers.getlist("X-Request-Id")
    return values[0] if len(values) == 1 else None


async def _preload_bounded_request_body(request: Request) -> None:
    """Bound both declared and streamed request bodies before handlers parse them."""

    declared_values = request.headers.getlist("Content-Length")
    transfer_encoding_values = request.headers.getlist("Transfer-Encoding")
    if len(declared_values) > 1 or len(transfer_encoding_values) > 1:
        raise ApiError(
            "REQUEST_FRAMING_AMBIGUOUS",
            "请求包含重复的消息分帧头",
            400,
        )
    declared = declared_values[0] if declared_values else None
    transfer_encoding = transfer_encoding_values[0] if transfer_encoding_values else None
    if declared is not None and transfer_encoding is not None:
        raise ApiError(
            "REQUEST_FRAMING_AMBIGUOUS",
            "请求不能同时包含 Content-Length 和 Transfer-Encoding",
            400,
        )
    if declared is not None:
        if not declared.isascii() or not declared.isdecimal():
            raise ApiError("CONTENT_LENGTH_INVALID", "Content-Length 格式无效", 400)
        declared_length = int(declared)
        if declared_length > settings.max_request_body_bytes:
            raise _request_body_too_large()
        if declared_length == 0:
            return
    elif transfer_encoding is None and request.method in {
        "GET",
        "HEAD",
        "OPTIONS",
    }:
        return

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > settings.max_request_body_bytes:
            raise _request_body_too_large()
        chunks.append(chunk)
    # Starlette reuses this cached body when call_next constructs the downstream
    # request, so chunked requests cannot bypass the streaming byte budget.
    request._body = b"".join(chunks)


@app.middleware("http")
async def request_logging_middleware(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    request.state.trace_id = server_generated_public_id("trace", suffix_length=32)
    request.state.server_trace_initialized = True
    request.state.request_id = sanitized_request_id(_single_request_id_header(request))
    annotate_current_span(
        business_trace_id=request.state.trace_id,
        request_id=request.state.request_id,
    )
    started = time.perf_counter()
    log_health_probe = request.url.path != "/healthz"
    if log_health_probe:
        log_event(
            logger,
            "http.request.start",
            method=request.method,
            path=request.url.path,
            request_id=request.state.request_id,
            trace_id=request.state.trace_id,
        )
    response: Response | None = None
    rate_limit_decision = None
    idempotency_values = request.headers.getlist("Idempotency-Key")
    if idempotency_values and (
        len(idempotency_values) != 1 or not is_safe_request_identifier(idempotency_values[0])
    ):
        duplicate = len(idempotency_values) != 1
        api_error = _invalid_idempotency_key(duplicate=duplicate)
        log_event(
            logger,
            "http.request.rejected",
            level=logging.WARNING,
            method=request.method,
            path=request.url.path,
            status_code=api_error.status_code,
            error_code=api_error.code,
            request_id=request.state.request_id,
            trace_id=request.state.trace_id,
        )
        response = JSONResponse(
            status_code=api_error.status_code,
            content=error_payload(request, api_error),
        )
    if (
        response is None
        and settings.rate_limit_per_minute > 0
        and request.url.path
        not in {
            "/healthz",
            "/readyz",
            "/metrics",
            "/docs",
            "/openapi.json",
        }
    ):
        # Authentication runs downstream. Using an unverified bearer token here would let
        # callers rotate invalid tokens to obtain a fresh rate-limit bucket every request.
        actor_source = (request.client.host if request.client else None) or "unknown"
        actor = hashlib.sha256(actor_source.encode("utf-8")).hexdigest()[:24]
        rate_limit_decision = app.state.rate_limiter.allow(
            f"{settings.app_env}:{actor}:{request.method}:{request.url.path}",
            limit=settings.rate_limit_per_minute,
            window_seconds=60,
        )
        metrics.record_rate_limit_decision(
            allowed=rate_limit_decision.allowed,
            backend=rate_limit_decision.backend,
        )
        if not rate_limit_decision.allowed:
            backend_unavailable = rate_limit_decision.backend == "redis-unavailable"
            if backend_unavailable:
                log_event(
                    logger,
                    "rate_limit.backend_unavailable",
                    level=logging.ERROR,
                    method=request.method,
                    path=request.url.path,
                    request_id=request.state.request_id,
                    trace_id=request.state.trace_id,
                )
            api_error = ApiError(
                "RATE_LIMIT_BACKEND_UNAVAILABLE" if backend_unavailable else "RATE_LIMIT_EXCEEDED",
                "限流服务暂不可用，请稍后重试"
                if backend_unavailable
                else "请求过于频繁，请稍后重试",
                503 if backend_unavailable else 429,
                retryable=True,
                details=[
                    {
                        "limit": rate_limit_decision.limit,
                        "backend": rate_limit_decision.backend,
                        "reset_after_seconds": rate_limit_decision.reset_after_seconds,
                    }
                ],
            )
            response = JSONResponse(
                status_code=api_error.status_code,
                content=error_payload(request, api_error),
                headers={"Retry-After": str(rate_limit_decision.reset_after_seconds)},
            )
    if response is None:
        try:
            await _preload_bounded_request_body(request)
        except ApiError as exc:
            log_event(
                logger,
                "http.request.rejected",
                level=logging.WARNING,
                method=request.method,
                path=request.url.path,
                status_code=exc.status_code,
                error_code=exc.code,
                request_id=request.state.request_id,
                trace_id=request.state.trace_id,
            )
            response = JSONResponse(
                status_code=exc.status_code,
                content=error_payload(request, exc),
            )
    if response is None:
        try:
            response = await call_next(request)
        except Exception as exc:
            duration_seconds = max(0.0, time.perf_counter() - started)
            route = request.scope.get("route")
            metrics.observe_http(
                method=request.method,
                route=getattr(route, "path", None),
                status_code=500,
                duration_seconds=duration_seconds,
            )
            log_event(
                logger,
                "http.request.error",
                level=logging.ERROR,
                method=request.method,
                path=request.url.path,
                request_id=request.state.request_id,
                trace_id=request.state.trace_id,
                error_type=exc.__class__.__name__,
            )
            raise
    oidc_callback_path = f"{settings.api_prefix}/auth/oidc/callback"
    oidc_backchannel_logout_path = f"{settings.api_prefix}/auth/oidc/back-channel-logout"
    if request.url.path == oidc_callback_path:
        clear_authorization_transaction_cookie(response, app_env=settings.app_env)
    if request.url.path in {oidc_callback_path, oidc_backchannel_logout_path}:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    route = request.scope.get("route")
    metrics.observe_http(
        method=request.method,
        route=getattr(route, "path", None),
        status_code=response.status_code,
        duration_seconds=duration_ms / 1000,
    )
    response.headers["X-Trace-Id"] = request.state.trace_id
    response.headers["X-Request-Id"] = request.state.request_id
    apply_security_headers(response)
    if rate_limit_decision:
        response.headers["X-RateLimit-Limit"] = str(rate_limit_decision.limit)
        response.headers["X-RateLimit-Remaining"] = str(rate_limit_decision.remaining)
        response.headers["X-RateLimit-Reset"] = str(rate_limit_decision.reset_after_seconds)
    if log_health_probe:
        log_event(
            logger,
            "http.request.end",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            request_id=request.state.request_id,
            trace_id=request.state.trace_id,
        )
    return response


def error_payload(request: Request, exc: ApiError) -> dict:
    trace_id = getattr(request.state, "trace_id", None)
    if not trace_id:
        trace_id = server_generated_public_id("trace", suffix_length=32)
        request.state.trace_id = trace_id
    request_id = getattr(request.state, "request_id", None)
    if not request_id:
        request_id = sanitized_request_id(_single_request_id_header(request))
        request.state.request_id = request_id
    idempotency_values = request.headers.getlist("Idempotency-Key")
    idempotency_key = (
        safe_idempotency_key_for_response(idempotency_values[0])
        if len(idempotency_values) == 1
        else None
    )
    return {
        "error": {
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
            "status": exc.status_code,
            "retryable": exc.retryable,
            "trace_id": trace_id,
            "request_id": request_id,
            "idempotency_key": idempotency_key,
        }
    }


@app.exception_handler(ApiError)
async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    log_event(
        logger,
        "http.api_error",
        level=logging.WARNING if exc.status_code < 500 else logging.ERROR,
        method=request.method,
        path=request.url.path,
        status_code=exc.status_code,
        error_code=exc.code,
        request_id=getattr(request.state, "request_id", None),
        trace_id=getattr(request.state, "trace_id", None),
    )
    return JSONResponse(status_code=exc.status_code, content=error_payload(request, exc))


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Keep framework-level body/path validation inside the public error contract."""

    return await handle_api_error(
        request,
        ApiError(
            "VALIDATION_ERROR",
            "请求参数校验失败",
            422,
            details=[
                {
                    "field": ".".join(str(part) for part in error["loc"]),
                    "message": str(error["msg"]),
                    "code": str(error["type"]),
                }
                for error in exc.errors()
            ],
        ),
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    api_error = ApiError("INTERNAL_ERROR", "服务内部错误", 500, retryable=True)
    log_event(
        logger,
        "http.unexpected_error",
        level=logging.ERROR,
        method=request.method,
        path=request.url.path,
        error_type=exc.__class__.__name__,
        request_id=getattr(request.state, "request_id", None),
        trace_id=getattr(request.state, "trace_id", None),
    )
    response = JSONResponse(status_code=500, content=error_payload(request, api_error))
    if request.url.path == f"{settings.api_prefix}/auth/oidc/callback":
        clear_authorization_transaction_cookie(response, app_env=settings.app_env)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "data": {"status": "success", "service": settings.app_name}}


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics(request: Request) -> Response:
    if not settings.metrics_enabled:
        return Response(status_code=404)
    peer_host = request.client.host if request.client else None
    if not is_metrics_client_allowed(peer_host, settings.metrics_trusted_cidrs):
        return Response(status_code=403)
    metrics.refresh_operational_metrics(session_factory=SessionLocal, engine=engine)
    return Response(
        content=metrics.render(),
        headers={
            "Cache-Control": "no-store",
            "Content-Type": metrics.content_type,
        },
    )


def probe_dagster_workspace(url: str | None) -> str:
    """Require the exact production code location, repository and generic job."""

    if not url:
        return "not_configured"
    body = json.dumps(
        {"query": DAGSTER_READINESS_QUERY, "variables": {}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    bearer_token = os.environ.get("DAGSTER_GRAPHQL_BEARER_TOKEN", "").strip()
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = UrlRequest(url, data=body, method="POST", headers=headers)
    try:
        with urlopen(request, timeout=1.0) as response:
            if response.status != 200:
                return "not_ready"
            raw = response.read(DAGSTER_READINESS_MAX_BYTES + 1)
        if len(raw) > DAGSTER_READINESS_MAX_BYTES:
            return "not_ready"
        payload = json.loads(raw.decode("utf-8"))
    except (
        AttributeError,
        HTTPException,
        OSError,
        URLError,
        UnicodeDecodeError,
        ValueError,
    ):
        return "not_ready"
    if not isinstance(payload, dict) or payload.get("errors"):
        return "not_ready"
    data = payload.get("data")
    instance = data.get("instance") if isinstance(data, dict) else None
    daemon_health = instance.get("daemonHealth") if isinstance(instance, dict) else None
    daemon_statuses = (
        daemon_health.get("allDaemonStatuses") if isinstance(daemon_health, dict) else None
    )
    if not isinstance(daemon_statuses, list) or not daemon_statuses:
        return "not_ready"
    daemon_types: set[str] = set()
    required_daemons = 0
    heartbeat_now = time.time()
    for daemon_status in daemon_statuses:
        if not isinstance(daemon_status, dict):
            return "not_ready"
        daemon_type = daemon_status.get("daemonType")
        required = daemon_status.get("required")
        healthy = daemon_status.get("healthy")
        if (
            not isinstance(daemon_type, str)
            or not daemon_type
            or daemon_type in daemon_types
            or not isinstance(required, bool)
            or (healthy is not None and not isinstance(healthy, bool))
        ):
            return "not_ready"
        daemon_types.add(daemon_type)
        if required:
            required_daemons += 1
            if healthy is not True:
                return "not_ready"
            last_heartbeat = daemon_status.get("lastHeartbeatTime")
            if (
                isinstance(last_heartbeat, bool)
                or not isinstance(last_heartbeat, (int, float))
                or not math.isfinite(float(last_heartbeat))
                or float(last_heartbeat) < heartbeat_now - DAGSTER_HEARTBEAT_MAX_AGE_SECONDS
                or float(last_heartbeat) > heartbeat_now + DAGSTER_HEARTBEAT_FUTURE_SKEW_SECONDS
            ):
                return "not_ready"
    if required_daemons == 0:
        return "not_ready"

    repositories = data.get("repositoriesOrError") if isinstance(data, dict) else None
    if (
        not isinstance(repositories, dict)
        or repositories.get("__typename") != "RepositoryConnection"
    ):
        return "not_ready"
    nodes = repositories.get("nodes")
    if not isinstance(nodes, list):
        return "not_ready"
    expected_location = os.environ.get(
        "DAGSTER_REPOSITORY_LOCATION_NAME", "auris_flow_defs"
    ).strip()
    expected_repository = os.environ.get("DAGSTER_REPOSITORY_NAME", "__repository__").strip()
    expected_job = os.environ.get("DAGSTER_DEFAULT_JOB_NAME", "auris_flow_generic_job").strip()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        location = node.get("location")
        pipelines = node.get("pipelines")
        pipeline_items = pipelines if isinstance(pipelines, list) else []
        pipeline_names = {
            str(item.get("name"))
            for item in pipeline_items
            if isinstance(item, dict) and item.get("name")
        }
        if (
            node.get("name") == expected_repository
            and isinstance(location, dict)
            and location.get("name") == expected_location
            and expected_job in pipeline_names
        ):
            return "ok"
    return "not_ready"


def probe_qdrant_collections(url: str | None, api_key: str | None = None) -> str:
    """Require a bounded, authenticated Qdrant collections response."""

    if not url:
        return "not_configured"
    headers = {"api-key": api_key} if api_key else {}
    request = UrlRequest(f"{url.rstrip('/')}/collections", method="GET", headers=headers)
    try:
        with urlopen(request, timeout=0.25) as response:
            if response.status != 200:
                return "not_ready"
            raw = response.read(QDRANT_READINESS_MAX_BYTES + 1)
        if len(raw) > QDRANT_READINESS_MAX_BYTES:
            return "not_ready"
        payload = json.loads(raw.decode("utf-8"))
    except (HTTPException, OSError, URLError, UnicodeDecodeError, ValueError):
        return "not_ready"
    if not isinstance(payload, dict):
        return "not_ready"
    result = payload.get("result")
    collections = result.get("collections") if isinstance(result, dict) else None
    if payload.get("status") != "ok" or not isinstance(collections, list):
        return "not_ready"
    collection_names: list[str] = []
    for collection in collections:
        name = collection.get("name") if isinstance(collection, dict) else None
        if not isinstance(name, str) or not QDRANT_COLLECTION_NAME_PATTERN.fullmatch(name):
            return "not_ready"
        collection_names.append(name)
    if len(collection_names) != len(set(collection_names)):
        return "not_ready"
    return "ok"


def probe_observability_status(
    url: str | None,
    *,
    expected_trace_id: str | None = None,
    timeout_seconds: float = 0.25,
) -> str:
    """Require the observability sidecar's bounded, exact JSON acknowledgement."""

    if not url:
        return "not_configured"
    try:
        request = UrlRequest(url, method="GET", headers={"Accept": "application/json"})
        with urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                return "not_ready"
            raw = response.read(OBSERVABILITY_READINESS_MAX_BYTES + 1)
        if len(raw) > OBSERVABILITY_READINESS_MAX_BYTES:
            return "not_ready"
        payload = json.loads(raw.decode("utf-8"))
    except (HTTPException, OSError, URLError, UnicodeDecodeError, ValueError):
        return "not_ready"
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return "not_ready"
    if expected_trace_id is not None and payload.get("trace_id") != expected_trace_id:
        return "not_ready"
    return "ok"


@app.get("/readyz")
def readyz() -> JSONResponse:
    def required_checks() -> set[str]:
        configured = settings.required_dependency_checks.strip().lower()
        strict_mode = (
            settings.dependency_check_mode == "strict"
            or settings.app_env.strip().lower() == "ci"
            or is_production_environment(settings.app_env)
        )
        if strict_mode:
            checks = {"auth"}
            if is_production_environment(settings.app_env):
                checks.add("dagster")
            if configured and configured != "auto":
                checks.update(item.strip() for item in configured.split(",") if item.strip())
                return checks
            automatic = {"auth", "database", "redis", "object_storage", "qdrant"}
            if is_production_environment(settings.app_env):
                automatic.add("dagster")
            return automatic
        if configured and configured != "auto":
            return {item.strip() for item in configured.split(",") if item.strip()}
        return {"database"}

    def probe_auth() -> str:
        try:
            get_auth_provider()
        except ApiError:
            return "not_configured"
        if settings.auth_provider.strip().lower() != "oidc":
            return "ok"
        try:
            auth.get_oidc_authorization_flow().discover(
                force_refresh=is_production_environment(settings.app_env)
            )
            return "ok"
        except OIDCError:
            return "not_ready"

    def probe_redis(url: str | None) -> str:
        if not url:
            return "not_configured"
        try:
            Redis.from_url(url, socket_connect_timeout=0.2, socket_timeout=0.2).ping()
            return "ok"
        except Exception:
            return "not_ready"

    def probe_http(
        url: str | None,
        path: str = "",
        headers: dict[str, str] | None = None,
        *,
        required_token: bytes | None = None,
        timeout_seconds: float = 0.25,
    ) -> str:
        if not url:
            return "not_configured"
        target = f"{url.rstrip('/')}{path}"
        try:
            request = UrlRequest(target, method="GET", headers=headers or {})
            with urlopen(request, timeout=timeout_seconds) as response:
                if not 200 <= response.status < 300:
                    return "not_ready"
                if required_token is not None:
                    body = response.read(READINESS_MARKER_MAX_BYTES + 1)
                    if len(body) > READINESS_MARKER_MAX_BYTES or required_token not in body:
                        return "not_ready"
                return "ok"
        except (HTTPException, OSError, URLError, ValueError):
            return "not_ready"

    def probe_observability() -> str:
        runtime = getattr(app.state, "observability", None)
        if (
            not bool(getattr(runtime, "enabled", False))
            or getattr(runtime, "error_code", None) is not None
        ):
            return "not_ready"
        if probe_observability_status(settings.observability_health_url) != "ok":
            return "not_ready"

        timeout_millis = min(
            max(int(float(settings.otel_export_timeout_seconds) * 1000), 100),
            1000,
        )
        pipeline_probe = getattr(runtime, "readiness_pipeline_is_live", None)
        if not callable(pipeline_probe):
            return "not_ready"

        def marker_is_visible(trace_id: str) -> bool:
            if not re.fullmatch(r"[0-9a-f]{32}", trace_id):
                return False
            parts = urlsplit(settings.observability_health_url)
            parent_path = parts.path.rsplit("/", 1)[0].rstrip("/")
            marker_url = urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    f"{parent_path}/traces/{trace_id}",
                    "",
                    "",
                )
            )
            return (
                probe_observability_status(
                    marker_url,
                    expected_trace_id=trace_id,
                    timeout_seconds=0.75,
                )
                == "ok"
            )

        return (
            "ok"
            if pipeline_probe(
                timeout_millis=timeout_millis,
                trace_visible=marker_is_visible,
            )
            else "not_ready"
        )

    def probe_object_storage() -> str:
        try:
            client = object_storage_client_for_provider(settings.object_storage_provider)
            result = client.head_bucket(
                client.bucket,
                timeout_seconds=OBJECT_STORAGE_READINESS_TIMEOUT_SECONDS,
            )
            status = result.get("status") if isinstance(result, dict) else None
            return "ok" if isinstance(status, int) and 200 <= status < 300 else "not_ready"
        except (HTTPException, OSError, URLError, TimeoutError, ValueError):
            return "not_ready"

    checks = {
        "auth": probe_auth(),
        "database": "unknown",
        "observability": probe_observability(),
        "redis": probe_redis(settings.redis_url),
        "object_storage": probe_object_storage(),
        "qdrant": probe_qdrant_collections(settings.qdrant_url, settings.qdrant_api_key),
        "dagster": probe_dagster_workspace(settings.dagster_graphql_url),
    }
    try:
        with SessionLocal() as session:
            session.execute(text("select 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "failed"
    metrics.set_dependency_readiness(checks)
    required = required_checks()
    missing_required = {
        key: checks.get(key, "not_configured")
        for key in sorted(required)
        if checks.get(key) != "ok"
    }
    status = "ok" if not missing_required else "degraded"
    payload = {
        "status": status,
        "data": {
            "status": "success" if status == "ok" else "failed",
            "checks": checks,
            "required_checks": sorted(required),
            "missing_required": missing_required,
        },
    }
    return JSONResponse(status_code=503 if missing_required else 200, content=payload)


for router in [
    auth.router,
    ops.router,
    data_assets.router,
    scene_profiles.router,
    task_runs.router,
    audio_sessions.router,
    hotwords.router,
    calibrations.router,
    human_review.router,
    quality_appeals.router,
    label_closed_loop.router,
    label_fact_sets.router,
    label_lifecycle.router,
    label_mappings.router,
    label_optimization_orchestrator.router,
    label_recomputations.router,
    labels.router,
    insights.router,
    evaluation.router,
    experiments.router,
    prompt_candidate_reviews.router,
    prompt_release.router,
    traces.router,
    workspace_context.router,
    imports.router,
    generic.router,
]:
    app.include_router(router, prefix=settings.api_prefix)


_default_openapi = app.openapi


def _openapi_with_completion_hmac_security() -> dict[str, Any]:
    document = _default_openapi()
    security_schemes = document.setdefault("components", {}).setdefault(
        "securitySchemes",
        {},
    )
    security_schemes.update(
        {
            "aurisCompletionSignature": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Auris-Signature",
                "description": "规范请求消息的 HMAC-SHA256 签名。",
            },
            "aurisCompletionKeyId": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Auris-Key-Id",
                "description": "完成回执签名密钥标识。",
            },
            "aurisCompletionLegacyKeyId": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Auris-Signature-Id",
                "description": "兼容旧客户端的完成回执签名密钥标识。",
                "x-deprecated": True,
            },
        }
    )
    for receipt_path in (
        "external-completion-receipts",
        "external-progress-receipts",
    ):
        operation = document["paths"][f"{settings.api_prefix}/runs/{{id}}/{receipt_path}"]["post"]
        operation["security"] = [
            {
                "aurisCompletionSignature": [],
                "aurisCompletionKeyId": [],
            },
            {
                "aurisCompletionSignature": [],
                "aurisCompletionLegacyKeyId": [],
            },
        ]
    return document


app.openapi = _openapi_with_completion_hmac_security  # type: ignore[method-assign]


app.state.observability = configure_observability(app, settings, engine=engine)
