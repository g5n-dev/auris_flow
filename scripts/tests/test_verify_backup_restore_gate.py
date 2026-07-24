from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_UNDER_TEST = ROOT / "scripts" / "verify_backup_restore_gate.py"
SOURCE_COMMIT = "1" * 40
SHA256 = "a" * 64


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_backup_restore_gate", SCRIPT_UNDER_TEST
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_evidence() -> dict[str, object]:
    return {
        "schema_version": "auris.backup-restore-gate.v1",
        "status": "ok",
        "source_commit": SOURCE_COMMIT,
        "execution_environment": "native-linux-compose",
        "producer": "production/scripts/verify-backup.sh",
        "release": {
            "release_tag": "v1.0.0-rc.1",
            "signed_release_metadata_verified": True,
            "release_metadata_sha256": SHA256,
            "compose_sha256": SHA256,
            "image_lock_sha256": SHA256,
        },
        "host": {
            "platform": "linux",
            "native_linux": True,
            "docker_context": "default",
            "docker_ostype": "linux",
            "docker_operating_system": "Ubuntu 24.04.2 LTS",
            "rootless": False,
        },
        "backup": {
            "backup_id": "auris-flow-20260724T080000Z-111111111111",
            "manifest_sha256": SHA256,
            "manifest_signature_verified": True,
            "storage_boundary": "ephemeral-ci-drill",
            "off_host_retained": False,
            "source_project": "auris-flow",
            "verification_started_at": "2026-07-24T08:00:00Z",
            "verification_completed_at": "2026-07-24T08:02:00Z",
            "verification_duration_seconds": 120,
            "authority_counts": {
                "mysql": {
                    "business_rows_total": 1,
                    "tables_total": 3,
                    "rows_total": 8,
                },
                "minio": {
                    "object_keys": 1,
                    "versions": 2,
                    "content_bytes": 4096,
                },
                "qdrant": {"collections": 1, "points_total": 2},
            },
        },
        "restore": {
            "project_name": "auris-flow-restore-drill-a1b2c3d4e5f6",
            "network_subnet": "172.31.49.0/24",
            "edge_internal_ip": "172.31.49.10",
            "started_at": "2026-07-24T08:02:01Z",
            "completed_at": "2026-07-24T08:05:01Z",
            "duration_seconds": 180,
            "qdrant_mode": "snapshot",
            "empty_target_verified": {
                "mysql": True,
                "minio": True,
                "qdrant": True,
            },
            "consistency": {
                "mysql_counts_match": True,
                "minio_versions_and_sha256_match": True,
                "qdrant_fingerprints_match": True,
            },
        },
        "cleanup": {
            "started_at": "2026-07-24T08:05:02Z",
            "completed_at": "2026-07-24T08:05:12Z",
            "duration_seconds": 10,
            "containers_removed": True,
            "volumes_removed": True,
            "networks_removed": True,
        },
        "tool_bindings": {
            relative: _sha256(ROOT / relative)
            for relative in (
                "production/backup/backup_restore_evidence.py",
                "production/backup/manifest.py",
                "production/backup/restore_network_allocator.py",
                "production/scripts/backup.sh",
                "production/scripts/restore.sh",
                "production/scripts/verify-backup.sh",
                "scripts/release_bundle.py",
                "scripts/run_with_deadline.py",
                "scripts/verify_backup_restore_gate.py",
            )
        },
        "verified_at": "2026-07-24T08:05:13Z",
    }


def test_accepts_complete_commit_bound_native_linux_restore_drill() -> None:
    module = _load_module()

    assert (
        module.validate_evidence(
            _valid_evidence(),
            root=ROOT,
            expected_commit=SOURCE_COMMIT,
        )
        == []
    )


def test_rejects_wrong_commit_and_non_native_or_desktop_docker() -> None:
    module = _load_module()
    evidence = _valid_evidence()
    evidence["source_commit"] = "2" * 40
    release = evidence["release"]
    assert isinstance(release, dict)
    release["release_tag"] = "v1.0.0"
    host = evidence["host"]
    assert isinstance(host, dict)
    host["native_linux"] = False
    host["docker_operating_system"] = "Docker Desktop"

    errors = module.validate_evidence(
        evidence,
        root=ROOT,
        expected_commit=SOURCE_COMMIT,
        expected_release_tag="v1.0.0-rc.1",
    )

    assert any("source_commit" in error for error in errors)
    assert any("expected release" in error for error in errors)
    assert any("native Linux" in error for error in errors)
    assert any("Docker Desktop" in error for error in errors)


