from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "production" / "scripts" / "recovery-linkage.sh"


def test_recovery_linkage_script_has_three_independent_live_reads() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "write-mysql-export-sql" in source
    assert "mysql --protocol=tcp" in source
    assert "capture-json-stdin" in source
    assert "minio_mc_with_deadline" in source
    assert 'stat --json "${MINIO_OBJECT}"' in source
    assert "capture-object-stdin" in source
    assert "qdrant-export" in source
    assert "build-proof" in source
    assert "scripts/run_with_deadline.py" in source
    assert 'cat "${MINIO_OBJECT}" >' not in source
    assert source.index("write-mysql-export-sql") < source.index("build-proof")
    assert source.index('stat --json "${MINIO_OBJECT}"') < source.index("build-proof")
    assert source.index("capture-object-stdin") < source.index("build-proof")
    assert source.index("qdrant-export") < source.index("build-proof")


def test_recovery_linkage_seed_and_rebuild_are_authority_driven() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "write-fixture" in source
    assert "write-mysql-seed-sql" in source
    assert "qdrant-seed" in source
    assert "backup_restore_gate_seed.py" not in source
    assert "http://169.254.169.254" not in source
    assert "curl " not in source
    assert "urllib" not in source
    assert "seed)" in source
    assert "capture)" in source
    assert "rebuild)" in source


def test_recovery_linkage_script_is_valid_bash() -> None:
    completed = subprocess.run(
        ["bash", "-n", SCRIPT],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_recovery_linkage_cleanup_normalizes_trailing_tmpdir(
    tmp_path: Path,
) -> None:
    temporary_parent = tmp_path / "temporary"
    temporary_parent.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    fake_docker.chmod(0o755)
    env_file = tmp_path / "production.env"
    env_file.write_text("APP_ENV=release\n", encoding="utf-8")
    proof_output = tmp_path / "proof.json"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "PYTHON": sys.executable,
        "TMPDIR": f"{temporary_parent}{os.sep}",
    }
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "COMPOSE_PROJECT_NAME"):
        environment.pop(name, None)

    completed = subprocess.run(
        [
            "bash",
            SCRIPT,
            "capture",
            "--project-name",
            "auris-flow-test",
            "--env-file",
            env_file,
            "--proof-output",
            proof_output,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode != 0
    assert not proof_output.exists()
    assert list(temporary_parent.glob("auris-flow-recovery-linkage.*")) == []
