#!/usr/bin/env python3
"""Seed one scoped Qdrant point for the native-Linux recovery release gate."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


BASE_URL = "http://qdrant:6333"
API_KEY_FILE = Path("/run/secrets/qdrant_api_key")
COLLECTION = "auris_restore_gate"


def _request(method: str, path: str, body: object | None = None) -> dict[str, Any]:
    payload = None
    headers = {
        "Accept": "application/json",
        "api-key": API_KEY_FILE.read_text(encoding="utf-8").strip(),
    }
    if not headers["api-key"]:
        raise RuntimeError("Qdrant gate API key is empty")
    if body is not None:
        payload = json.dumps(
            body,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        BASE_URL + path,
        data=payload,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Qdrant gate seed failed with HTTP {exc.code}") from exc
    if len(raw) > 1024 * 1024:
        raise RuntimeError("Qdrant gate seed response exceeds 1 MiB")
    document = json.loads(raw)
    if not isinstance(document, dict) or document.get("status") != "ok":
        raise RuntimeError("Qdrant gate seed response is invalid")
    return document


def main() -> int:
    _request(
        "PUT",
        f"/collections/{COLLECTION}",
        {"vectors": {"distance": "Cosine", "size": 4}},
    )
    _request(
        "PUT",
        f"/collections/{COLLECTION}/points?wait=true",
        {
            "points": [
                {
                    "id": 7001,
                    "payload": {
                        "project_id": "release_recovery_gate",
                        "tenant_id": "auris_release",
                        "trace_id": "trace_release_recovery_gate_0001",
                    },
                    "vector": [0.25, 0.5, 0.75, 1.0],
                }
            ]
        },
    )
    point = _request(
        "GET",
        f"/collections/{COLLECTION}/points/7001?with_payload=true&with_vector=true",
    )
    result = point.get("result")
    if not isinstance(result, dict) or result.get("id") != 7001:
        raise RuntimeError("Qdrant gate seed point was not readable")
    print("Qdrant recovery-gate fixture seeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