def test_rejects_empty_authorities_unverified_consistency_and_failed_cleanup() -> None:
    module = _load_module()
    evidence = _valid_evidence()
    backup = evidence["backup"]
    restore = evidence["restore"]
    cleanup = evidence["cleanup"]
    assert isinstance(backup, dict)
    assert isinstance(restore, dict)
    assert isinstance(cleanup, dict)
    authority_counts = backup["authority_counts"]
    consistency = restore["consistency"]
    assert isinstance(authority_counts, dict)
    assert isinstance(consistency, dict)
    mysql = authority_counts["mysql"]
    assert isinstance(mysql, dict)
    mysql["rows_total"] = 0
    mysql["business_rows_total"] = 0
    consistency["qdrant_fingerprints_match"] = False
    cleanup["volumes_removed"] = False

    errors = module.validate_evidence(
        evidence,
        root=ROOT,
        expected_commit=SOURCE_COMMIT,
    )

    assert any("non-empty MySQL" in error for error in errors)
    assert any("Qdrant" in error for error in errors)
    assert any("volumes" in error for error in errors)


def test_rejects_non_random_restore_project_and_invalid_time_order() -> None:
    module = _load_module()
    evidence = _valid_evidence()
    restore = evidence["restore"]
    assert isinstance(restore, dict)
    restore["project_name"] = "auris-flow"
    restore["started_at"] = "2026-07-24T07:59:59Z"

    errors = module.validate_evidence(
        evidence,
        root=ROOT,
        expected_commit=SOURCE_COMMIT,
    )

    assert any("random isolated Compose project" in error for error in errors)
    assert any("time ordering" in error for error in errors)


def test_rejects_tampered_tool_binding_and_unknown_fields() -> None:
    module = _load_module()
    evidence = _valid_evidence()
    bindings = evidence["tool_bindings"]
    assert isinstance(bindings, dict)
    bindings["production/scripts/restore.sh"] = "b" * 64
    evidence["unexpected"] = True

    errors = module.validate_evidence(
        evidence,
        root=ROOT,
        expected_commit=SOURCE_COMMIT,
    )

    assert any("fields are invalid" in error for error in errors)
    assert any("restore.sh" in error for error in errors)


def test_rejects_absolute_paths_and_secret_shaped_fields() -> None:
    module = _load_module()
    evidence = _valid_evidence()
    poisoned = copy.deepcopy(evidence)
    release = poisoned["release"]
    assert isinstance(release, dict)
    release["operator_token"] = "secret-value"
    poisoned["producer"] = "/" + "home/operator/auris/verify-backup.sh"

    errors = module.validate_evidence(
        poisoned,
        root=ROOT,
        expected_commit=SOURCE_COMMIT,
    )

    assert any("sensitive field" in error for error in errors)
    assert any("absolute path" in error for error in errors)

    windows_path = _valid_evidence()
    windows_path["producer"] = "C:" + r"\workspace\auris\verify-backup.sh"
    errors = module.validate_evidence(
        windows_path,
        root=ROOT,
        expected_commit=SOURCE_COMMIT,
    )
    assert any("absolute path" in error for error in errors)


