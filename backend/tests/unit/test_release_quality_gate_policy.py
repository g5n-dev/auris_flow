from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

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


def test_release_mode_audits_hashed_locked_runtime_graphs() -> None:
    script = VERIFY_ALL.read_text(encoding="utf-8")
    release_block = _release_block(script)

    assert "--local" not in release_block
    assert "--skip-editable" not in release_block
    assert "--project backend" in release_block
    assert "backend-runtime-requirements.txt" in release_block
    assert "--project production/dagster" in release_block
    assert "dagster-runtime-requirements.txt" in release_block
    assert release_block.count("--no-header") == 2
    assert release_block.count("--strict --require-hashes --disable-pip") == 2
    backend_export = release_block.index(
        '--output-file "${release_audit_dir}/backend-runtime-requirements.txt"'
    )
    backend_audit = release_block.index(
        '--requirement "${release_audit_dir}/backend-runtime-requirements.txt"'
    )
    dagster_export = release_block.index(
        '--output-file "${release_audit_dir}/dagster-runtime-requirements.txt"'
    )
    dagster_audit = release_block.index(
        '--requirement "${release_audit_dir}/dagster-runtime-requirements.txt"'
    )
    assert backend_export < backend_audit < dagster_export < dagster_audit


def test_regular_python_quality_gate_covers_production_policy_tests() -> None:
    script = VERIFY_ALL.read_text(encoding="utf-8")

    assert "ruff format --check backend scripts production/tests" in script
    assert "ruff check backend scripts production/tests" in script


def test_release_uses_the_production_mysql_gates_own_phase_deadlines() -> None:
    release = (ROOT / "scripts" / "verify_release.sh").read_text(encoding="utf-8")

    assert "bash scripts/verify_production_mysql_migrations.sh" in release
    assert '--label "production MySQL migration gate"' not in release


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
        "backend-runtime-requirements.txt",
        "dagster-runtime-requirements.txt",
        "--strict --require-hashes --disable-pip",
        "bash scripts/verify_production_path.sh",
        "bash scripts/verify_production_mysql_migrations.sh",
    ):
        if pattern in {
            "bash scripts/verify_production_path.sh",
            "bash scripts/verify_production_mysql_migrations.sh",
        }:
            assert pattern in quality_check.contains["scripts/verify_release.sh"]
        else:
            assert pattern in required_patterns

    for path in (
        "backend/pyproject.toml",
        "backend/uv.lock",
        "config/release/exact-artifact-license-conclusions.json",
        "prototype/auris-flow-ui/package.json",
        "prototype/auris-flow-ui/package-lock.json",
        "scripts/verify_production_path.sh",
        "scripts/verify_production_path_gate.py",
        "production/tests/production-path-gate.compose.yaml",
        "production/tests/production-path-gate.md",
        "backend/tests/unit/test_production_path_gate.py",
        "scripts/verify_production_mysql_migrations.sh",
        "backend/scripts/verify_mysql_migration_security.py",
        "production/tests/test_mysql_migration_security.py",
    ):
        assert path in quality_check.paths or path in readiness.RELEASE_REQUIRED_TRACKED_PATHS


def test_readiness_parses_production_path_contract_instead_of_matching_claim_text(
    tmp_path: Path,
) -> None:
    readiness = _load_readiness()
    source = ROOT / "production" / "tests" / "production-path-gate.compose.yaml"
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    document["services"].pop("production-path-verifier")
    destination = tmp_path / "production" / "tests"
    destination.mkdir(parents=True)
    (destination / "production-path-gate.compose.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False),
        encoding="utf-8",
    )

    failures = readiness.validate_production_path_readiness_contract(tmp_path)

    assert any("service is missing: production-path-verifier" in failure for failure in failures)


def test_readiness_keeps_blocked_production_blueprint_outside_p0_score() -> None:
    readiness = _load_readiness()
    quality_check = next(
        check for check in readiness.CHECKS if check.key == "one_command_quality_gate"
    )

    assert readiness.validate_production_path_readiness_contract(ROOT) == []
    assert "fail-closed" in quality_check.title
    assert "P2" in quality_check.rationale
    assert "不代表" in quality_check.rationale


def test_readiness_rejects_commented_out_production_path_release_command(
    tmp_path: Path,
) -> None:
    readiness = _load_readiness()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "verify_release.sh").write_text(
        "#!/usr/bin/env bash\n"
        "# bash scripts/verify_production_path.sh\n"
        "scripts/generate_supply_chain_evidence.py\n"
        "scripts/finalize_release_evidence.py\n",
        encoding="utf-8",
    )

    failures = readiness.validate_release_gate_wiring(tmp_path)

    assert any("verify_production_path.sh" in failure for failure in failures), failures


