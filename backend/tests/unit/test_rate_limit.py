from __future__ import annotations

from types import SimpleNamespace

from app.core.rate_limit import (
    InMemoryRateLimiter,
    RedisRateLimiter,
    UnavailableRateLimiter,
    build_rate_limiter,
)


class FailingRedis:
    def eval(self, *_args: object) -> list[int]:
        raise ConnectionError("redis unavailable")


class RecordingRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def eval(self, *args: object) -> list[int]:
        self.calls.append(args)
        return [2, 41]


def test_redis_rate_limiter_uses_memory_fallback_outside_production() -> None:
    limiter = RedisRateLimiter(FailingRedis())  # type: ignore[arg-type]

    decision = limiter.allow("tenant:user", limit=2, window_seconds=60)

    assert decision.allowed is True
    assert decision.backend == "memory"


def test_redis_rate_limiter_fails_closed_in_production() -> None:
    limiter = RedisRateLimiter(FailingRedis(), fail_closed=True)  # type: ignore[arg-type]

    decision = limiter.allow("tenant:user", limit=2, window_seconds=60)

    assert decision.allowed is False
    assert decision.remaining == 0
    assert decision.backend == "redis-unavailable"


def test_redis_rate_limiter_executes_count_and_expiry_atomically() -> None:
    redis = RecordingRedis()
    limiter = RedisRateLimiter(redis)  # type: ignore[arg-type]

    decision = limiter.allow("tenant:user", limit=3, window_seconds=60)

    assert decision.allowed is True
    assert decision.remaining == 1
    assert decision.reset_after_seconds == 41
    assert len(redis.calls) == 1
    script, key_count, key, ttl = redis.calls[0]
    assert "INCR" in str(script)
    assert "EXPIRE" in str(script)
    assert key_count == 1
    assert str(key).startswith("auris:rate-limit:tenant:user:")
    assert ttl == 60


def test_build_rate_limiter_keeps_memory_only_for_non_production_without_redis() -> None:
    local = build_rate_limiter(SimpleNamespace(app_env="local", redis_url=""))  # type: ignore[arg-type]
    production = build_rate_limiter(
        SimpleNamespace(app_env="production", redis_url="")  # type: ignore[arg-type]
    )

    assert isinstance(local, InMemoryRateLimiter)
    assert isinstance(production, UnavailableRateLimiter)
    decision = production.allow("tenant:user", limit=2, window_seconds=60)
    assert decision.allowed is False
    assert decision.backend == "redis-unavailable"
