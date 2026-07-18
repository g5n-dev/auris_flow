from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.models import AuthSession


@dataclass(frozen=True)
class AuthActor:
    user_id: str
    roles: tuple[str, ...]
    provider: str
    tenant_ids: tuple[str, ...] = ()
    project_ids: tuple[str, ...] = ()
    actor_kind: str = "human"
    session_id: str | None = None
    issued_at: int | None = None
    expires_at: int | None = None


@dataclass(frozen=True)
class DevAuthProfile:
    email: str
    user_id: str
    name: str
    role_label: str
    initials: str
    roles: tuple[str, ...]
    actor_kind: str = "human"
    tenant_id: str = "aurora_auto"
    tenant_name: str = "极光汽车"
    project_id: str = "sales_qa"
    project_name: str = "销售话术质检"


DEV_AUTH_PROFILES: dict[str, DevAuthProfile] = {
    "demo.operator@auris.local": DevAuthProfile(
        email="demo.operator@auris.local",
        user_id="u_admin_001",
        name="Demo Operator",
        role_label="平台管理员",
        initials="D",
        roles=("project_admin", "asset_manager"),
    ),
    "admin@auris.local": DevAuthProfile(
        email="admin@auris.local",
        user_id="u_admin_001",
        name="项目管理员",
        role_label="平台管理员",
        initials="管",
        roles=("project_admin", "asset_manager"),
    ),
    "release.approver@auris.local": DevAuthProfile(
        email="release.approver@auris.local",
        user_id="u_release_admin_001",
        name="发布复核管理员",
        role_label="项目管理员",
        initials="审",
        roles=("project_admin",),
    ),
    "annotator@auris.local": DevAuthProfile(
        email="annotator@auris.local",
        user_id="u_annotator_001",
        name="质检运营 A",
        role_label="质检运营",
        initials="A",
        roles=("annotator", "review_arbitrator"),
    ),
    "annotator.b@auris.local": DevAuthProfile(
        email="annotator.b@auris.local",
        user_id="u_annotator_002",
        name="质检运营 B",
        role_label="质检运营",
        initials="B",
        roles=("annotator",),
    ),
    "model@auris.local": DevAuthProfile(
        email="model@auris.local",
        user_id="u_model_001",
        name="模型工程师",
        role_label="模型工程师",
        initials="M",
        roles=("model_engineer",),
        actor_kind="model",
    ),
}


class AuthProvider(Protocol):
    def authenticate(self, token: str, *, now: float | None = None) -> AuthActor:
        """Resolve a bearer token into a platform actor."""


class StaticDevAuthProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._tokens: dict[str, AuthActor] = {
            settings.dev_auth_token: AuthActor(
                "u_admin_001", ("project_admin", "asset_manager"), "dev", actor_kind="human"
            ),
            "system-token": AuthActor("system", ("system",), "dev", actor_kind="system"),
            "model-token": AuthActor("u_model_001", ("model_engineer",), "dev", actor_kind="model"),
            "annotator-token": AuthActor(
                "u_annotator_001", ("annotator", "review_arbitrator"), "dev"
            ),
            "annotator-b-token": AuthActor("u_annotator_002", ("annotator",), "dev"),
        }

    def authenticate(self, token: str, *, now: float | None = None) -> AuthActor:
        if self.settings.app_env not in {"local", "test", "ci"} or not self.settings.allow_dev_auth:
            raise ApiError("DEV_TOKEN_DISABLED", "生产环境不能使用本地演示 token", 401)
        actor = self._tokens.get(token)
        if actor:
            return actor
        if token.startswith("auris.v1."):
            signed_actor = SignedTokenAuthProvider(
                self.settings,
                secret=_dev_session_secret(self.settings),
                provider_name="dev_session",
            ).authenticate(token, now=now)
            return signed_actor
        raise ApiError("UNAUTHORIZED", "无效或过期 token", 401)


class SignedTokenAuthProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        secret: str | None = None,
        provider_name: str = "signed",
    ) -> None:
        self.settings = settings
        self.secret = secret if secret is not None else settings.auth_token_secret
        self.provider_name = provider_name
        if len(self.secret) < 32:
            raise ApiError("AUTH_PROVIDER_NOT_CONFIGURED", "生产认证签名密钥未配置或长度不足", 500)

    def authenticate(self, token: str, *, now: float | None = None) -> AuthActor:
        try:
            prefix, version, payload_part, signature_part = token.split(".", 3)
        except ValueError as exc:
            raise ApiError("UNAUTHORIZED", "无效或过期 token", 401) from exc
        if (prefix, version) != ("auris", "v1"):
            raise ApiError("UNAUTHORIZED", "无效或过期 token", 401)
        signing_input = f"{prefix}.{version}.{payload_part}".encode()
        expected_signature = _base64url_encode(
            hmac.new(
                self.secret.encode("utf-8"),
                signing_input,
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(signature_part, expected_signature):
            raise ApiError("UNAUTHORIZED", "无效或过期 token", 401)
        payload = _decode_payload(payload_part)
        issuer = payload.get("iss")
        audience = payload.get("aud")
        issued_at = _integer_claim(payload.get("iat"))
        expires_at = _integer_claim(payload.get("exp"))
        session_id = payload.get("jti")
        current_time = time.time() if now is None else now
        clock_skew = self.settings.auth_token_clock_skew_seconds
        if issuer != self.settings.auth_token_issuer:
            raise ApiError("UNAUTHORIZED", "无效或过期 token", 401)
        if not _audience_contains(audience, self.settings.auth_token_audience):
            raise ApiError("UNAUTHORIZED", "无效或过期 token", 401)
        if (
            issued_at is None
            or expires_at is None
            or expires_at <= issued_at
            or issued_at > current_time + clock_skew
            or expires_at <= current_time - clock_skew
            or not isinstance(session_id, str)
            or not session_id
            or len(session_id) > 128
        ):
            raise ApiError("UNAUTHORIZED", "无效或过期 token", 401)
        user_id = payload.get("sub")
        roles = _string_tuple(payload.get("roles"))
        tenant_ids = _string_tuple(payload.get("tenant_ids"))
        project_ids = _string_tuple(payload.get("project_ids"))
        actor_kind = payload.get("actor_kind", "human")
        if (
            not isinstance(user_id, str)
            or not user_id
            or not roles
            or not tenant_ids
            or not project_ids
            or actor_kind not in {"human", "model", "service", "system"}
        ):
            raise ApiError("UNAUTHORIZED", "无效或过期 token", 401)
        return AuthActor(
            user_id,
            roles,
            self.provider_name,
            tenant_ids=tenant_ids,
            project_ids=project_ids,
            actor_kind=actor_kind,
            session_id=session_id,
            issued_at=issued_at,
            expires_at=expires_at,
        )


def dev_auth_enabled(settings: Settings) -> bool:
    return settings.app_env.strip().lower() in {"local", "test", "ci"} and settings.allow_dev_auth


def get_dev_auth_profile(email: str) -> DevAuthProfile | None:
    return DEV_AUTH_PROFILES.get(email.strip().lower())


def _dev_session_secret(settings: Settings) -> str:
    # Dev sessions are intentionally process-independent so local multi-worker setups work.
    # They are accepted only while dev auth is enabled and never by the production provider.
    seed = "|".join(
        (
            settings.dev_auth_token,
            settings.dev_auth_password,
            settings.auth_token_issuer,
            "auris-flow-local-dev-session-v1",
        )
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def issue_dev_auth_token(
    profile: DevAuthProfile,
    settings: Settings,
    *,
    now: int | None = None,
    session: Session | None = None,
) -> tuple[str, int]:
    if not dev_auth_enabled(settings):
        raise ApiError("DEV_LOGIN_DISABLED", "当前环境未启用本地开发登录", 404)
    issued_at = int(time.time()) if now is None else now
    expires_at = issued_at + max(300, settings.dev_auth_session_ttl_seconds)
    project_scope = ("*",) if "project_admin" in profile.roles else (profile.project_id,)
    session_id = f"dev_{uuid.uuid4().hex}"
    token = sign_auth_token(
        secret=_dev_session_secret(settings),
        user_id=profile.user_id,
        roles=profile.roles,
        tenant_ids=(profile.tenant_id,),
        project_ids=project_scope,
        issued_at=issued_at,
        expires_at=expires_at,
        issuer=settings.auth_token_issuer,
        audience=settings.auth_token_audience,
        token_id=session_id,
        actor_kind=profile.actor_kind,
    )
    _persist_dev_auth_session(
        session=session,
        session_id=session_id,
        profile=profile,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    return token, expires_at


def _persist_dev_auth_session(
    *,
    session: Session | None,
    session_id: str,
    profile: DevAuthProfile,
    issued_at: int,
    expires_at: int,
) -> None:
    issued = datetime.fromtimestamp(issued_at, tz=UTC)
    expires = datetime.fromtimestamp(expires_at, tz=UTC)
    record = AuthSession(
        session_id=session_id,
        user_id=profile.user_id,
        tenant_id=profile.tenant_id,
        provider="dev_session",
        issued_at=issued,
        expires_at=expires,
        last_seen_at=issued,
    )
    if session is not None:
        session.add(record)
        session.commit()
        return

    # Some backend contract helpers issue scoped dev sessions directly. Persisting here keeps
    # those callers on the same revocable-session path as /auth/dev-login.
    from app.core.database import SessionLocal

    with SessionLocal.begin() as owned_session:
        owned_session.add(record)


def _base64url_encode(value: bytes) -> str:
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return urlsafe_b64decode(f"{value}{padding}".encode("ascii"))


def _decode_payload(payload_part: str) -> dict[str, Any]:
    try:
        payload = json.loads(_base64url_decode(payload_part).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError("UNAUTHORIZED", "无效或过期 token", 401) from exc
    if not isinstance(payload, dict):
        raise ApiError("UNAUTHORIZED", "无效或过期 token", 401)
    return payload


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.replace(",", "|").split("|") if item.strip())
    if isinstance(value, list):
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return ()


def _integer_claim(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _audience_contains(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return hmac.compare_digest(value, expected)
    if isinstance(value, list):
        return any(isinstance(item, str) and hmac.compare_digest(item, expected) for item in value)
    return False


def sign_auth_token(
    *,
    secret: str,
    user_id: str,
    roles: tuple[str, ...],
    tenant_ids: tuple[str, ...],
    project_ids: tuple[str, ...],
    expires_at: int,
    issuer: str = "auris-flow",
    audience: str = "auris-flow-api",
    issued_at: int | None = None,
    token_id: str | None = None,
    actor_kind: str = "human",
) -> str:
    if actor_kind not in {"human", "model", "service", "system"}:
        raise ValueError("actor_kind must be human, model, service, or system")
    resolved_issued_at = int(time.time()) if issued_at is None else issued_at
    resolved_token_id = token_id or f"session_{uuid.uuid4().hex}"
    payload = {
        "iss": issuer,
        "aud": audience,
        "iat": resolved_issued_at,
        "jti": resolved_token_id,
        "sub": user_id,
        "roles": list(roles),
        "tenant_ids": list(tenant_ids),
        "project_ids": list(project_ids),
        "actor_kind": actor_kind,
        "exp": expires_at,
    }
    payload_part = _base64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"auris.v1.{payload_part}".encode()
    signature = _base64url_encode(
        hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    )
    return f"auris.v1.{payload_part}.{signature}"


def get_auth_provider() -> AuthProvider:
    settings = get_settings()
    provider = settings.auth_provider.lower().strip()
    if provider == "auto":
        if settings.app_env in {"local", "test", "ci"} and settings.allow_dev_auth:
            return StaticDevAuthProvider(settings)
        return SignedTokenAuthProvider(settings)
    if provider == "dev":
        if settings.app_env not in {"local", "test", "ci"} or not settings.allow_dev_auth:
            raise ApiError("DEV_TOKEN_DISABLED", "生产环境不能使用本地演示 token", 500)
        return StaticDevAuthProvider(settings)
    if provider in {"signed", "hmac", "hmac_sha256"}:
        return SignedTokenAuthProvider(settings)
    raise ApiError("AUTH_PROVIDER_INVALID", f"未知认证提供方：{settings.auth_provider}", 500)


def authenticate_bearer(authorization: str | None) -> AuthActor:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError("UNAUTHORIZED", "缺少 Authorization Bearer token", 401)
    token = authorization.removeprefix("Bearer ").strip()
    return get_auth_provider().authenticate(token)
