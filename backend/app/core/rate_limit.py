from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol, cast

from redis import Redis

from app.core.config import Settings

FIXED_WINDOW_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_after_seconds: int
    backend: str


class RateLimiter(Protocol):
    def allow(self, key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        """Return the current fixed-window rate-limit decision."""


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._windows: dict[str, tuple[int, float]] = {}

    def allow(self, key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        now = time.time()
        count, reset_at = self._windows.get(key, (0, now + window_seconds))
        if now >= reset_at:
            count, reset_at = 0, now + window_seconds
        count += 1
        self._windows[key] = (count, reset_at)
        remaining = max(limit - count, 0)
        reset_after = max(int(reset_at - now), 1)
        return RateLimitDecision(
            allowed=count <= limit,
            limit=limit,
            remaining=remaining,
            reset_after_seconds=reset_after,
            backend="memory",
        )


class UnavailableRateLimiter:
    def allow(self, _key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        return RateLimitDecision(
            allowed=False,
            limit=limit,
            remaining=0,
            reset_after_seconds=max(window_seconds, 1),
            backend="redis-unavailable",
        )


class RedisRateLimiter:
    def __init__(
        self,
        redis: Redis,
        fallback: RateLimiter | None = None,
        *,
        fail_closed: bool = False,
    ) -> None:
        self.redis = redis
        self.fallback = fallback or InMemoryRateLimiter()
        self.fail_closed = fail_closed

    def allow(self, key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        redis_key = f"auris:rate-limit:{key}:{int(time.time() // window_seconds)}"
        try:
            result = cast(
                list[Any],
                self.redis.eval(FIXED_WINDOW_SCRIPT, 1, redis_key, window_seconds),
            )
            count = int(result[0])
            ttl = int(result[1])
            reset_after = ttl if ttl and ttl > 0 else window_seconds
            return RateLimitDecision(
                allowed=count <= limit,
                limit=limit,
                remaining=max(limit - count, 0),
                reset_after_seconds=reset_after,
                backend="redis",
            )
        except Exception:
            if self.fail_closed:
                return RateLimitDecision(
                    allowed=False,
                    limit=limit,
                    remaining=0,
                    reset_after_seconds=max(window_seconds, 1),
                    backend="redis-unavailable",
                )
            return self.fallback.allow(key, limit=limit, window_seconds=window_seconds)


def build_rate_limiter(settings: Settings) -> RateLimiter:
    fail_closed = settings.app_env.strip().lower() in {"prod", "production", "release"}
    if settings.redis_url:
        redis = Redis.from_url(settings.redis_url, socket_connect_timeout=0.05, socket_timeout=0.05)
        return RedisRateLimiter(redis, fail_closed=fail_closed)
    if fail_closed:
        return UnavailableRateLimiter()
    return InMemoryRateLimiter()
