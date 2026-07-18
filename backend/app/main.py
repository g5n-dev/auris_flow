from __future__ import annotations

import hashlib
import logging
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import text

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
    insights,
    label_closed_loop,
    label_fact_sets,
    label_lifecycle,
    label_mappings,
    label_optimization_orchestrator,
    labels,
    ops,
    prompt_candidate_reviews,
    prompt_release,
    quality_appeals,
    scene_profiles,
    task_runs,
    traces,
)
from app.core.auth import get_auth_provider
from app.core.config import _csv_items, get_settings, is_production_environment
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.core.logging import configure_logging, get_logger, log_event
from app.core.rate_limit import build_rate_limiter
from app.services.adapters import object_storage_client_for_provider

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger("app")

app = FastAPI(title="Auris Flow BFF", version="0.1.0")
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


def apply_security_headers(response) -> None:
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


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request.state.trace_id = f"trace_{uuid.uuid4().hex}"
    request.state.server_trace_initialized = True
    request.state.request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
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
    response = None
    rate_limit_decision = None
    if settings.rate_limit_per_minute > 0 and request.url.path not in {
        "/healthz",
        "/readyz",
        "/docs",
        "/openapi.json",
    }:
        # Authentication runs downstream. Using an unverified bearer token here would let
        # callers rotate invalid tokens to obtain a fresh rate-limit bucket every request.
        actor_source = (request.client.host if request.client else None) or "unknown"
        actor = hashlib.sha256(actor_source.encode("utf-8")).hexdigest()[:24]
        rate_limit_decision = app.state.rate_limiter.allow(
            f"{settings.app_env}:{actor}:{request.method}:{request.url.path}",
            limit=settings.rate_limit_per_minute,
            window_seconds=60,
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
            response = await call_next(request)
        except Exception as exc:
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
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
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
        trace_id = f"trace_{uuid.uuid4().hex}"
        request.state.trace_id = trace_id
    return {
        "error": {
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
            "status": exc.status_code,
            "retryable": exc.retryable,
            "trace_id": trace_id,
            "idempotency_key": request.headers.get("Idempotency-Key"),
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
    return JSONResponse(status_code=500, content=error_payload(request, api_error))


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "data": {"status": "success", "service": settings.app_name}}


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
            if configured and configured != "auto":
                checks.update(item.strip() for item in configured.split(",") if item.strip())
                return checks
            return {"auth", "database", "redis", "object_storage", "qdrant"}
        if configured and configured != "auto":
            return {item.strip() for item in configured.split(",") if item.strip()}
        return {"database"}

    def probe_auth() -> str:
        try:
            get_auth_provider()
            return "ok"
        except ApiError:
            return "not_configured"

    def probe_redis(url: str | None) -> str:
        if not url:
            return "not_configured"
        try:
            Redis.from_url(url, socket_connect_timeout=0.2, socket_timeout=0.2).ping()
            return "ok"
        except Exception:
            return "not_ready"

    def probe_http(url: str | None, path: str = "", headers: dict[str, str] | None = None) -> str:
        if not url:
            return "not_configured"
        target = f"{url.rstrip('/')}{path}"
        try:
            request = UrlRequest(target, method="GET", headers=headers or {})
            with urlopen(request, timeout=0.25) as response:
                return "ok" if 200 <= response.status < 500 else "not_ready"
        except (OSError, URLError, ValueError):
            return "not_ready"

    def probe_object_storage() -> str:
        if settings.object_storage_provider == "minio":
            return probe_http(settings.object_storage_endpoint, "/minio/health/ready")
        try:
            client = object_storage_client_for_provider(settings.object_storage_provider)
            client.head_object(client.bucket, "")
            return "ok"
        except (HTTPError, OSError, URLError, TimeoutError, ValueError):
            return "not_ready"

    checks = {
        "auth": probe_auth(),
        "database": "unknown",
        "redis": probe_redis(settings.redis_url),
        "object_storage": probe_object_storage(),
        "qdrant": probe_http(
            settings.qdrant_url,
            "/collections",
            {"api-key": settings.qdrant_api_key} if settings.qdrant_api_key else None,
        ),
        "dagster": probe_http(settings.dagster_graphql_url.removesuffix("/graphql")),
    }
    try:
        with SessionLocal() as session:
            session.execute(text("select 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "failed"
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
    labels.router,
    insights.router,
    evaluation.router,
    experiments.router,
    prompt_candidate_reviews.router,
    prompt_release.router,
    traces.router,
    generic.router,
]:
    app.include_router(router, prefix=settings.api_prefix)
