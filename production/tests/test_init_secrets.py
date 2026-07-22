from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "production" / "scripts" / "init-secrets.sh"
EXPECTED_SECRET_FILES = {
    "audio_inference_api_token",
    "audio_playback_grant_secret",
    "backup_manifest_signing_private_key.pem",
    "backup_manifest_signing_public_key.pem",
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
    "restore_attestation_signing_private_key.pem",
    "restore_attestation_signing_public_key.pem",
    "runtime_database_url",
}

PRIVATE_KEY_FILES = {
    "backup_manifest_signing_private_key.pem",
    "restore_attestation_signing_private_key.pem",
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
        # Docker Compose file-backed secrets retain host modes on native Linux.
        # The parent stays 0700; leaf files must be readable by remapped
        # non-root container UIDs without exposing the directory to host users.
        expected_mode = 0o400 if path.name in PRIVATE_KEY_FILES else 0o444
        assert stat.S_IMODE(path.stat().st_mode) == expected_mode
    public_keys: list[bytes] = []
    for prefix in ("backup_manifest", "restore_attestation"):
        private_key = secrets_dir / f"{prefix}_signing_private_key.pem"
        public_key = secrets_dir / f"{prefix}_signing_public_key.pem"
        derived_public = subprocess.run(
            ["openssl", "pkey", "-in", private_key, "-pubout"],
            check=True,
            capture_output=True,
        ).stdout
        assert derived_public == public_key.read_bytes()
        public_keys.append(derived_public)
    assert public_keys[0] != public_keys[1]
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


def test_secret_initialization_refuses_symlink_secrets_directory(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-secrets"
    outside.mkdir()
    secrets_link = tmp_path / "secrets"
    secrets_link.symlink_to(outside, target_is_directory=True)

    result = _run(secrets_link, tmp_path / "runtime-metrics")

    assert result.returncode == 2
    assert "refusing symlink secrets directory" in result.stderr
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    ("prefix", "error_label"),
    [
        ("backup_manifest", "backup manifest signing key pair"),
        ("restore_attestation", "restore attestation signing key pair"),
    ],
)
def test_secret_initialization_refuses_partial_or_symlinked_signing_keys(
    tmp_path: Path, prefix: str, error_label: str
) -> None:
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / f"{prefix}_signing_private_key.pem").write_text(
        "not-a-key\n", encoding="ascii"
    )
    partial_result = _run(partial, tmp_path / "partial-metrics")
    assert partial_result.returncode == 2
    assert error_label in partial_result.stderr

    symlinked = tmp_path / "symlinked"
    symlinked.mkdir()
    outside = tmp_path / "outside-key"
    outside.write_text("not-a-key\n", encoding="ascii")
    (symlinked / f"{prefix}_signing_private_key.pem").symlink_to(outside)
    (symlinked / f"{prefix}_signing_public_key.pem").write_text(
        "not-a-key\n", encoding="ascii"
    )
    symlink_result = _run(symlinked, tmp_path / "symlink-metrics")
    assert symlink_result.returncode == 2
    assert f"unsafe {error_label.removesuffix(' pair')}" in symlink_result.stderr


def test_secret_initialization_refuses_reusing_one_key_for_both_roles(
    tmp_path: Path,
) -> None:
    secrets_dir = tmp_path / "secrets"
    initialized = _run(secrets_dir, tmp_path / "metrics")
    assert initialized.returncode == 0, initialized.stderr
    manifest_private = secrets_dir / "backup_manifest_signing_private_key.pem"
    manifest_public = secrets_dir / "backup_manifest_signing_public_key.pem"
    attestation_private = secrets_dir / "restore_attestation_signing_private_key.pem"
    attestation_public = secrets_dir / "restore_attestation_signing_public_key.pem"
    attestation_private.chmod(0o600)
    attestation_public.chmod(0o600)
    attestation_private.write_bytes(manifest_private.read_bytes())
    attestation_public.write_bytes(manifest_public.read_bytes())
    attestation_private.chmod(0o400)
    attestation_public.chmod(0o444)

    rejected = _run(secrets_dir, tmp_path / "metrics")

    assert rejected.returncode == 2
    assert "must use distinct Ed25519 keys" in rejected.stderr


def test_secret_initialization_script_is_executable() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
