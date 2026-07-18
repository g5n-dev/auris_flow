from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "production" / "scripts" / "init-secrets.sh"
EXPECTED_SECRET_FILES = {
    "audio_playback_grant_secret",
    "completion_receipt_key_bindings",
    "dagster_database_url",
    "dagster_db_password",
    "embedding_api_key",
    "experiment_assignment_secret",
    "external_callback_key_bindings",
    "grafana_admin_password",
    "grafana_admin_user",
    "keycloak_admin_password",
    "keycloak_admin_user",
    "keycloak_bootstrap_operator_password",
    "keycloak_db_password",
    "migration_database_url",
    "mysql_migration_password",
    "mysql_root_password",
    "mysql_runtime_password",
    "object_storage_access_key",
    "object_storage_secret_key",
    "qdrant_api_key",
    "redis_url",
    "redis_users.acl",
    "runtime_database_url",
}


def _run(secrets_dir: Path, metrics_dir: Path) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "AURIS_SECRETS_DIR": str(secrets_dir),
        "AURIS_RUNTIME_METRICS_DIR": str(metrics_dir),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _digests(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
    }


def test_secret_initialization_is_private_complete_and_idempotent(
    tmp_path: Path,
) -> None:
    secrets_dir = tmp_path / "secrets"
    metrics_dir = tmp_path / "runtime-metrics"

    first = _run(secrets_dir, metrics_dir)
    assert first.returncode == 0, first.stderr
    assert {path.name for path in secrets_dir.iterdir()} == EXPECTED_SECRET_FILES
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(metrics_dir.stat().st_mode) == 0o755
    for path in secrets_dir.iterdir():
        assert path.is_file() and not path.is_symlink()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    initial = _digests(secrets_dir)
    operator_password = (
        (secrets_dir / "keycloak_bootstrap_operator_password")
        .read_text(encoding="utf-8")
        .strip()
    )
    assert len(operator_password) == 64
    assert all(character in "0123456789abcdef" for character in operator_password)
    assert operator_password not in first.stdout
    assert operator_password not in first.stderr

    second = _run(secrets_dir, metrics_dir)
    assert second.returncode == 0, second.stderr
    assert _digests(secrets_dir) == initial


def test_secret_initialization_refuses_symlink_metrics_directory(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    metrics_link = tmp_path / "runtime-metrics"
    metrics_link.symlink_to(target, target_is_directory=True)

    result = _run(tmp_path / "secrets", metrics_link)

    assert result.returncode == 2
    assert "refusing symlink runtime metrics directory" in result.stderr


def test_secret_initialization_script_is_executable() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
