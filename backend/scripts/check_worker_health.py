from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("worker heartbeat is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if len(args) != 2:
        print("usage: check_worker_health.py HEALTH_FILE MAX_AGE_SECONDS", file=sys.stderr)
        return 2
    path = Path(args[0])
    try:
        maximum_age = max(1, int(args[1]))
        payload = json.loads(path.read_text(encoding="utf-8"))
        heartbeat = _parse_timestamp(payload.get("heartbeat_at"))
        age = (datetime.now(UTC) - heartbeat).total_seconds()
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"worker health unavailable: {exc.__class__.__name__}", file=sys.stderr)
        return 1
    if payload.get("status") not in {"running", "stopping"} or not 0 <= age <= maximum_age:
        print("worker heartbeat is stale or stopped", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
