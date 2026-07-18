from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Barrier, Lock
from typing import Any

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.services.idempotency_service import replay_or_conflict, save_idempotency_result


@dataclass(frozen=True)
class Attempt:
    kind: str
    value: Any


def _context(*, user_id: str, key: str) -> RequestContext:
    return RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id=user_id,
        roles=("project_admin",),
        request_id=f"request-{user_id}",
        trace_id=f"trace-{user_id}",
        idempotency_key=key,
    )


def _run_concurrently(*, key: str, request_hashes: tuple[str, str]) -> tuple[list[Attempt], int]:
    start = Barrier(2)
    lock = Lock()
    side_effect_count = 0

    def execute(index: int) -> Attempt:
        nonlocal side_effect_count
        ctx = _context(user_id=f"concurrent-user-{index}", key=key)
        body_hash = request_hashes[index]
        start.wait(timeout=5)
        try:
            with SessionLocal() as session:
                replay = replay_or_conflict(
                    session,
                    ctx,
                    operation="create:concurrency-probe",
                    body_hash=body_hash,
                )
                if replay is not None:
                    return Attempt("response", replay)

                with lock:
                    side_effect_count += 1
                # Keep the winner transaction open long enough for the contender
                # to exercise the database uniqueness path.
                time.sleep(0.2)
                response = {"data": {"winner": ctx.user_id, "request_hash": body_hash}}
                save_idempotency_result(
                    session,
                    ctx,
                    operation="create:concurrency-probe",
                    body_hash=body_hash,
                    status_code=202,
                    response_json=response,
                )
                session.commit()
                return Attempt("response", response)
        except ApiError as exc:
            return Attempt("api_error", exc.code)
        except Exception as exc:  # pragma: no cover - assertion reports the concrete race failure
            return Attempt("exception", f"{type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = list(executor.map(execute, range(2)))
    return attempts, side_effect_count


def test_same_scoped_key_and_payload_execute_once_across_users() -> None:
    attempts, side_effect_count = _run_concurrently(
        key="concurrent-same-payload",
        request_hashes=("a" * 64, "a" * 64),
    )

    assert side_effect_count == 1
    assert [attempt.kind for attempt in attempts] == ["response", "response"]
    assert attempts[0].value == attempts[1].value


def test_same_scoped_key_with_different_payload_conflicts_under_concurrency() -> None:
    attempts, side_effect_count = _run_concurrently(
        key="concurrent-different-payload",
        request_hashes=("b" * 64, "c" * 64),
    )

    assert side_effect_count == 1
    assert sorted(attempt.kind for attempt in attempts) == ["api_error", "response"]
    assert next(attempt.value for attempt in attempts if attempt.kind == "api_error") == (
        "IDEMPOTENCY_KEY_CONFLICT"
    )
