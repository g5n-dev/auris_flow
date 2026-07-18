from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[3]
VERIFY_ALL = ROOT / "scripts" / "verify_all.sh"
READINESS = ROOT / "scripts" / "check_platform_readiness.py"


def _load_readiness() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_platform_readiness", READINESS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _release_block(script: str) -> str:
    start = script.index('if [ "${AURIS_RELEASE_CHECK:-0}" = "1" ]; then')
    end = script.index(
        'else\n  "${PYTHON_BIN}" scripts/scan_secrets.py',
        start,
    )
    return script[start:end]


def test_release_mode_runs_production_compose_and_dagster_quality_gates() -> None:
    script = VERIFY_ALL.read_text(encoding="utf-8")
    release_block = _release_block(script)

    for command in (
        '"${PYTHON_BIN}" scripts/verify_production_compose.py',
        '"${PYTHON_BIN}" -m pytest production/tests',
        "uv run --frozen --all-extras --project production/dagster pytest production/dagster/tests",
        "uv run --frozen --all-extras --project production/dagster ruff format --check "
        "production/dagster/src production/dagster/tests",
        "uv run --frozen --all-extras --project production/dagster ruff check "
        "production/dagster/src production/dagster/tests",
        "uv run --frozen --all-extras --project production/dagster mypy production/dagster/src",
    ):
        assert command in release_block


def test_regular_python_quality_gate_covers_production_policy_tests() -> None:
    script = VERIFY_ALL.read_text(encoding="utf-8")

    assert "ruff format --check backend scripts production/tests" in script
    assert "ruff check backend scripts production/tests" in script


def test_readiness_contract_requires_every_production_release_gate() -> None:
    readiness = _load_readiness()
    quality_check = next(
        check for check in readiness.CHECKS if check.key == "one_command_quality_gate"
    )
    required_patterns = quality_check.contains["scripts/verify_all.sh"]

    for pattern in (
        "scripts/verify_production_compose.py",
        "pytest production/tests",
        "pytest production/dagster/tests",
        "ruff format --check production/dagster/src production/dagster/tests",
        "ruff check production/dagster/src production/dagster/tests",
        "mypy production/dagster/src",
        "ruff format --check backend scripts production/tests",
        "ruff check backend scripts production/tests",
        "bash scripts/verify_production_path.sh",
    ):
        if pattern == "bash scripts/verify_production_path.sh":
            assert pattern in quality_check.contains["scripts/verify_release.sh"]
        else:
            assert pattern in required_patterns

    for path in (
        "scripts/verify_production_path.sh",
        "scripts/verify_production_path_gate.py",
        "production/tests/production-path-gate.compose.yaml",
        "production/tests/production-path-gate.md",
        "backend/tests/unit/test_production_path_gate.py",
    ):
        assert path in quality_check.paths or path in readiness.RELEASE_REQUIRED_TRACKED_PATHS
