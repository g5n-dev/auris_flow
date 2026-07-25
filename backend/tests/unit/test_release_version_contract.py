from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[3]
READINESS = ROOT / "scripts" / "check_platform_readiness.py"


def _load_readiness() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_platform_readiness", READINESS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_version_contract(root: Path) -> None:
    files = {
        "VERSION": "1.0.0\n",
        "backend/pyproject.toml": ('[project]\nname = "auris-flow-bff"\nversion = "1.0.0"\n'),
        "backend/uv.lock": (
            'version = 1\n\n[[package]]\nname = "auris-flow-bff"\nversion = "1.0.0"\n'
        ),
        "backend/app/main.py": ('app = FastAPI(title="Auris Flow BFF", version="1.0.0")\n'),
        "backend/app/core/config.py": ('class Settings:\n    api_prefix: str = "/api/v1"\n'),
        "prototype/auris-flow-ui/package.json": json.dumps(
            {"name": "auris-flow-ui", "version": "1.0.0"}
        ),
        "prototype/auris-flow-ui/package-lock.json": json.dumps(
            {
                "name": "auris-flow-ui",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "packages": {"": {"name": "auris-flow-ui", "version": "1.0.0"}},
            }
        ),
        "doc/backend-spec/openapi-v0.1.yaml": (
            "openapi: 3.0.3\n"
            "info:\n"
            "  title: Auris Flow BFF API\n"
            "  version: 1.0.0\n"
            "servers:\n"
            "  - url: /api/v1\n"
        ),
        "production/dagster/pyproject.toml": (
            '[project]\nname = "auris-flow-dagster"\nversion = "1.0.0"\n'
        ),
        "production/dagster/uv.lock": (
            'version = 1\n\n[[package]]\nname = "auris-flow-dagster"\nversion = "1.0.0"\n'
        ),
    }
    for relative_path, contents in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")


def test_current_release_version_contract_is_coherent() -> None:
    readiness = _load_readiness()

    assert "VERSION" in readiness.RELEASE_REQUIRED_TRACKED_PATHS
    assert readiness.validate_release_version_contract(ROOT) == []


@pytest.mark.parametrize(
    ("relative_path", "old", "new"),
    [
        ("backend/pyproject.toml", 'version = "1.0.0"', 'version = "1.0.1"'),
        ("backend/uv.lock", 'version = "1.0.0"', 'version = "1.0.1"'),
        ("backend/app/main.py", 'version="1.0.0"', 'version="1.0.1"'),
        (
            "prototype/auris-flow-ui/package.json",
            '"version": "1.0.0"',
            '"version": "1.0.1"',
        ),
        (
            "prototype/auris-flow-ui/package-lock.json",
            '"version": "1.0.0"',
            '"version": "1.0.1"',
        ),
        ("doc/backend-spec/openapi-v0.1.yaml", "version: 1.0.0", "version: 1.0.1"),
        (
            "production/dagster/pyproject.toml",
            'version = "1.0.0"',
            'version = "1.0.1"',
        ),
        (
            "production/dagster/uv.lock",
            'version = "1.0.0"',
            'version = "1.0.1"',
        ),
    ],
)
def test_release_version_contract_rejects_component_drift(
    tmp_path: Path,
    relative_path: str,
    old: str,
    new: str,
) -> None:
    readiness = _load_readiness()
    _write_version_contract(tmp_path)
    target = tmp_path / relative_path
    source = target.read_text(encoding="utf-8")
    assert old in source
    target.write_text(source.replace(old, new, 1), encoding="utf-8")

    failures = readiness.validate_release_version_contract(tmp_path)

    assert any(relative_path in failure for failure in failures), failures


def test_release_version_contract_fails_closed_when_source_is_missing(
    tmp_path: Path,
) -> None:
    readiness = _load_readiness()
    _write_version_contract(tmp_path)
    (tmp_path / "backend/app/main.py").unlink()

    failures = readiness.validate_release_version_contract(tmp_path)

    assert any("backend/app/main.py" in failure and "missing" in failure for failure in failures)