def test_current_release_gate_has_executable_production_path_command() -> None:
    readiness = _load_readiness()

    assert readiness.validate_release_gate_wiring(ROOT) == []


def test_repository_trust_contract_matches_the_publication_target() -> None:
    readiness = _load_readiness()

    assert readiness.OFFICIAL_GITHUB_REPOSITORY == "g5n-dev/auris_flow"
    assert readiness.validate_repository_trust_contract(ROOT) == []


def test_repository_trust_contract_rejects_a_legacy_workflow_binding(
    tmp_path: Path,
) -> None:
    readiness = _load_readiness()
    for relative_path in readiness.REPOSITORY_TRUST_BINDINGS:
        source = ROOT / relative_path
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    workflow = tmp_path / ".github" / "workflows" / "release-images.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "g5n-dev/auris_flow",
            "auris-flow/auris-flow",
        ),
        encoding="utf-8",
    )

    failures = readiness.validate_repository_trust_contract(tmp_path)

    assert any(
        ".github/workflows/release-images.yml" in failure
        and "legacy repository identity" in failure
        for failure in failures
    ), failures


@pytest.mark.parametrize(
    ("relative_path", "original", "replacement"),
    [
        (
            ".github/workflows/release-images.yml",
            "g5n-dev/auris_flow",
            "attacker/repo",
        ),
        (
            "scripts/verify_visual_baseline.py",
            '("g5n-dev", "auris_flow")',
            '("attacker", "repo")',
        ),
        (
            "scripts/finalize_release_evidence.py",
            "g5n-dev/auris_flow",
            "attacker/repo",
        ),
        (
            "prototype/auris-flow-ui/scripts/frontend-bundle-lock.mjs",
            "g5n-dev",
            "attacker",
        ),
        (
            "scripts/verify_frontend_bundle.mjs",
            "FRONTEND_BUNDLE_OFFICIAL_REPOSITORY;",
            '"attacker/repo";',
        ),
        (
            "scripts/release_bundle.py",
            "g5n-dev/auris_flow",
            "attacker/repo",
        ),
        (
            "production/visual/Dockerfile",
            "g5n-dev/auris_flow",
            "attacker/repo",
        ),
    ],
)
def test_repository_trust_contract_rejects_each_component_drift(
    tmp_path: Path,
    relative_path: str,
    original: str,
    replacement: str,
) -> None:
    readiness = _load_readiness()
    for trust_path in readiness.REPOSITORY_TRUST_BINDINGS:
        source = ROOT / trust_path
        destination = tmp_path / trust_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    target = tmp_path / relative_path
    source = target.read_text(encoding="utf-8")
    assert original in source
    target.write_text(
        source.replace(original, replacement),
        encoding="utf-8",
    )

    failures = readiness.validate_repository_trust_contract(tmp_path)

    assert any(relative_path in failure for failure in failures), failures


def test_release_readiness_executes_repository_trust_validation() -> None:
    source = READINESS.read_text(encoding="utf-8")

    assert "tree_failures.extend(validate_repository_trust_contract())" in source


def test_readiness_contract_targets_the_moduleized_frontend_surface() -> None:
    readiness = _load_readiness()
    domain_check = next(
        check for check in readiness.CHECKS if check.key == "evaluation_labeling_insights_domains"
    )

    assert "prototype/auris-flow-ui/src/App.tsx" not in domain_check.paths
    for path in (
        "prototype/auris-flow-ui/src/workspace/ModuleWorkspace.tsx",
        "prototype/auris-flow-ui/src/workspace/moduleWorkspaceCatalog.ts",
        "prototype/auris-flow-ui/src/features/labels/LabelsModule.tsx",
        "prototype/auris-flow-ui/src/features/evaluation/EvaluationModule.tsx",
        "prototype/auris-flow-ui/src/features/insights/InsightsModule.tsx",
        "prototype/auris-flow-ui/src/catalogs/module-catalog.json",
        "prototype/auris-flow-ui/scripts/check-bundle-budget.mjs",
    ):
        assert path in domain_check.paths

    assert (
        "moduleConfigs"
        in domain_check.contains["prototype/auris-flow-ui/src/workspace/moduleWorkspaceCatalog.ts"]
    )
    assert (
        "initialClosureBrotliBytes"
        in domain_check.contains["prototype/auris-flow-ui/scripts/check-bundle-budget.mjs"]
    )