def _write_release_bundle(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    bundle = tmp_path / "deployment"
    production = bundle / "production"
    production.mkdir(parents=True)
    compose = b'{"services":{"bff":{"image":"example.invalid/bff@sha256:' + (
        b"b" * 64
    ) + b'"}}}\n'
    image_lock = (
        b'{"images":{"bff":"example.invalid/bff@sha256:'
        + (b"b" * 64)
        + b'"},"schema_version":"auris.release-image-lock.v1"}\n'
    )
    (production / "compose.yaml").write_bytes(compose)
    (production / "images.lock.json").write_bytes(image_lock)
    metadata = {
        "schema_version": "auris.release-deployment-metadata.v3",
        "release_tag": "v1.0.0-rc.1",
        "source_commit": SOURCE_COMMIT,
        "compose": {
            "path": "production/compose.yaml",
            "sha256": hashlib.sha256(compose).hexdigest(),
        },
        "image_lock": {
            "path": "production/images.lock.json",
            "sha256": hashlib.sha256(image_lock).hexdigest(),
        },
    }
    metadata_path = production / "release-metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return bundle, metadata


def test_formal_release_bindings_match_actual_deployment_files(tmp_path: Path) -> None:
    module = _load_module()
    evidence = _valid_evidence()
    release = evidence["release"]
    assert isinstance(release, dict)
    bundle, metadata = _write_release_bundle(tmp_path)
    release["release_metadata_sha256"] = hashlib.sha256(
        (bundle / "production/release-metadata.json").read_bytes()
    ).hexdigest()
    compose = metadata["compose"]
    image_lock = metadata["image_lock"]
    assert isinstance(compose, dict)
    assert isinstance(image_lock, dict)
    release["compose_sha256"] = compose["sha256"]
    release["image_lock_sha256"] = image_lock["sha256"]

    assert (
        module.validate_release_bindings(
            evidence,
            release_bundle_root=bundle,
            expected_commit=SOURCE_COMMIT,
            expected_release_tag="v1.0.0-rc.1",
        )
        == []
    )

    (bundle / "production/compose.yaml").write_text(
        '{"services":{}}\n',
        encoding="utf-8",
    )
    errors = module.validate_release_bindings(
        evidence,
        release_bundle_root=bundle,
        expected_commit=SOURCE_COMMIT,
        expected_release_tag="v1.0.0-rc.1",
    )
    assert any("Compose" in error and "digest" in error for error in errors)


def test_sigstore_attestation_uses_exact_tag_bound_workflow_identity(
    tmp_path: Path,
) -> None:
    module = _load_module()
    evidence = tmp_path / "backup-restore-gate.json"
    signature = tmp_path / "backup-restore-gate.sigstore.json"
    evidence.write_text("{}\n", encoding="utf-8")
    signature.write_text("{}\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def accept(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    module.verify_sigstore_attestation(
        evidence_path=evidence,
        signature_bundle=signature,
        release_tag="v1.0.0-rc.1",
        run=accept,
    )

    assert len(calls) == 1
    command = calls[0]
    assert command[0:2] == ("cosign", "verify-blob")
    assert (
        "https://github.com/g5n-dev/auris_flow/.github/workflows/"
        "release-images.yml@refs/tags/v1.0.0-rc.1"
    ) in command
    assert "https://token.actions.githubusercontent.com" in command

    def reject(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "invalid signature")

    with pytest.raises(module.FormalEvidenceError, match="Sigstore"):
        module.verify_sigstore_attestation(
            evidence_path=evidence,
            signature_bundle=signature,
            release_tag="v1.0.0-rc.1",
            run=reject,
        )


def test_signed_bundle_verification_never_executes_external_bundle_code(
    tmp_path: Path,
) -> None:
    module = _load_module()
    bundle, _metadata = _write_release_bundle(tmp_path)
    external_scripts = bundle / "scripts"
    external_scripts.mkdir()
    marker = tmp_path / "external-verifier-executed"
    (external_scripts / "release_bundle.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []

    def accept(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    module.verify_signed_release_bundle(bundle, run=accept)

    assert len(calls) == 1
    assert Path(calls[0][1]) == ROOT / "scripts" / "release_bundle.py"
    assert Path(calls[0][1]) != external_scripts / "release_bundle.py"
    assert not marker.exists()


def test_cli_rejects_symlinked_evidence_instead_of_resolving_it(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "real.json"
    artifact.write_text(json.dumps(_valid_evidence()), encoding="utf-8")
    link = tmp_path / "evidence.json"
    link.symlink_to(artifact)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_UNDER_TEST),
            "--artifact",
            str(link),
            "--expected-commit",
            SOURCE_COMMIT,
            "--root",
            str(ROOT),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert "regular file" in result.stderr


def test_formal_cli_never_accepts_an_unsigned_schema_valid_json(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "evidence.json"
    artifact.write_text(json.dumps(_valid_evidence()), encoding="utf-8")
    bundle, _metadata = _write_release_bundle(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_UNDER_TEST),
            "--artifact",
            str(artifact),
            "--expected-commit",
            SOURCE_COMMIT,
            "--expected-release-tag",
            "v1.0.0-rc.1",
            "--release-bundle-root",
            str(bundle),
            "--formal",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert "--signature-bundle" in result.stderr
