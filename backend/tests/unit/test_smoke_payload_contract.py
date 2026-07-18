from __future__ import annotations

import json
import re
from pathlib import Path

from app.schemas.requests import TaskRunRequest

ROOT = Path(__file__).resolve().parents[3]


def test_shell_smoke_task_run_body_matches_the_public_request_contract() -> None:
    script = (ROOT / "backend/scripts/smoke_backend.sh").read_text(encoding="utf-8")
    match = re.search(r"^BODY='([^']+)'$", script, flags=re.MULTILINE)

    assert match is not None
    payload = json.loads(match.group(1))
    validated = TaskRunRequest.model_validate(payload)

    assert validated.task_version_id == "task_version_v3_2_1"
    assert set(payload) == {"task_version_id", "trigger_type", "partition_key"}