@pytest.mark.parametrize("version_source", ["1.0.0-rc.1\n", " 1.0.0\n", "1.0\n"])
def test_release_version_contract_rejects_non_stable_root_version(
    tmp_path: Path,
    version_source: str,
) -> None:
    readiness = _load_readiness()
    _write_version_contract(tmp_path)
    (tmp_path / "VERSION").write_text(version_source, encoding="utf-8")

    failures = readiness.validate_release_version_contract(tmp_path)

    assert "VERSION must contain one stable SemVer value" in failures


def test_release_version_contract_rejects_nested_ui_lock_drift(
    tmp_path: Path,
) -> None:
    readiness = _load_readiness()
    _write_version_contract(tmp_path)
    lock_path = tmp_path / "prototype/auris-flow-ui/package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["packages"][""]["version"] = "1.0.1"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    failures = readiness.validate_release_version_contract(tmp_path)

    assert any("package-lock.json packages['']" in failure for failure in failures)


@pytest.mark.parametrize(
    ("relative_path", "old", "new"),
    [
        ("backend/app/core/config.py", "/api/v1", "/api/v2"),
        ("doc/backend-spec/openapi-v0.1.yaml", "/api/v1", "/api/v2"),
    ],
)
def test_release_version_contract_rejects_api_major_drift(
    tmp_path: Path,
    relative_path: str,
    old: str,
    new: str,
) -> None:
    readiness = _load_readiness()
    _write_version_contract(tmp_path)
    target = tmp_path / relative_path
    target.write_text(
        target.read_text(encoding="utf-8").replace(old, new),
        encoding="utf-8",
    )

    failures = readiness.validate_release_version_contract(tmp_path)

    assert any(relative_path in failure and "API major" in failure for failure in failures)


@pytest.mark.parametrize("tag", ["v1.0.0", "v1.0.0-rc.1", "v1.0.0-rc.42"])
def test_release_tag_maps_to_product_version(tag: str) -> None:
    readiness = _load_readiness()

    assert readiness.release_tag_matches_version(tag, "1.0.0")


@pytest.mark.parametrize(
    "tag",
    [
        "1.0.0",
        "v1.0.1",
        "v1.0.0-rc.0",
        "v1.0.0-rc.01",
        "v1.0.0-beta.1",
        "v01.0.0",
    ],
)
def test_release_tag_rejects_invalid_or_foreign_product_version(tag: str) -> None:
    readiness = _load_readiness()

    assert not readiness.release_tag_matches_version(tag, "1.0.0")


def test_release_readiness_executes_version_validation() -> None:
    source = READINESS.read_text(encoding="utf-8")

    assert "tree_failures.extend(validate_release_version_contract())" in source


def test_version_policy_documents_single_source_tag_and_api_major_mapping() -> None:
    policy = (ROOT / "doc/release/versioning-and-compatibility.md").read_text(encoding="utf-8")

    for statement in (
        "`VERSION` 是稳定基础版本的唯一真相源",
        "`vX.Y.Z-rc.N` 必须映射到同一 `X.Y.Z`",
        "`/api/v{MAJOR}`",
    ):
        assert statement in policy


@pytest.mark.parametrize(
    ("tag", "expected_returncode", "expected_error"),
    [
        ("v1.0.0", 0, ""),
        ("v1.0.0-rc.2", 1, "final SemVer tags only"),
        ("v1.0.1", 1, "does not match VERSION"),
    ],
)
def test_release_workflow_binds_tag_to_version(
    tmp_path: Path,
    tag: str,
    expected_returncode: int,
    expected_error: str,
) -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/release-images.yml").read_text(encoding="utf-8")
    )
    context_steps = [
        step for step in workflow["jobs"]["release-context"]["steps"] if step.get("id") == "context"
    ]
    assert len(context_steps) == 1
    run_block = context_steps[0]["run"]
    python_source = run_block.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    output_path = tmp_path / "github-output"
    environment = {**os.environ, "GITHUB_OUTPUT": str(output_path)}

    completed = subprocess.run(
        [sys.executable, "-", tag, "a" * 40],
        cwd=ROOT,
        input=python_source,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == expected_returncode, completed.stderr
    if expected_returncode:
        assert expected_error in completed.stderr
