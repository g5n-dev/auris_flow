from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_release_gate_rejects_real_stack_skip() -> None:
    env = os.environ.copy()
    env["AURIS_SKIP_REAL_STACK_E2E"] = "1"

    result = subprocess.run(
        ["bash", "scripts/verify_release.sh"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert "AURIS_SKIP_REAL_STACK_E2E=1 is not allowed" in result.stderr
    assert "Using Python:" not in result.stdout
    assert "verify_all ok" not in result.stdout
