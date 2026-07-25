"""Short-lived browser binding for an OIDC Authorization Code transaction."""

from __future__ import annotations

import hashlib
import re
import secrets

from starlette.responses import Response

from app.core.config import is_production_environment

LOCAL_COOKIE_NAME = "auris_oidc_transaction"
PRODUCTION_COOKIE_NAME = "__Host-auris_oidc_transaction"
_TRANSACTION_BINDING_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def authorization_transaction_cookie_name(app_env: str) -> str:
    return PRODUCTION_COOKIE_NAME if is_production_environment(app_env) else LOCAL_COOKIE_NAME


def new_authorization_transaction_binding() -> str:
    """Return a fresh opaque browser binding without reflecting cookie input."""

    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


def set_authorization_transaction_cookie(
    response: Response,
    *,
    app_env: str,
    transaction_binding: str,
    max_age: int,
) -> None:
    if _TRANSACTION_BINDING_PATTERN.fullmatch(transaction_binding) is None:
        raise ValueError("OIDC transaction binding is invalid")
    if not 1 <= max_age <= 600:
        raise ValueError("OIDC transaction cookie lifetime is invalid")
    response.set_cookie(
        key=authorization_transaction_cookie_name(app_env),
        value=transaction_binding,
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
