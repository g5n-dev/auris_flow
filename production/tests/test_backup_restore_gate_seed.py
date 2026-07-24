from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


SCRIPT = Path(__file__).with_name("backup_restore_gate_seed.py")


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backup_restore_gate_seed", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_is_one_scoped_recovery_fixture_not_embedding_evidence(
    monkeypatch,
) -> None:
    module = _load_module()
    calls: list[tuple[str, str, object | None]] = []

    def fake_request(
        method: str, path: str, body: object | None = None
    ) -> dict[str, object]:
        calls.append((method, path, body))
        if method == "GET":
            return {"status": "ok", "result": {"id": 7001}}
        return {"status": "ok", "result": True}

    monkeypatch.setattr(module, "_request", fake_request)

    assert module.main() == 0
    assert len(calls) == 3
    upsert = calls[1][2]
    assert isinstance(upsert, dict)
    points = upsert["points"]
    assert isinstance(points, list) and len(points) == 1
    point = points[0]
    assert point["payload"]["tenant_id"] == "auris_release"
    assert point["payload"]["project_id"] == "release_recovery_gate"
    assert "embedding" not in point["payload"]
