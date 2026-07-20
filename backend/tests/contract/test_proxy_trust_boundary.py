from __future__ import annotations

from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.rate_limit import InMemoryRateLimiter
from app.main import app, settings

EDGE_INTERNAL_IP = "172.31.48.10"
SCOPE_HEADERS = {
    "Authorization": "Bearer dev-token",
    "X-Tenant-Id": "aurora_auto",
    "X-Project-Id": "sales_qa",
}


def _exercise_rotating_forwarded_headers(
    *,
    peer_ip: str,
    forwarded_values: tuple[str, str, str],
) -> list[int]:
    old_limit = settings.rate_limit_per_minute
    old_limiter = app.state.rate_limiter
    settings.rate_limit_per_minute = 2
    app.state.rate_limiter = InMemoryRateLimiter()
    proxied_app = ProxyHeadersMiddleware(app, trusted_hosts=[EDGE_INTERNAL_IP])
    try:
        with TestClient(proxied_app, client=(peer_ip, 43120)) as client:
            responses = [
                client.get(
                    "/api/v1/insights/ops-summary",
                    headers={
                        **SCOPE_HEADERS,
                        "X-Forwarded-For": forwarded_value,
                    },
                )
                for forwarded_value in forwarded_values
            ]
    finally:
        settings.rate_limit_per_minute = old_limit
        app.state.rate_limiter = old_limiter
    return [response.status_code for response in responses]


def test_untrusted_asgi_peer_cannot_rotate_xff_to_escape_rate_limit_bucket() -> None:
    statuses = _exercise_rotating_forwarded_headers(
        peer_ip="172.31.48.20",
        forwarded_values=("198.51.100.1", "198.51.100.2", "198.51.100.3"),
    )

    assert statuses == [200, 200, 429]


def test_trusted_edge_overwrite_keeps_one_client_in_one_rate_limit_bucket() -> None:
    # The production Nginx policy overwrites every user-supplied XFF value with
    # its socket peer's $remote_addr before this trusted ASGI hop. These three
    # requests therefore carry the same authoritative client address.
    statuses = _exercise_rotating_forwarded_headers(
        peer_ip=EDGE_INTERNAL_IP,
        forwarded_values=("198.51.100.44", "198.51.100.44", "198.51.100.44"),
    )

    assert statuses == [200, 200, 429]
