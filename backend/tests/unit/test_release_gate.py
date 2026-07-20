from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _isolated_release_gate_repo(tmp_path: Path) -> Path:
    repository = tmp_path / "release-gate-repo"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "verify_release.sh").write_text(
        (ROOT / "scripts" / "verify_release.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (repository / ".gitignore").write_text("/build/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "release-gate@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Release Gate Test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"],
        cwd=repository,
        check=True,
    )
    return repository


def _run_isolated_release_gate(repository: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for variable in (
        "AURIS_SKIP_REAL_STACK_E2E",
        "AURIS_SKIP_REAL_DAGSTER",
        "AURIS_SKIP_PRODUCT_DAGSTER_GATE",
        "AURIS_SKIP_PRODUCTION_PATH_GATE",
    ):
        env.pop(variable, None)
    return subprocess.run(
        ["bash", "scripts/verify_release.sh"],
        cwd=repository,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _write_executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


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


def test_release_gate_rejects_product_dagster_skip_before_work() -> None:
    env = os.environ.copy()
    env["AURIS_SKIP_PRODUCT_DAGSTER_GATE"] = "1"

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
    assert "AURIS_SKIP_PRODUCT_DAGSTER_GATE=1 is not allowed" in result.stderr
    assert "verify_all ok" not in result.stdout


def test_release_gate_rejects_production_path_skip_before_work() -> None:
    result = subprocess.run(
        ["bash", "scripts/verify_release.sh"],
        cwd=ROOT,
        env={**os.environ, "AURIS_SKIP_PRODUCTION_PATH_GATE": "1"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert "AURIS_SKIP_PRODUCTION_PATH_GATE=1 is not allowed" in result.stderr
    assert "verify_all ok" not in result.stdout


def test_release_gate_rejects_symlinked_evidence_directories_before_writing() -> None:
    source = (ROOT / "scripts" / "verify_release.sh").read_text(encoding="utf-8")

    build_guard = 'if [ -L "${ROOT}/build" ]'
    evidence_guard = 'if [ -L "${EVIDENCE_DIR}" ]'
    clean_tree_check = 'source_status="$(git status'
    assert build_guard in source
    assert evidence_guard in source
    assert source.index(build_guard) < source.index(clean_tree_check)
    assert source.index(evidence_guard) < source.index(clean_tree_check)


def test_release_gate_audits_every_locked_runtime_before_final_manifest() -> None:
    source = (ROOT / "scripts" / "verify_release.sh").read_text(encoding="utf-8")

    supply = "scripts/generate_supply_chain_evidence.py"
    backend_audit = "backend-python-audit.json"
    dagster_audit = "dagster-python-audit.json"
    npm_audit = "npm-audit.json"
    finalizer = "scripts/finalize_release_evidence.py"
    assert source.count(finalizer) == 1
    for evidence in (backend_audit, dagster_audit, npm_audit):
        assert evidence in source
    assert "--require-audits" in source
    assert source.count("--no-header") == 2
    assert '--output-file "${EVIDENCE_REL}/backend-runtime-requirements.txt"' in source
    assert '--output-file "${EVIDENCE_DIR}/' not in source
    assert source.index(supply) < source.index(backend_audit)
    assert source.index(backend_audit) < source.index(dagster_audit)
    assert source.index(dagster_audit) < source.index(npm_audit)
    assert source.index(npm_audit) < source.index(finalizer)


def test_failed_audit_runs_full_matrix_and_never_calls_finalizer(
    tmp_path: Path,
) -> None:
    repository = _isolated_release_gate_repo(tmp_path)
    for script_name in (
        "verify_clean_clone.sh",
        "verify_all.sh",
        "verify_production_mysql_migrations.sh",
        "verify_real_stack.sh",
        "verify_real_dagster.sh",
        "verify_product_dagster_path.sh",
        "verify_production_path.sh",
    ):
        _write_executable(repository / "scripts" / script_name, "#!/usr/bin/env bash\nexit 0\n")
    fake_bin = repository / "test-bin"
    fake_python = fake_bin / "python"
    _write_executable(
        fake_python,
        """#!/usr/bin/env bash
set -eu
mkdir -p build/release-evidence
printf '%s\n' "$*" >> build/python-calls.log
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip_audit" ]; then
  if [ "${3:-}" = "--version" ]; then
    echo "pip-audit fixture"
    exit 0
  fi
  output=""
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--output" ]; then
      output="$2"
      break
    fi
    shift
  done
  printf '{"dependencies":[]}\n' > "${output}"
  case "${output}" in
    *backend-python-audit.json) exit 1 ;;
  esac
  exit 0
fi
case "${1:-}" in
  *finalize_release_evidence.py)
    touch build/release-evidence/release-gate-manifest.json
    ;;
esac
exit 0
""",
    )
    _write_executable(
        fake_bin / "uv",
        """#!/usr/bin/env bash
set -eu
output=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output-file" ]; then
    output="$2"
    break
  fi
  shift
done
printf 'fixture==1.0 --hash=sha256:%064d\n' 0 > "${output}"
""",
    )
    _write_executable(
        fake_bin / "npm",
        """#!/usr/bin/env bash
printf '{"metadata":{"vulnerabilities":{"high":0,"critical":0}}}\n'
""",
    )
    _write_executable(fake_bin / "docker", "#!/usr/bin/env bash\nexit 0\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "audit fixtures"],
        cwd=repository,
        check=True,
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PYTHON": str(fake_python),
    }

    result = subprocess.run(
        ["bash", "scripts/verify_release.sh"],
        cwd=repository,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    evidence = repository / "build" / "release-evidence"
    assert result.returncode == 1
    assert "no final release evidence manifest" in result.stderr
    assert (evidence / "backend-python-audit.json").is_file()
    assert (evidence / "dagster-python-audit.json").is_file()
    assert (evidence / "npm-audit.json").is_file()
    assert not (evidence / "release-gate-manifest.json").exists()
    calls = (repository / "build" / "python-calls.log").read_text(encoding="utf-8")
    assert "finalize_release_evidence.py" not in calls


def test_blocked_production_path_stops_release_before_supply_chain(
    tmp_path: Path,
) -> None:
    repository = _isolated_release_gate_repo(tmp_path)
    for script_name in (
        "verify_clean_clone.sh",
        "verify_all.sh",
        "verify_production_mysql_migrations.sh",
        "verify_real_stack.sh",
        "verify_real_dagster.sh",
        "verify_product_dagster_path.sh",
    ):
        _write_executable(repository / "scripts" / script_name, "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        repository / "scripts" / "verify_production_path.sh",
        "#!/usr/bin/env bash\necho production-path-blocked >&2\nexit 73\n",
    )
    fake_bin = repository / "test-bin"
    fake_python = fake_bin / "python"
    _write_executable(
        fake_python,
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> build/python-calls.log\nexit 0\n",
    )
    _write_executable(fake_bin / "docker", "#!/usr/bin/env bash\nexit 0\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "gate fixtures"],
        cwd=repository,
        check=True,
    )

    completed = subprocess.run(
        ["bash", "scripts/verify_release.sh"],
        cwd=repository,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PYTHON": str(fake_python),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 73
    assert "production-path-blocked" in completed.stderr
    calls = (repository / "build" / "python-calls.log").read_text(encoding="utf-8")
    assert "generate_supply_chain_evidence.py" not in calls
    assert "finalize_release_evidence.py" not in calls


@pytest.mark.parametrize("target", ["build", "build/release-evidence"])
@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_release_gate_rejects_unsafe_evidence_path_components(
    tmp_path: Path,
    target: str,
    kind: str,
) -> None:
    repository = _isolated_release_gate_repo(tmp_path)
    path = repository / target
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        symlink_target = tmp_path / f"outside-{target.replace('/', '-')}"
        symlink_target.mkdir()
        path.symlink_to(symlink_target, target_is_directory=True)
    else:
        path.write_text("not a directory\n", encoding="utf-8")

    result = _run_isolated_release_gate(repository)

    assert result.returncode == 2
    assert "must be a real directory" in result.stderr
    assert not (path / "release-write-probe").exists()


def test_release_gate_creates_real_evidence_directories_before_clean_tree_gate(
    tmp_path: Path,
) -> None:
    repository = _isolated_release_gate_repo(tmp_path)
    (repository / "dirty-marker").write_text("force clean-tree rejection\n", encoding="utf-8")

    result = _run_isolated_release_gate(repository)

    assert result.returncode == 2
    assert "clean committed HEAD" in result.stderr
    build = repository / "build"
    evidence = build / "release-evidence"
    assert build.is_dir() and not build.is_symlink()
    assert evidence.is_dir() and not evidence.is_symlink()


def test_real_stack_bootstraps_the_authenticated_minio_bucket_before_strict_readyz() -> None:
    compose = (ROOT / "docker" / "local" / "docker-compose.yml").read_text(encoding="utf-8")
    gate = (ROOT / "scripts" / "verify_real_stack.sh").read_text(encoding="utf-8")
    ui_gate = (ROOT / "scripts" / "verify_ui_bff_e2e.sh").read_text(encoding="utf-8")

    assert "  minio-bootstrap:" in compose
    assert "mc mb --ignore-existing auris/auris-flow-local" in compose
    deadline_setting = "AURIS_MINIO_BOOTSTRAP_TIMEOUT_SECONDS:-60"
    deadline_runner = 'scripts/run_with_deadline.py"'
    bootstrap = 'run --no-TTY --name "${container_name}" --rm --no-deps minio-bootstrap'
    for source in (gate, ui_gate):
        assert deadline_setting in source
        assert deadline_runner in source
        assert bootstrap in source

    assert 'COMPOSE_WAIT_TIMEOUT_SECONDS="${AURIS_REAL_STACK_WAIT_TIMEOUT:-180}"' in gate
    assert 'compose_with_deadline "${COMPOSE_WAIT_DEADLINE}" "start real stack"' in gate
    assert '"${COMPOSE[@]}" up --detach --wait' not in gate
    assert "unset DATABASE_URL_FILE" in gate
    assert 'run_with_deadline 900 "real-stack MySQL full migration cycle"' in gate
    assert 'run_with_deadline 60 "real-stack migration MySQL security check"' in gate
    assert 'run_with_deadline 60 "real-stack runtime MySQL security check"' in gate
    assert "mysql --protocol=socket --user=root --connect-timeout=5" in gate

    outer_health = gate.index("assert_compose_health")
    outer_bootstrap = gate.index("run_minio_bootstrap", outer_health)
    assert outer_bootstrap < gate.index('echo "Running UI/BFF E2E')

    standalone_health = ui_gate.index('wait_for_real_stack "${DB_URL}"')
    standalone_bootstrap = ui_gate.index("run_minio_bootstrap", standalone_health)
    standalone_readyz = ui_gate.index('assert_strict_readyz "${BFF_URL}"')
    assert standalone_health < standalone_bootstrap < standalone_readyz
    assert "return 200 <= response.status < 300" in ui_gate


def test_deadline_runner_terminates_a_command_that_exceeds_its_budget() -> None:
    runner = ROOT / "scripts" / "run_with_deadline.py"

    result = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--timeout-seconds",
            "0.05",
            "--label",
            "deadline regression fixture",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(5)",
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 124
    assert "deadline regression fixture exceeded 0.05s deadline" in result.stderr
