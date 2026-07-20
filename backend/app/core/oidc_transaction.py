"""Short-lived browser binding for an OIDC Authorization Code transaction."""

from __future__ import annotations

import re
import secrets

from starlette.responses import Response

from app.core.config import is_production_environment

LOCAL_COOKIE_NAME = "auris_oidc_transaction"
PRODUCTION_COOKIE_NAME = "__Host-auris_oidc_transaction"
_TRANSACTION_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")


def authorization_transaction_cookie_name(app_env: str) -> str:
    return PRODUCTION_COOKIE_NAME if is_production_environment(app_env) else LOCAL_COOKIE_NAME


def authorization_transaction_secret(existing: str | None) -> str:
    if isinstance(existing, str) and _TRANSACTION_SECRET_PATTERN.fullmatch(existing):
        return existing
    return secrets.token_urlsafe(48)


def set_authorization_transaction_cookie(
    response: Response,
    *,
    app_env: str,
    transaction_secret: str,
    max_age: int,
) -> None:
    response.set_cookie(
        key=authorization_transaction_cookie_name(app_env),
        value=transaction_secret,
        max_age=max_age,
        path="/",
        secure=is_production_environment(app_env),
        httponly=True,
        samesite="lax",
    )


def clear_authorization_transaction_cookie(response: Response, *, app_env: str) -> None:
    response.delete_cookie(
        key=authorization_transaction_cookie_name(app_env),
        path="/",
        secure=is_production_environment(app_env),
        httponly=True,
        samesite="lax",
    )
