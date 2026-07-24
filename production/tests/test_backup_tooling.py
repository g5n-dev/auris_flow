from __future__ import annotations

import base64
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest
import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKUP_TOOLS = REPOSITORY_ROOT / "production" / "backup"
SCRIPTS = REPOSITORY_ROOT / "production" / "scripts"
COMPOSE = REPOSITORY_ROOT / "production" / "compose.yaml"
MINIO_CLIENT = REPOSITORY_ROOT / "production" / "minio" / "client.sh"
MANIFEST = BACKUP_TOOLS / "manifest.py"
MINIO = BACKUP_TOOLS / "minio_versions.py"
QDRANT = BACKUP_TOOLS / "qdrant_snapshots.py"
MYSQL_DUMP = BACKUP_TOOLS / "mysql_dump.py"
RESTORE_STATE = BACKUP_TOOLS / "restore_state.py"
RELEASE_BUNDLE = REPOSITORY_ROOT / "scripts" / "release_bundle.py"
SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
RELEASE_TAG = "v1.0.0"
TEST_IMAGE = "ghcr.io/auris-flow/auris-flow-bff:v1.0.0@sha256:" + ("a" * 64)
AUTHORITY_IMAGES = {
    service: f"registry.example.com/auris/{service}:v1.0.0@sha256:{character * 64}"
    for service, character in (
        ("mysql", "1"),
        ("minio", "2"),
        ("qdrant", "3"),
        ("redis", "4"),
    )
}


def load_release_bundle() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_bundle", RELEASE_BUNDLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_release_metadata() -> dict[str, object]:
    return {
        "schema_version": "auris.release-deployment-metadata.v3",
        "release_tag": RELEASE_TAG,
        "source_commit": SOURCE_COMMIT,
        "compose": {"path": "production/compose.yaml", "sha256": "b" * 64},
        "image_lock": {
            "path": "production/images.lock.json",
            "sha256": "c" * 64,
        },
        "restore_policy": {
            "path": "production/restore-compatibility.json",
            "sha256": "d" * 64,
        },
        "images": AUTHORITY_IMAGES,
        "members": [
            {
                "path": "README.md",
                "sha256": "e" * 64,
                "type": "regular-file",
                "mode": "0644",
            }
        ],
    }


def run_tool(
    *arguments: object,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, *(str(argument) for argument in arguments)],
        capture_output=True,
        check=False,
        text=True,
        env=env,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr)
    return result


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def make_restore_finalize_evidence(tmp_path: Path) -> tuple[Path, Path, Path]:
    qdrant = tmp_path / "qdrant-finalize.json"
    running_images = tmp_path / "running-images-finalize.json"
    readyz = tmp_path / "readyz-finalize.json"
    write_json(
        qdrant,
        {
            "status": "verified",
            "collections": {"knowledge_chunks": 3},
            "verification": "full-fingerprint-and-scoped-probe",
        },
    )
    write_json(
        running_images,
        {
            "schema_version": "auris.release-running-images.v1",
            "release_tag": RELEASE_TAG,
            "source_commit": SOURCE_COMMIT,
            "verification_scope": "all-running-release-services",
            "images": AUTHORITY_IMAGES,
        },
    )
    required_checks = [
        "auth",
        "dagster",
        "database",
        "object_storage",
        "observability",
        "qdrant",
        "redis",
    ]
    write_json(
        readyz,
        {
            "status": "ok",
            "data": {
                "status": "success",
                "checks": {name: "ok" for name in required_checks},
                "required_checks": required_checks,
                "missing_required": {},
            },
        },
    )
    for path in (qdrant, running_images, readyz):
        path.chmod(0o600)
    return qdrant, running_images, readyz


def make_minio_plan(path: Path) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "auris-flow.minio-version-plan/v2",
        "bucket": "auris-flow",
        "content_hash_algorithm": "sha256",
        "ordering": "last_modified_ascending_then_reverse_mc_source_order",
        "summary": {
            "object_keys": 0,
            "versions": 0,
            "delete_markers": 0,
            "content_bytes": 0,
        },
        "versions": [],
    }
    write_json(path, document)
    return document


def make_backup_root(tmp_path: Path) -> Path:
    root = tmp_path / "backup"
    (root / "mysql").mkdir(parents=True)
    (root / "minio").mkdir()
    (root / "metadata").mkdir()
    (root / "qdrant" / "snapshots").mkdir(parents=True)
    with gzip.open(
        root / "mysql" / "all-databases.sql.gz", "wt", encoding="utf-8"
    ) as handle:
        handle.write("-- MySQL dump\nSTART TRANSACTION;\n")
        for database in ("auris_flow", "keycloak", "dagster"):
            handle.write(f"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `{database}`;\n")
            handle.write(f"USE `{database}`;\n")
    (root / "mysql" / "table-counts.tsv").write_text(
        "auris_flow.audit_logs\t3\n", encoding="utf-8"
    )
    make_minio_plan(root / "minio" / "versions.json")
    write_json(
        root / "metadata" / "counts.json",
        {
            "mysql": {"tables": {"auris_flow.audit_logs": 3}, "rows_total": 3},
            "minio": {"object_keys": 0, "versions": 0, "delete_markers": 0},
            "qdrant": {"included": False, "collections": {}, "points_total": 0},
            "redis": {"included": False, "authoritative": False},
        },
    )
    write_json(root / "metadata" / "tool-versions.json", {"commands": {"mysql": "8.4"}})
    write_json(root / "metadata" / "release-metadata.json", make_release_metadata())
    write_json(
        root / "metadata" / "release-metadata.sigstore.json",
        {"fixture": "sigstore-bundle"},
    )
    write_json(
        root / "metadata" / "running-images.json",
        {
            "schema_version": "auris.release-running-images.v1",
            "release_tag": RELEASE_TAG,
            "source_commit": SOURCE_COMMIT,
            "verification_scope": "all-running-release-services",
            "images": AUTHORITY_IMAGES,
        },
    )
    write_json(
        root / "qdrant" / "snapshots.json",
        {
            "schema_version": "auris-flow.qdrant-snapshots/v2",
            "qdrant_version": "1.14.1",
            "authority": "derived-rebuildable-from-mysql-and-minio",
            "collections": [],
            "aliases": [],
        },
    )
    return root


def make_ed25519_keys(
    root: Path, *, trust_directory: str, key_name: str
) -> tuple[Path, Path]:
    trust_root = root.parent / trust_directory
    private_key = trust_root / f"{key_name}-private.pem"
    public_key = trust_root / f"{key_name}-public.pem"
    if private_key.exists():
        return private_key, public_key
    trust_root.mkdir(mode=0o700)
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "ED25519", "-out", private_key],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "openssl",
            "pkey",
            "-in",
            private_key,
            "-pubout",
            "-out",
            public_key,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    private_key.chmod(0o600)
    public_key.chmod(0o644)
    return private_key, public_key


def make_manifest_signing_keys(root: Path) -> tuple[Path, Path]:
    return make_ed25519_keys(
        root,
        trust_directory="backup-manifest-trust",
        key_name="manifest-signing",
    )


def make_restore_attestation_keys(root: Path) -> tuple[Path, Path]:
    return make_ed25519_keys(
        root,
        trust_directory="restore-attestation-trust",
        key_name="restore-attestation",
    )


def key_id(public_key: Path) -> str:
    return str(
        json.loads(run_tool(MANIFEST, "key-id", "--public-key", public_key).stdout)[
            "key_id"
        ]
    )


def create_unsigned_manifest(
    root: Path,
    *,
    restore_attestation_public_key: Path | None = None,
    storage_boundary: str = "encrypted-external",
) -> None:
    if restore_attestation_public_key is None:
        _private_key, restore_attestation_public_key = make_restore_attestation_keys(
            root
        )
    run_tool(
        MANIFEST,
        "create",
        "--root",
        root,
        "--backup-id",
        "auris-flow-20260718T120000Z-0123456789ab",
        "--created-at-utc",
        "2026-07-18T12:00:00Z",
        "--git-commit",
        SOURCE_COMMIT,
        "--release-version",
        RELEASE_TAG,
        "--counts",
        root / "metadata" / "counts.json",
        "--tool-versions",
        root / "metadata" / "tool-versions.json",
        "--release-metadata",
        root / "metadata" / "release-metadata.json",
        "--running-images",
        root / "metadata" / "running-images.json",
        "--storage-boundary",
        storage_boundary,
        "--restore-attestation-public-key",
        restore_attestation_public_key,
    )


def create_manifest(root: Path) -> tuple[Path, Path]:
    private_key, public_key = make_manifest_signing_keys(root)
    create_unsigned_manifest(root)
    run_tool(
        MANIFEST,
        "sign",
        "--root",
        root,
        "--private-key",
        private_key,
        "--public-key",
        public_key,
    )
    return private_key, public_key


def test_manifest_records_truthful_storage_boundary_mode(tmp_path: Path) -> None:
    external_root = make_backup_root(tmp_path / "external")
    create_unsigned_manifest(external_root)
    external = json.loads(
        (external_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert external["storage_boundary"] == {
        "contains_sensitive_data": True,
        "mode": "encrypted-external",
        "operator_assertion": "encrypted-at-rest-and-copied-off-host",
        "repository_never_contains_backup_payloads": True,
    }

    drill_root = make_backup_root(tmp_path / "drill")
    create_unsigned_manifest(
        drill_root,
        storage_boundary="ephemeral-ci-drill",
    )
    drill = json.loads((drill_root / "manifest.json").read_text(encoding="utf-8"))
    assert drill["storage_boundary"] == {
        "contains_sensitive_data": True,
        "mode": "ephemeral-ci-drill",
        "operator_assertion": "ephemeral-runner-recovery-drill-not-retained",
        "repository_never_contains_backup_payloads": True,
    }


def verify_manifest(
    root: Path, *, public_key: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    if public_key is None:
        _private_key, public_key = make_manifest_signing_keys(root)
    return run_tool(
        MANIFEST,
        "verify",
        "--root",
        root,
        "--public-key",
        public_key,
        check=check,
    )


def test_manifest_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    root = make_backup_root(tmp_path)
    create_manifest(root)
    verified = verify_manifest(root)
    summary = json.loads(verified.stdout)
    _attestation_private_key, attestation_public_key = make_restore_attestation_keys(
        root
    )
    attestation_key_id = key_id(attestation_public_key)
    assert summary["status"] == "verified"
    assert summary["restore_attestation_key_id"] == attestation_key_id
    document = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert document["restore_attestation"] == {
        "algorithm": "ed25519",
        "key_id": attestation_key_id,
        "purpose": "auris-flow-restore-completion",
        "schema_version": "auris-flow.restore-attestation-delegation/v1",
    }
    signature = json.loads(
        (root / "manifest.signature.json").read_text(encoding="utf-8")
    )
    assert signature["schema_version"] == "auris-flow.backup-manifest-signature/v1"
    assert signature["algorithm"] == "ed25519"
    assert signature["key_id"] == summary["signing_key_id"]
    assert signature["backup_id"] == summary["backup_id"]
    assert signature["created_at_utc"] == summary["created_at_utc"]
    assert signature["source_commit"] == summary["git_commit"]
    assert signature["manifest_sha256"] == summary["manifest_sha256"]
    assert signature["key_id"] != summary["restore_attestation_key_id"]

    (root / "mysql" / "table-counts.tsv").write_text(
        "auris_flow.audit_logs\t4\n", encoding="utf-8"
    )
    rejected = verify_manifest(root, check=False)
    assert rejected.returncode == 2
    assert "checksum mismatch" in rejected.stderr


def test_manifest_signature_rejects_restore_attestation_delegation_tampering(
    tmp_path: Path,
) -> None:
    root = make_backup_root(tmp_path)
    _private_key, manifest_public_key = create_manifest(root)
    manifest_path = root / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["restore_attestation"]["key_id"] = "ed25519-sha256:" + ("f" * 64)
    canonical = (
        json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode()
    manifest_path.write_bytes(canonical)
    (root / "manifest.sha256").write_text(
        f"{hashlib.sha256(canonical).hexdigest()}  manifest.json\n",
        encoding="ascii",
    )

    rejected = verify_manifest(root, public_key=manifest_public_key, check=False)

    assert rejected.returncode == 2
    assert "signature" in rejected.stderr.casefold()


def test_manifest_refuses_same_key_for_manifest_and_restore_attestation(
    tmp_path: Path,
) -> None:
    root = make_backup_root(tmp_path)
    manifest_private_key, manifest_public_key = make_manifest_signing_keys(root)
    create_unsigned_manifest(root, restore_attestation_public_key=manifest_public_key)

    rejected = run_tool(
        MANIFEST,
        "sign",
        "--root",
        root,
        "--private-key",
        manifest_private_key,
        "--public-key",
        manifest_public_key,
        check=False,
    )

    assert rejected.returncode == 2
    assert "distinct" in rejected.stderr.casefold()
    assert not (root / "manifest.signature.json").exists()


def test_manifest_verifier_rejects_a_valid_signature_with_reused_role_key(
    tmp_path: Path,
) -> None:
    root = make_backup_root(tmp_path)
    private_key, public_key = make_manifest_signing_keys(root)
    create_unsigned_manifest(root, restore_attestation_public_key=public_key)
    document = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    statement = {
        "algorithm": "ed25519",
        "backup_id": document["backup_id"],
        "created_at_utc": document["created_at_utc"],
        "key_id": key_id(public_key),
        "manifest_sha256": hashlib.sha256(
            (root / "manifest.json").read_bytes()
        ).hexdigest(),
        "purpose": "auris-flow-production-backup-manifest",
        "schema_version": "auris-flow.backup-manifest-signature/v1",
        "source_commit": document["source"]["git_commit"],
    }
    statement_path = tmp_path / "statement.json"
    signature_path = tmp_path / "statement.sig"
    statement_path.write_text(
        json.dumps(statement, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-sign",
            "-rawin",
            "-inkey",
            str(private_key),
            "-in",
            str(statement_path),
            "-out",
            str(signature_path),
        ],
        check=True,
        capture_output=True,
    )
    write_json(
        root / "manifest.signature.json",
        {
            **statement,
            "signature_base64": base64.b64encode(signature_path.read_bytes()).decode(
                "ascii"
            ),
        },
    )

    rejected = verify_manifest(root, public_key=public_key, check=False)

    assert rejected.returncode == 2
    assert "distinct" in rejected.stderr.casefold()


def test_manifest_refuses_embedded_restore_attestation_trust_key(
    tmp_path: Path,
) -> None:
    root = make_backup_root(tmp_path)
    _private_key, external_public_key = make_restore_attestation_keys(root)
    embedded_public_key = root / "metadata" / "restore-attestation-public.pem"
    shutil.copyfile(external_public_key, embedded_public_key)

    rejected = run_tool(
        MANIFEST,
        "create",
        "--root",
        root,
        "--backup-id",
        "auris-flow-20260718T120000Z-0123456789ab",
        "--created-at-utc",
        "2026-07-18T12:00:00Z",
        "--git-commit",
        SOURCE_COMMIT,
        "--release-version",
        RELEASE_TAG,
        "--counts",
        root / "metadata" / "counts.json",
        "--tool-versions",
        root / "metadata" / "tool-versions.json",
        "--release-metadata",
        root / "metadata" / "release-metadata.json",
        "--running-images",
        root / "metadata" / "running-images.json",
        "--storage-boundary",
        "encrypted-external",
        "--restore-attestation-public-key",
        embedded_public_key,
        check=False,
    )

    assert rejected.returncode == 2
    assert "external" in rejected.stderr.casefold()
    assert not (root / "manifest.json").exists()


def test_external_manifest_signature_rejects_fully_rehashed_backup_tampering(
    tmp_path: Path,
) -> None:
    root = make_backup_root(tmp_path)
    _private_key, public_key = create_manifest(root)
    manifest_path = root / "manifest.json"
    checksum_path = root / "manifest.sha256"
    signature_path = root / "manifest.signature.json"
    assert signature_path.is_file()

    # Model an attacker who controls the backup store: alter an artifact, then
    # coherently rewrite every repository-local hash. The deployment trust key
    # remains external, so the old signature must still make recovery fail.
    artifact_path = root / "mysql" / "table-counts.tsv"
    artifact_path.write_text("auris_flow.audit_logs\t999\n", encoding="utf-8")
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    for artifact in document["artifacts"]:
        if artifact["path"] == "mysql/table-counts.tsv":
            artifact["size_bytes"] = artifact_path.stat().st_size
            artifact["sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    canonical = (
        json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode()
    manifest_path.write_bytes(canonical)
    checksum_path.write_text(
        f"{hashlib.sha256(canonical).hexdigest()}  manifest.json\n",
        encoding="ascii",
    )

    rejected = run_tool(
        MANIFEST,
        "verify",
        "--root",
        root,
        "--public-key",
        public_key,
        check=False,
    )

    assert rejected.returncode == 2
    assert "signature" in rejected.stderr.casefold()


def test_manifest_signature_binds_external_ed25519_key_identity(tmp_path: Path) -> None:
    root = make_backup_root(tmp_path)
    _private_key, trusted_public_key = create_manifest(root)
    attacker_root = tmp_path / "attacker"
    attacker_root.mkdir()
    _attacker_private_key, attacker_public_key = make_manifest_signing_keys(
        attacker_root / "backup"
    )

    rejected = run_tool(
        MANIFEST,
        "verify",
        "--root",
        root,
        "--public-key",
        attacker_public_key,
        check=False,
    )

    assert trusted_public_key.read_bytes() != attacker_public_key.read_bytes()
    assert rejected.returncode == 2
    assert "key identity" in rejected.stderr.casefold()


def test_manifest_rejects_a_trust_key_embedded_in_the_backup(tmp_path: Path) -> None:
    root = make_backup_root(tmp_path)
    private_key, external_public_key = make_manifest_signing_keys(root)
    embedded_public_key = root / "metadata" / "embedded-manifest-trust.pem"
    shutil.copyfile(external_public_key, embedded_public_key)
    create_unsigned_manifest(root)
    run_tool(
        MANIFEST,
        "sign",
        "--root",
        root,
        "--private-key",
        private_key,
        "--public-key",
        external_public_key,
    )

    rejected = run_tool(
        MANIFEST,
        "verify",
        "--root",
        root,
        "--public-key",
        embedded_public_key,
        check=False,
    )

    assert rejected.returncode == 2
    assert "external" in rejected.stderr.casefold()


def test_manifest_refuses_to_sign_with_a_private_key_inside_the_backup(
    tmp_path: Path,
) -> None:
    root = make_backup_root(tmp_path)
    external_private_key, external_public_key = make_manifest_signing_keys(root)
    embedded_private_key = root / "metadata" / "embedded-signing-private.pem"
    embedded_public_key = root / "metadata" / "embedded-signing-public.pem"
    shutil.copyfile(external_private_key, embedded_private_key)
    shutil.copyfile(external_public_key, embedded_public_key)
    embedded_private_key.chmod(0o600)
    create_unsigned_manifest(root)

    rejected = run_tool(
        MANIFEST,
        "sign",
        "--root",
        root,
        "--private-key",
        embedded_private_key,
        "--public-key",
        embedded_public_key,
        check=False,
    )

    assert rejected.returncode == 2
    assert "external" in rejected.stderr.casefold()
    assert not (root / "manifest.signature.json").exists()


def test_restore_snapshot_is_no_follow_private_and_source_independent(
    tmp_path: Path,
) -> None:
    source = make_backup_root(tmp_path)
    _private_key, public_key = create_manifest(source)
    snapshot_root = tmp_path / "snapshot-root"
    snapshot_root.mkdir(mode=0o700)

    created = run_tool(
        MANIFEST,
        "snapshot",
        "--source",
        source,
        "--snapshot-root",
        snapshot_root,
        "--public-key",
        public_key,
    )
    assert json.loads(created.stdout)["status"] == "created"
    snapshot = snapshot_root / "backup"
    assert verify_manifest(snapshot, public_key=public_key).returncode == 0

    (source / "mysql" / "table-counts.tsv").write_text(
        "auris_flow.audit_logs\t999\n", encoding="utf-8"
    )
    assert verify_manifest(snapshot, public_key=public_key).returncode == 0
    assert (snapshot / "mysql" / "table-counts.tsv").read_text(
        encoding="utf-8"
    ) == "auris_flow.audit_logs\t3\n"

    destroyed = run_tool(
        MANIFEST,
        "destroy-snapshot",
        "--snapshot-root",
        snapshot_root,
    )
    assert json.loads(destroyed.stdout)["status"] == "destroyed"
    assert not snapshot_root.exists()


def test_restore_snapshot_rejects_symlinked_signed_backup_members(
    tmp_path: Path,
) -> None:
    source = make_backup_root(tmp_path)
    _private_key, public_key = create_manifest(source)
    target = tmp_path / "outside"
    target.write_text("outside\n", encoding="utf-8")
    signed_member = source / "mysql" / "table-counts.tsv"
    signed_member.unlink()
    os.symlink(target, signed_member)
    snapshot_root = tmp_path / "snapshot-root"
    snapshot_root.mkdir(mode=0o700)

    rejected = run_tool(
        MANIFEST,
        "snapshot",
        "--source",
        source,
        "--snapshot-root",
        snapshot_root,
        "--public-key",
        public_key,
        check=False,
    )
    assert rejected.returncode == 2
    assert "regular file" in rejected.stderr


def test_restore_snapshot_requires_external_public_key(tmp_path: Path) -> None:
    source = make_backup_root(tmp_path)
    create_manifest(source)
    snapshot_root = tmp_path / "snapshot-root"
    snapshot_root.mkdir(mode=0o700)

    rejected = run_tool(
        MANIFEST,
        "snapshot",
        "--source",
        source,
        "--snapshot-root",
        snapshot_root,
        check=False,
    )

    assert rejected.returncode == 2
    assert "--public-key" in rejected.stderr


def test_restore_snapshot_copies_only_signed_members(tmp_path: Path) -> None:
    source = make_backup_root(tmp_path)
    _private_key, public_key = create_manifest(source)
    unsigned_extra = source / "unsigned-extra.bin"
    unsigned_extra.write_bytes(b"x" * (1024 * 1024))
    snapshot_root = tmp_path / "snapshot-root"
    snapshot_root.mkdir(mode=0o700)

    created = run_tool(
        MANIFEST,
        "snapshot",
        "--source",
        source,
        "--snapshot-root",
        snapshot_root,
        "--public-key",
        public_key,
    )

    snapshot = snapshot_root / "backup"
    assert json.loads(created.stdout)["status"] == "created"
    assert not (snapshot / unsigned_extra.name).exists()
    assert verify_manifest(snapshot, public_key=public_key).returncode == 0


@pytest.mark.parametrize(
    ("control_name", "budget_name", "budget"),
    [
        ("manifest.json", "AURIS_BACKUP_MAX_MANIFEST_BYTES", "1024"),
        ("manifest.sha256", "AURIS_BACKUP_MAX_CHECKSUM_BYTES", "16"),
        (
            "manifest.signature.json",
            "AURIS_BACKUP_MAX_SIGNATURE_BYTES",
            "128",
        ),
    ],
)
def test_verify_rejects_oversized_control_before_touching_artifacts(
    tmp_path: Path,
    control_name: str,
    budget_name: str,
    budget: str,
) -> None:
    root = make_backup_root(tmp_path)
    _private_key, public_key = create_manifest(root)
    signed_member = root / "mysql" / "table-counts.tsv"
    signed_member.unlink()
    os.symlink(tmp_path / "outside", signed_member)
    env = {**os.environ, budget_name: budget}

    rejected = run_tool(
        MANIFEST,
        "verify",
        "--root",
        root,
        "--public-key",
        public_key,
        check=False,
        env=env,
    )

    assert rejected.returncode == 2
    assert control_name in rejected.stderr
    assert "budget" in rejected.stderr.casefold()
    assert "symlink" not in rejected.stderr.casefold()


@pytest.mark.parametrize(
    ("budget_name", "budget", "expected"),
    [
        ("AURIS_BACKUP_MAX_ARTIFACTS", "1", "artifact count"),
        ("AURIS_BACKUP_MAX_SIGNED_BYTES", "1", "signed artifact bytes"),
    ],
)
def test_verify_rejects_signed_manifest_resource_budget_excess(
    tmp_path: Path,
    budget_name: str,
    budget: str,
    expected: str,
) -> None:
    root = make_backup_root(tmp_path)
    _private_key, public_key = create_manifest(root)

    rejected = run_tool(
        MANIFEST,
        "verify",
        "--root",
        root,
        "--public-key",
        public_key,
        check=False,
        env={**os.environ, budget_name: budget},
    )

    assert rejected.returncode == 2
    assert expected in rejected.stderr.casefold()
    assert "budget" in rejected.stderr.casefold()


def test_verify_rejects_invalid_resource_budget_environment(tmp_path: Path) -> None:
    root = make_backup_root(tmp_path)
    _private_key, public_key = create_manifest(root)

    rejected = run_tool(
        MANIFEST,
        "verify",
        "--root",
        root,
        "--public-key",
        public_key,
        check=False,
        env={**os.environ, "AURIS_BACKUP_MAX_ARTIFACTS": "unbounded"},
    )

    assert rejected.returncode == 2
    assert "AURIS_BACKUP_MAX_ARTIFACTS" in rejected.stderr
    assert "positive integer" in rejected.stderr


@pytest.mark.parametrize("command", ["verify", "inspect"])
def test_manifest_commands_authenticate_controls_before_signed_artifacts(
    tmp_path: Path,
    command: str,
) -> None:
    root = make_backup_root(tmp_path)
    _private_key, public_key = create_manifest(root)
    signed_member = root / "mysql" / "table-counts.tsv"
    signed_member.unlink()
    os.symlink(tmp_path / "outside", signed_member)
    (root / "manifest.signature.json").write_text("{}\n", encoding="utf-8")

    rejected = run_tool(
        MANIFEST,
        command,
        "--root",
        root,
        "--public-key",
        public_key,
        check=False,
    )

    assert rejected.returncode == 2
    assert "signature envelope" in rejected.stderr.casefold()
    assert "symlink" not in rejected.stderr.casefold()


def test_offline_verification_wrapper_accepts_a_complete_backup(tmp_path: Path) -> None:
    root = make_backup_root(tmp_path)
    _private_key, public_key = create_manifest(root)
    result = subprocess.run(
        [SCRIPTS / "verify-backup.sh", "--backup", root],
        capture_output=True,
        check=False,
        text=True,
        env={
            **os.environ,
            "AURIS_BACKUP_MANIFEST_VERIFY_KEY_FILE": str(public_key),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "Offline backup verification passed" in result.stdout


def test_offline_wrapper_uses_and_cleans_signed_snapshot(tmp_path: Path) -> None:
    root = make_backup_root(tmp_path)
    _private_key, public_key = create_manifest(root)
    (root / "unsigned-extra.bin").write_bytes(b"x" * (1024 * 1024))
    private_tmp = tmp_path / "private-tmp"
    private_tmp.mkdir(mode=0o700)

    result = subprocess.run(
        [SCRIPTS / "verify-backup.sh", "--backup", root],
        capture_output=True,
        check=False,
        text=True,
        env={
            **os.environ,
            "AURIS_BACKUP_MANIFEST_VERIFY_KEY_FILE": str(public_key),
            "TMPDIR": str(private_tmp),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "Offline backup verification passed" in result.stdout
    assert list(private_tmp.iterdir()) == []


def test_manifest_rejects_symlinks_and_unexpected_files(tmp_path: Path) -> None:
    root = make_backup_root(tmp_path)
    create_manifest(root)
    (root / "unexpected.txt").write_text("not declared\n", encoding="utf-8")
    rejected = verify_manifest(root, check=False)
    assert rejected.returncode == 2
    assert "artifact set mismatch" in rejected.stderr

    (root / "unexpected.txt").unlink()
    target = tmp_path / "target"
    target.write_text("x", encoding="utf-8")
    os.symlink(target, root / "unsafe-link")
    rejected_link = verify_manifest(root, check=False)
    assert rejected_link.returncode == 2
    assert "symlink" in rejected_link.stderr


def minio_entry(
    *,
    key: str,
    version: str,
    timestamp: str,
    size: int,
    etag: str,
    deleted: bool = False,
) -> str:
    return json.dumps(
        {
            "status": "success",
            "type": "file",
            "key": key,
            "versionId": version,
            "lastModified": timestamp,
            "size": size,
            "etag": etag,
            "isDeleteMarker": deleted,
        }
    )


def test_minio_plan_quotes_keys_and_compares_semantic_generations(
    tmp_path: Path,
) -> None:
    listing = tmp_path / "source.jsonl"
    listing.write_text(
        "\n".join(
            [
                minio_entry(
                    key="audio/voice's.wav",
                    version="new-delete",
                    timestamp="2026-07-18T12:01:00Z",
                    size=0,
                    etag="",
                    deleted=True,
                ),
                minio_entry(
                    key="audio/voice's.wav",
                    version="new-content",
                    timestamp="2026-07-18T12:00:30Z",
                    size=7,
                    etag="source-new-etag",
                ),
                minio_entry(
                    key="audio/voice's.wav",
                    version="old-content",
                    timestamp="2026-07-18T12:00:00Z",
                    size=7,
                    etag="source-old-etag",
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    plan = tmp_path / "plan.json"
    run_tool(
        MINIO, "plan", "--listing", listing, "--bucket", "auris-flow", "--output", plan
    )
    document = json.loads(plan.read_text(encoding="utf-8"))
    assert document["schema_version"] == "auris-flow.minio-version-plan/v2"
    assert document["content_hash_algorithm"] == "sha256"
    assert document["summary"] == {
        "content_bytes": 14,
        "delete_markers": 1,
        "object_keys": 1,
        "versions": 3,
    }
    assert all(
        record["content_sha256"] is None
        for record in document["versions"]
        if not record["delete_marker"]
    )

    backup_root = tmp_path / "backup"
    payloads = {
        "old-content": b"old-v1!",
        "new-content": b"new-v2!",
    }
    for record in document["versions"]:
        if record["delete_marker"]:
            continue
        artifact = backup_root / record["artifact"]
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(payloads[record["version_id"]])
    bound_plan = tmp_path / "bound-plan.json"
    run_tool(
        MINIO,
        "bind-artifacts",
        "--plan",
        plan,
        "--backup-root",
        backup_root,
        "--output",
        bound_plan,
    )
    bound = json.loads(bound_plan.read_text(encoding="utf-8"))
    for record in bound["versions"]:
        if record["delete_marker"]:
            assert record["content_sha256"] is None
        else:
            assert (
                record["content_sha256"]
                == hashlib.sha256(payloads[record["version_id"]]).hexdigest()
            )
    run_tool(
        MINIO,
        "verify-artifacts",
        "--plan",
        bound_plan,
        "--backup-root",
        backup_root,
    )

    restore_shell = tmp_path / "restore.sh"
    run_tool(
        MINIO, "emit-restore-shell", "--plan", bound_plan, "--output", restore_shell
    )
    subprocess.run(["bash", "-n", restore_shell], check=True)
    shell_text = restore_shell.read_text(encoding="utf-8")
    assert "voice'\"'\"'s.wav" in shell_text
    assert "/opt/auris/minio-client.sh rm --quiet --force" in shell_text
    assert "auris/auris-flow/audio/voice" in shell_text
    assert "target/auris-flow" not in shell_text
    assert "/run/secrets/" not in shell_text
    assert "alias set" not in shell_text
    assert "sha256sum" in shell_text
    assert "wc -c" in shell_text

    restored = tmp_path / "restored.jsonl"
    restored.write_text(
        "\n".join(
            [
                minio_entry(
                    key="audio/voice's.wav",
                    version="target-delete",
                    timestamp="2026-07-18T13:01:00Z",
                    size=0,
                    etag="",
                    deleted=True,
                ),
                minio_entry(
                    key="audio/voice's.wav",
                    version="target-new-content",
                    timestamp="2026-07-18T13:00:30Z",
                    size=7,
                    etag="different-new-etag",
                ),
                minio_entry(
                    key="audio/voice's.wav",
                    version="target-content",
                    timestamp="2026-07-18T13:00:00Z",
                    size=7,
                    etag="different-old-etag",
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    compared = run_tool(
        MINIO, "compare-listing", "--plan", bound_plan, "--listing", restored
    )
    assert json.loads(compared.stdout)["status"] == "verified"

    verify_shell = tmp_path / "verify-restored.sh"
    run_tool(
        MINIO,
        "emit-verify-shell",
        "--plan",
        bound_plan,
        "--listing",
        restored,
        "--output",
        verify_shell,
    )
    verify_text = verify_shell.read_text(encoding="utf-8")
    assert "--version-id target-content" in verify_text
    assert "--version-id target-new-content" in verify_text
    assert "voice'\"'\"'s.wav" in verify_text
    assert "sha256sum" in verify_text
    assert "wc -c" in verify_text

    fake_client = tmp_path / "fake-minio-client.sh"
    fake_client.write_text(
        """#!/bin/sh
set -eu
[ "$1" = cp ] && [ "$2" = --quiet ] && [ "$3" = --version-id ]
version_id="$4"
target="$6"
case "$version_id" in
  target-content) cp "$TARGET_OLD" "$target" ;;
  target-new-content) cp "$TARGET_NEW" "$target" ;;
  *) exit 41 ;;
esac
""",
        encoding="utf-8",
    )
    fake_client.chmod(0o755)
    executable_verify = tmp_path / "verify-restored-executable.sh"
    executable_verify.write_text(
        verify_text.replace("/opt/auris/minio-client.sh", str(fake_client)),
        encoding="utf-8",
    )
    old_target = tmp_path / "target-old.bin"
    new_target = tmp_path / "target-new.bin"
    old_target.write_bytes(payloads["old-content"])
    new_target.write_bytes(payloads["new-content"])
    verify_env = {
        **os.environ,
        "TARGET_OLD": str(old_target),
        "TARGET_NEW": str(new_target),
    }
    subprocess.run(["sh", executable_verify], check=True, env=verify_env)

    new_target.write_bytes(b"tamper!")  # same seven-byte size as the bound generation
    same_size_tamper = subprocess.run(
        ["sh", executable_verify], check=False, env=verify_env
    )
    assert same_size_tamper.returncode != 0

    new_target.write_bytes(payloads["new-content"])
    swapped = subprocess.run(
        ["sh", executable_verify],
        check=False,
        env={
            **verify_env,
            "TARGET_OLD": str(new_target),
            "TARGET_NEW": str(old_target),
        },
    )
    assert swapped.returncode != 0

    missing_version = tmp_path / "missing-version.jsonl"
    missing_version.write_text(
        "\n".join(restored.read_text(encoding="utf-8").splitlines()[:-1]) + "\n",
        encoding="utf-8",
    )
    mismatch = run_tool(
        MINIO,
        "compare-listing",
        "--plan",
        bound_plan,
        "--listing",
        missing_version,
        check=False,
    )
    assert mismatch.returncode == 2


def test_minio_bound_plan_rejects_same_size_artifact_replacement(
    tmp_path: Path,
) -> None:
    listing = tmp_path / "source.jsonl"
    listing.write_text(
        minio_entry(
            key="special/空 格+quote's.bin",
            version="source-version",
            timestamp="2026-07-18T12:00:00Z",
            size=8,
            etag="etag-is-not-authority",
        )
        + "\n",
        encoding="utf-8",
    )
    plan = tmp_path / "plan.json"
    run_tool(
        MINIO, "plan", "--listing", listing, "--bucket", "auris-flow", "--output", plan
    )
    document = json.loads(plan.read_text(encoding="utf-8"))
    artifact = tmp_path / "backup" / document["versions"][0]["artifact"]
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"original")
    bound = tmp_path / "bound.json"
    run_tool(
        MINIO,
        "bind-artifacts",
        "--plan",
        plan,
        "--backup-root",
        tmp_path / "backup",
        "--output",
        bound,
    )

    artifact.write_bytes(b"replaced")
    rejected = run_tool(
        MINIO,
        "verify-artifacts",
        "--plan",
        bound,
        "--backup-root",
        tmp_path / "backup",
        check=False,
    )
    assert rejected.returncode == 2
    assert "checksum mismatch" in rejected.stderr


def test_minio_plan_rejects_control_characters(tmp_path: Path) -> None:
    listing = tmp_path / "unsafe.jsonl"
    listing.write_text(
        minio_entry(
            key="audio/bad\tname.wav",
            version="v1",
            timestamp="2026-07-18T12:00:00Z",
            size=1,
            etag="a",
        )
        + "\n",
        encoding="utf-8",
    )
    rejected = run_tool(
        MINIO,
        "plan",
        "--listing",
        listing,
        "--bucket",
        "auris-flow",
        "--output",
        tmp_path / "plan.json",
        check=False,
    )
    assert rejected.returncode == 2
    assert "control character" in rejected.stderr


def test_qdrant_snapshot_metadata_is_offline_verifiable(tmp_path: Path) -> None:
    qdrant = tmp_path / "qdrant"
    snapshots = qdrant / "snapshots"
    snapshots.mkdir(parents=True)
    payload = b"derived snapshot"
    artifact = snapshots / "collection.snapshot"
    artifact.write_bytes(payload)
    write_json(
        qdrant / "snapshots.json",
        {
            "schema_version": "auris-flow.qdrant-snapshots/v2",
            "qdrant_version": "1.14.1",
            "authority": "derived-rebuildable-from-mysql-and-minio",
            "collections": [
                {
                    "name": "knowledge_chunks",
                    "points_count": 9,
                    "snapshot_name": "source.snapshot",
                    "artifact": "qdrant/snapshots/collection.snapshot",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                    "semantics": {
                        "fingerprint_algorithm": "sha256-canonical-point-digests-v1",
                        "points_fingerprint_sha256": "a" * 64,
                        "scope_policy": "tenant-project-required",
                        "probe": {
                            "point_id": "00000000-0000-0000-0000-000000000001",
                            "payload_sha256": "b" * 64,
                            "vector_sha256": "c" * 64,
                            "vector_kind": "unnamed-dense",
                            "scope": {
                                "tenant_id": "tenant-fixture",
                                "project_id": "project-fixture",
                            },
                        },
                    },
                }
            ],
            "aliases": [],
        },
    )
    verified = run_tool(QDRANT, "validate", "--input", qdrant)
    assert json.loads(verified.stdout)["collections"] == 1
    artifact.write_bytes(b"tampered")
    rejected = run_tool(QDRANT, "validate", "--input", qdrant, check=False)
    assert rejected.returncode == 2
    assert "checksum mismatch" in rejected.stderr


def test_mysql_dump_structural_verifier(tmp_path: Path) -> None:
    dump = tmp_path / "all-databases.sql.gz"
    with gzip.open(dump, "wt", encoding="utf-8") as handle:
        handle.write("-- MySQL dump 10.13\nSTART TRANSACTION;\n")
        for database in ("auris_flow", "keycloak", "dagster"):
            handle.write(f"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `{database}`;\n")
            handle.write(f"USE `{database}`;\n")
    assert run_tool(MYSQL_DUMP, "verify", "--input", dump).returncode == 0


@pytest.mark.parametrize(
    "script_name",
    ["backup.sh", "restore.sh", "verify-backup.sh", "finalize-restore.sh"],
)
def test_shell_scripts_are_strict_and_parse(script_name: str) -> None:
    script = SCRIPTS / script_name
    subprocess.run(["bash", "-n", script], check=True)
    source = script.read_text(encoding="utf-8")
    assert "set -euo pipefail" in source
    assert "rm -rf" not in source
    assert "/Users/" not in source
    assert "/home/" not in source


def test_minio_client_uses_ephemeral_config_and_secret_backed_environment_alias() -> (
    None
):
    subprocess.run(["sh", "-n", MINIO_CLIENT], check=True)
    source = MINIO_CLIENT.read_text(encoding="utf-8")

    assert "set -eu" in source
    assert "umask 077" in source
    assert 'MC_CONFIG_PARENT="${TMPDIR:-/tmp}"' in source
    assert 'mktemp -d "${MC_CONFIG_PARENT%/}/auris-flow-mc.XXXXXX"' in source
    assert (
        "${OBJECT_STORAGE_ACCESS_KEY_FILE:-/run/secrets/object_storage_access_key}"
        in source
    )
    assert (
        "${OBJECT_STORAGE_SECRET_KEY_FILE:-/run/secrets/object_storage_secret_key}"
        in source
    )
    assert 'cat "${OBJECT_STORAGE_ACCESS_KEY_FILE}"' in source
    assert 'cat "${OBJECT_STORAGE_SECRET_KEY_FILE}"' in source
    assert 'MC_HOST_auris="http://${access_key}:${secret_key}@minio:9000"' in source
    assert 'mc --config-dir "${MC_CONFIG_DIR}" "$@"' in source
    assert not any(
        line.strip().startswith("mc alias set") for line in source.splitlines()
    )
    assert 'mc "$access_key" "$secret_key"' not in source


def test_minio_client_keeps_secret_values_out_of_mc_process_arguments(
    tmp_path: Path,
) -> None:
    access_key = "auristestaccess"
    secret_key = "testsecret0123456789"
    access_file = tmp_path / "access-key"
    secret_file = tmp_path / "secret-key"
    access_file.write_text(access_key + "\n", encoding="utf-8")
    secret_file.write_text(secret_key + "\n", encoding="utf-8")

    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_mc = fake_bin / "mc"
    fake_mc.write_text(
        """#!/bin/sh
set -eu
printf '%s\n' "${MC_CONFIG_DIR}" >"${AURIS_TEST_CAPTURE_DIR}/config-dir"
printf '%s\n' "${MC_HOST_auris}" >"${AURIS_TEST_CAPTURE_DIR}/host-alias"
printf '%s\n' "$@" >"${AURIS_TEST_CAPTURE_DIR}/arguments"
if [ -d "${MC_CONFIG_DIR}" ] && [ -w "${MC_CONFIG_DIR}" ]; then
  printf 'writable\n' >"${AURIS_TEST_CAPTURE_DIR}/config-state"
fi
""",
        encoding="utf-8",
    )
    fake_mc.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AURIS_TEST_CAPTURE_DIR": str(capture_dir),
        "OBJECT_STORAGE_ACCESS_KEY_FILE": str(access_file),
        "OBJECT_STORAGE_SECRET_KEY_FILE": str(secret_file),
    }

    subprocess.run(
        [MINIO_CLIENT, "stat", "auris/auris-flow"],
        check=True,
        env=env,
    )
    config_dir = Path((capture_dir / "config-dir").read_text(encoding="utf-8").strip())
    assert (capture_dir / "config-state").read_text(encoding="utf-8").strip() == (
        "writable"
    )
    assert not config_dir.exists()
    assert (capture_dir / "host-alias").read_text(encoding="utf-8").strip() == (
        f"http://{access_key}:{secret_key}@minio:9000"
    )
    arguments = (capture_dir / "arguments").read_text(encoding="utf-8").splitlines()
    assert arguments == [
        "--config-dir",
        str(config_dir),
        "stat",
        "auris/auris-flow",
    ]
    assert access_key not in arguments
    assert secret_key not in arguments


def test_minio_bootstrap_mounts_dedicated_client_with_writable_ephemeral_config() -> (
    None
):
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    service = compose["services"]["minio-bootstrap"]
    mounted_configs = {
        item["source"]: item for item in service["configs"] if isinstance(item, dict)
    }

    assert service["image"].startswith("${MINIO_MC_IMAGE:-minio/mc:")
    assert service["read_only"] is True
    assert any(str(item).startswith("/tmp:") for item in service["tmpfs"])
    assert set(service["secrets"]) >= {
        "object_storage_access_key",
        "object_storage_secret_key",
    }
    assert service["environment"] == {
        "OBJECT_STORAGE_ACCESS_KEY_FILE": "/run/secrets/object_storage_access_key",
        "OBJECT_STORAGE_SECRET_KEY_FILE": "/run/secrets/object_storage_secret_key",
    }
    assert mounted_configs["minio_client"] == {
        "source": "minio_client",
        "target": "/opt/auris/minio-client.sh",
        "mode": 0o555,
    }
    assert compose["configs"]["minio_client"]["file"] == "./minio/client.sh"


def test_compose_declares_distinct_host_only_backup_and_restore_key_roles() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    assert compose["x-auris-backup-manifest-trust"]["exposure"] == (
        "host-backup-tools-only"
    )
    assert compose["x-auris-restore-attestation-trust"] == {
        "algorithm": "ed25519",
        "private_key_file": (
            "${AURIS_SECRETS_DIR:-./secrets}/"
            "restore_attestation_signing_private_key.pem"
        ),
        "public_key_file": (
            "${AURIS_SECRETS_DIR:-./secrets}/restore_attestation_signing_public_key.pem"
        ),
        "exposure": "host-restore-tools-only",
    }
    assert compose["secrets"]["restore_attestation_signing_private_key"][
        "file"
    ].endswith("/restore_attestation_signing_private_key.pem")
    assert compose["secrets"]["restore_attestation_signing_public_key"][
        "file"
    ].endswith("/restore_attestation_signing_public_key.pem")


def test_minio_fresh_named_volume_uses_a_least_privileged_one_shot_initializer() -> (
    None
):
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    initializer = compose["services"]["minio-volume-init"]
    minio = compose["services"]["minio"]

    assert initializer["image"] == minio["image"]
    assert initializer["user"] == "0:0"
    assert initializer["restart"] == "no"
    assert initializer["read_only"] is True
    assert initializer["network_mode"] == "none"
    assert initializer["cap_drop"] == ["ALL"]
    assert initializer["cap_add"] == ["CHOWN"]
    assert initializer["security_opt"] == ["no-new-privileges:true"]
    assert initializer["volumes"] == ["minio_data:/data"]
    assert "secrets" not in initializer
    assert "environment" not in initializer

    command = "\n".join(str(item) for item in initializer["command"])
    chown_lines = [
        line.strip()
        for line in command.splitlines()
        if line.strip().startswith("chown ")
    ]
    assert chown_lines == ["chown 1000:1000 /data"]
    assert "chown -R" not in command
    assert "--recursive" not in command
    assert "/data/*" not in command
    assert minio["user"] == "1000:1000"
    assert minio["depends_on"]["minio-volume-init"] == {
        "condition": "service_completed_successfully"
    }


def test_backup_and_restore_use_dedicated_mc_container_for_every_server_query() -> None:
    backup = (SCRIPTS / "backup.sh").read_text(encoding="utf-8")
    restore = (SCRIPTS / "restore.sh").read_text(encoding="utf-8")

    for source in (backup, restore):
        assert "minio_mc()" in source
        assert "--entrypoint /opt/auris/minio-client.sh" in source
        assert 'minio-bootstrap "$@"' in source
        assert "compose exec -T minio mc" not in source
        assert "local/auris-flow" not in source

    assert "minio_mc du --versions --json auris/auris-flow" in backup
    assert "minio_mc ls --recursive --versions --json auris/auris-flow" in backup
    assert '"$(minio_mc --version | head -n 1)"' in backup
    assert (
        restore.count("minio_mc ls --recursive --versions --json auris/auris-flow") == 2
    )


def test_backup_and_restore_encode_authority_and_fail_closed_invariants() -> None:
    backup = (SCRIPTS / "backup.sh").read_text(encoding="utf-8")
    assert "--single-transaction" in backup
    assert "--routines --triggers --events" in backup
    assert "encrypted-external" in backup
    assert "ephemeral-ci-drill" in backup
    assert "--release-gate-drill" in backup
    assert '--storage-boundary "${STORAGE_BOUNDARY}"' in backup
    assert "writer service" in backup
    assert "dagster-storage-bootstrap" in backup
    assert "mc ls --recursive --versions" in backup
    assert "auris_backup_last_success_timestamp_seconds" in backup
    assert "auris_backup.prom" in backup
    assert "verify-running-images" in backup
    assert "release-metadata.sigstore.json" in backup
    assert "--all-running-release-services" in backup
    assert "--verify-signature" in backup
    assert "bind-artifacts" in backup
    assert "verify-artifacts" in backup
    assert "db-bootstrap minio-volume-init minio-bootstrap identity-bootstrap" in backup
    assert '--project-name "${PRODUCTION_PROJECT_NAME}"' in backup
    assert '--docker-context "${DOCKER_CONTEXT_NAME}"' in backup
    assert "paths_overlap" in backup
    assert "AURIS_SOURCE_COMMIT is forbidden" in backup
    assert "git -C" not in backup
    assert 'COMPOSE_FILE="${PRODUCTION_ROOT}/compose.yaml"' in backup
    assert 'manifest.py" verify-key-pair' in backup
    assert 'manifest.py" sign' in backup
    assert "AURIS_BACKUP_MANIFEST_SIGNING_PRIVATE_KEY_FILE" in backup
    assert "AURIS_BACKUP_MANIFEST_VERIFY_KEY_FILE" in backup
    assert "AURIS_RESTORE_ATTESTATION_VERIFY_KEY_FILE" in backup
    assert (
        '--restore-attestation-public-key "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}"'
        in backup
    )
    assert (
        'paths_overlap "${MANIFEST_SIGNING_PRIVATE_KEY_FILE}" "${OUTPUT_ROOT}"'
        in backup
    )
    assert 'paths_overlap "${MANIFEST_VERIFY_KEY_FILE}" "${OUTPUT_ROOT}"' in backup
    assert (
        'paths_overlap "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" "${OUTPUT_ROOT}"'
        in backup
    )
    assert "--entrypoint python qdrant-backup-tool" in backup
    assert "--entrypoint python bff" not in backup

    restore = (SCRIPTS / "restore.sh").read_text(encoding="utf-8")
    assert (
        restore.index('RESTORE_STEP="mysql-authority"')
        < restore.index('RESTORE_STEP="minio-authority"')
        < restore.index('RESTORE_STEP="qdrant-derived-index"')
    )
    assert "target MySQL contains rows" in restore
    assert "dagster-storage-bootstrap" in restore
    assert "target MinIO bucket contains object versions" in restore
    assert "target Qdrant contains collections" in restore
    assert "Redis was not restored" in restore
    assert "release_bundle.py" in restore
    assert "identity" in restore
    assert "--allow-release-migration-from" in restore
    assert "verify-restore-source" in restore
    assert 'manifest.py" snapshot' in restore
    assert '--public-key "${MANIFEST_VERIFY_KEY_FILE}"' in restore
    assert 'BACKUP_ROOT="${RESTORE_SNAPSHOT_ROOT}/backup"' in restore
    assert "git -C" not in restore
    assert "verify-running-images" in restore
    assert "--all-running-release-services" in restore
    assert "--verify-signature" in restore
    assert "emit-verify-shell" in restore
    assert "restored MinIO generation content" in restore
    assert (
        "db-bootstrap minio-volume-init minio-bootstrap identity-bootstrap" in restore
    )
    assert '--project-name "${PRODUCTION_PROJECT_NAME}"' in restore
    assert '--docker-context "${DOCKER_CONTEXT_NAME}"' in restore
    assert "paths_overlap" in restore
    assert "AURIS_BACKUP_MANIFEST_VERIFY_KEY_FILE" in restore
    assert "AURIS_RESTORE_ATTESTATION_VERIFY_KEY_FILE" in restore
    assert 'paths_overlap "${MANIFEST_VERIFY_KEY_FILE}" "${BACKUP_ROOT}"' in restore
    assert '--manifest-signing-key-id "${backup_signing_key_id}"' in restore
    assert '--attestation-key-id "${restore_attestation_key_id}"' in restore
    assert restore.count("--entrypoint python qdrant-backup-tool") == 3
    assert "--entrypoint python bff" not in restore
    signature_verification = restore.index('manifest.py" verify')
    assert signature_verification < restore.index('gzip -t "${BACKUP_ROOT}')
    assert signature_verification < restore.index('RESTORE_STEP="mysql-authority"')
    assert "RESTORE_PENDING_EXIT_CODE=3" in restore
    assert '"pending-qdrant-rebuild"' in restore
    assert 'exit "${RESTORE_PENDING_EXIT_CODE}"' in restore
    assert (
        "MySQL authority counts and MinIO generation hashes are consistent; "
        "Qdrant remains pending"
    ) in restore
    assert restore.index('exit "${RESTORE_PENDING_EXIT_CODE}"') < restore.index(
        'RESTORE_STEP="complete"'
    )

    verify_backup = (SCRIPTS / "verify-backup.sh").read_text(encoding="utf-8")
    assert 'compose_drill_with_deadline "${DRILL_PULL_TIMEOUT}"' in verify_backup
    assert "compose_drill build" not in verify_backup
    assert "verify-running-images" in verify_backup
    assert "--verify-signature" in verify_backup
    assert "verify-artifacts" in verify_backup
    assert '--project-name "${DRILL_PROJECT}"' in verify_backup
    assert '--docker-context "${DOCKER_CONTEXT_NAME}"' in verify_backup
    assert "AURIS_BACKUP_MANIFEST_VERIFY_KEY_FILE" in verify_backup
    assert (
        'paths_overlap "${MANIFEST_VERIFY_KEY_FILE}" "${BACKUP_ROOT}"' in verify_backup
    )
    assert 'manifest.py" snapshot' in verify_backup
    assert '--public-key "${MANIFEST_VERIFY_KEY_FILE}"' in verify_backup
    snapshot_position = verify_backup.index('manifest.py" snapshot')
    verification_position = verify_backup.index('manifest.py" verify')
    mysql_position = verify_backup.index('mysql_dump.py" verify')
    assert snapshot_position < verification_position < mysql_position
    assert 'BACKUP_ROOT="${VERIFY_SNAPSHOT_ROOT}/backup"' in verify_backup
    assert 'manifest.py" destroy-snapshot' in verify_backup

    finalize = (SCRIPTS / "finalize-restore.sh").read_text(encoding="utf-8")
    assert '"${RESTORE_STATE_TOOL}" require-pending' in finalize
    assert "verify-semantics --input /backup/qdrant" in finalize
    assert 'RESTORE_STEP="bff-readiness"' in finalize
    assert '"${RESTORE_STATE_TOOL}" finalize' in finalize
    assert '"${RESTORE_STATE_TOOL}" verify-complete' in finalize
    assert 'paths_overlap "${MANIFEST_VERIFY_KEY_FILE}" "${BACKUP_ROOT}"' in finalize
    assert '--public-key "${MANIFEST_VERIFY_KEY_FILE}"' in finalize
    assert "MANIFEST_SIGNING_PRIVATE_KEY_FILE" not in finalize
    assert "AURIS_RESTORE_ATTESTATION_SIGNING_PRIVATE_KEY_FILE" in finalize
    assert "AURIS_RESTORE_ATTESTATION_VERIFY_KEY_FILE" in finalize
    assert '--private-key "${RESTORE_ATTESTATION_PRIVATE_KEY_FILE}"' in finalize
    assert '--public-key "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}"' in finalize
    assert '--observed-at-utc "${observed_at_utc}"' in finalize
    assert '--manifest-signing-key-id "${backup_signing_key_id}"' in finalize
    assert '--attestation-key-id "${restore_attestation_key_id}"' in finalize
    assert '--qdrant-evidence "${QDRANT_EVIDENCE_FILE}"' in finalize
    assert '--running-images-evidence "${RUNNING_IMAGES_EVIDENCE_FILE}"' in finalize
    assert '--readyz-evidence "${READYZ_EVIDENCE_FILE}"' in finalize
    assert '"${RESTORE_STATE_TOOL}" snapshot-private-file' in finalize
    assert 'ENV_FILE="${ENV_SNAPSHOT_FILE}"' in finalize
    assert "--entrypoint python qdrant-backup-tool" in finalize
    assert "compose stop --timeout 60 edge dagster-daemon bff dagster-code" in finalize
    assert "compose stop --timeout 60 worker" in finalize
    assert "start-read-only-readiness-plane" not in finalize
    assert 'Request("http://bff:8000/readyz"' in finalize
    assert 'Request("http://127.0.0.1:8000/readyz"' not in finalize
    assert "SELECT COUNT(*) FROM auris_flow.outbox_events" in finalize
    ingress_position = finalize.index('RESTORE_STEP="freeze-new-write-ingress"')
    env_snapshot_position = finalize.index(
        'RESTORE_STEP="snapshot-compose-environment"'
    )
    compose_preflight_position = finalize.index(
        'RESTORE_STEP="running-image-and-service-preflight"'
    )
    attestation_preflight_position = finalize.index(
        'RESTORE_STEP="restore-attestation-key-preflight"'
    )
    readiness_position = finalize.index('RESTORE_STEP="bff-readiness"')
    writer_freeze_position = finalize.index('RESTORE_STEP="freeze-all-qdrant-writers"')
    final_outbox_position = finalize.index('RESTORE_STEP="final-outbox-write-fence"')
    qdrant_position = finalize.index("verify-semantics --input /backup/qdrant")
    post_qdrant_position = finalize.index('RESTORE_STEP="post-qdrant-write-fence"')
    transition_position = finalize.index('"${RESTORE_STATE_TOOL}" finalize')
    assert (
        env_snapshot_position
        < attestation_preflight_position
        < compose_preflight_position
        < readiness_position
        < ingress_position
        < writer_freeze_position
        < final_outbox_position
        < qdrant_position
        < post_qdrant_position
        < transition_position
    )
    assert "Write plane remains fenced" in finalize


def test_qdrant_rebuild_restore_state_requires_governed_finalize(
    tmp_path: Path,
) -> None:
    state = tmp_path / "restore-state.json"
    manifest_private_key, manifest_public_key = make_manifest_signing_keys(state)
    attestation_private_key, attestation_public_key = make_restore_attestation_keys(
        state
    )
    qdrant_evidence, running_images_evidence, readyz_evidence = (
        make_restore_finalize_evidence(tmp_path)
    )
    backup_id = "auris-flow-20260718T120000Z-0123456789ab"
    manifest_sha256 = "a" * 64
    manifest_signing_key_id = key_id(manifest_public_key)
    attestation_key_id = key_id(attestation_public_key)

    pending = run_tool(
        RESTORE_STATE,
        "create-pending",
        "--output",
        state,
        "--backup-id",
        backup_id,
        "--backup-created-at-utc",
        "2026-07-18T12:00:00Z",
        "--source-commit",
        SOURCE_COMMIT,
        "--manifest-sha256",
        manifest_sha256,
        "--manifest-signing-key-id",
        manifest_signing_key_id,
        "--attestation-key-id",
        attestation_key_id,
        "--pending-at-utc",
        "2026-07-18T13:00:00Z",
        check=False,
    )
    assert pending.returncode == 3
    assert json.loads(pending.stdout)["status"] == "pending-qdrant-rebuild"
    document = json.loads(state.read_text(encoding="utf-8"))
    assert document["status"] == "pending-qdrant-rebuild"
    assert document["manifest_signing_key_id"] == manifest_signing_key_id
    assert document["attestation_key_id"] == attestation_key_id
    assert len(document["restore_challenge"]) == 64
    assert all(
        character in "0123456789abcdef" for character in document["restore_challenge"]
    )
    assert "completed_at_utc" not in document

    wrong_identity = run_tool(
        RESTORE_STATE,
        "require-pending",
        "--state",
        state,
        "--backup-id",
        backup_id,
        "--source-commit",
        "f" * 40,
        "--manifest-sha256",
        manifest_sha256,
        "--manifest-signing-key-id",
        manifest_signing_key_id,
        "--attestation-key-id",
        attestation_key_id,
        check=False,
    )
    assert wrong_identity.returncode == 2
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == (
        "pending-qdrant-rebuild"
    )

    verified_pending = run_tool(
        RESTORE_STATE,
        "require-pending",
        "--state",
        state,
        "--backup-id",
        backup_id,
        "--source-commit",
        SOURCE_COMMIT,
        "--manifest-sha256",
        manifest_sha256,
        "--manifest-signing-key-id",
        manifest_signing_key_id,
        "--attestation-key-id",
        attestation_key_id,
    )
    assert json.loads(verified_pending.stdout)["status"] == ("pending-qdrant-rebuild")
    pending_payload = state.read_bytes()

    wrong_key_role = run_tool(
        RESTORE_STATE,
        "finalize",
        "--state",
        state,
        "--backup-id",
        backup_id,
        "--source-commit",
        SOURCE_COMMIT,
        "--manifest-sha256",
        manifest_sha256,
        "--manifest-signing-key-id",
        manifest_signing_key_id,
        "--attestation-key-id",
        attestation_key_id,
        "--qdrant-evidence",
        qdrant_evidence,
        "--running-images-evidence",
        running_images_evidence,
        "--readyz-evidence",
        readyz_evidence,
        "--private-key",
        manifest_private_key,
        "--public-key",
        manifest_public_key,
        "--observed-at-utc",
        "2026-07-18T13:59:59Z",
        "--completed-at-utc",
        "2026-07-18T14:00:00Z",
        check=False,
    )
    assert wrong_key_role.returncode == 2
    assert "attestation key" in wrong_key_role.stderr.casefold()
    assert state.read_bytes() == pending_payload
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == (
        "pending-qdrant-rebuild"
    )

    finalized = run_tool(
        RESTORE_STATE,
        "finalize",
        "--state",
        state,
        "--backup-id",
        backup_id,
        "--source-commit",
        SOURCE_COMMIT,
        "--manifest-sha256",
        manifest_sha256,
        "--manifest-signing-key-id",
        manifest_signing_key_id,
        "--attestation-key-id",
        attestation_key_id,
        "--qdrant-evidence",
        qdrant_evidence,
        "--running-images-evidence",
        running_images_evidence,
        "--readyz-evidence",
        readyz_evidence,
        "--private-key",
        attestation_private_key,
        "--public-key",
        attestation_public_key,
        "--observed-at-utc",
        "2026-07-18T13:59:59Z",
        "--completed-at-utc",
        "2026-07-18T14:00:00Z",
    )
    assert json.loads(finalized.stdout)["status"] == "complete"
    completed = json.loads(state.read_text(encoding="utf-8"))
    assert completed["status"] == "complete"
    governed = completed["governed_finalize"]
    assert governed["qdrant_evidence"]["status"] == "verified"
    assert governed["observed_at_utc"] == "2026-07-18T13:59:59Z"
    assert governed["running_images_evidence"]["source_commit"] == SOURCE_COMMIT
    assert governed["readyz_evidence"]["data"]["missing_required"] == {}
    for evidence_name in ("qdrant", "running_images", "readyz"):
        canonical = (
            json.dumps(
                governed[f"{evidence_name}_evidence"],
                sort_keys=True,
                indent=2,
                ensure_ascii=True,
            )
            + "\n"
        ).encode("ascii")
        assert (
            governed[f"{evidence_name}_evidence_sha256"]
            == hashlib.sha256(canonical).hexdigest()
        )
    assert completed["attestation"]["algorithm"] == "ed25519"
    assert completed["attestation"]["key_id"] == attestation_key_id
    assert (
        completed["attestation"]["restore_challenge"] == document["restore_challenge"]
    )
    assert (
        completed["attestation"]["manifest_signing_key_id"] == manifest_signing_key_id
    )
    assert completed["attestation"]["observed_at_utc"] == governed["observed_at_utc"]

    verified = run_tool(
        RESTORE_STATE,
        "verify-complete",
        "--state",
        state,
        "--public-key",
        attestation_public_key,
        "--backup-id",
        backup_id,
        "--source-commit",
        SOURCE_COMMIT,
        "--manifest-sha256",
        manifest_sha256,
        "--manifest-signing-key-id",
        manifest_signing_key_id,
        "--attestation-key-id",
        attestation_key_id,
    )
    assert json.loads(verified.stdout)["status"] == "complete"

    wrong_verifier = run_tool(
        RESTORE_STATE,
        "verify-complete",
        "--state",
        state,
        "--public-key",
        manifest_public_key,
        "--backup-id",
        backup_id,
        "--source-commit",
        SOURCE_COMMIT,
        "--manifest-sha256",
        manifest_sha256,
        "--manifest-signing-key-id",
        manifest_signing_key_id,
        "--attestation-key-id",
        attestation_key_id,
        check=False,
    )
    assert wrong_verifier.returncode == 2
    assert "attestation key" in wrong_verifier.stderr.casefold()

    completed_payload = state.read_bytes()
    tampered_challenge = json.loads(completed_payload)
    tampered_challenge["restore_challenge"] = "f" * 64
    write_json(state, tampered_challenge)
    state.chmod(0o600)
    challenge_rejected = run_tool(
        RESTORE_STATE,
        "verify-complete",
        "--state",
        state,
        "--public-key",
        attestation_public_key,
        "--backup-id",
        backup_id,
        "--source-commit",
        SOURCE_COMMIT,
        "--manifest-sha256",
        manifest_sha256,
        "--manifest-signing-key-id",
        manifest_signing_key_id,
        "--attestation-key-id",
        attestation_key_id,
        check=False,
    )
    assert challenge_rejected.returncode == 2
    state.write_bytes(completed_payload)
    state.chmod(0o600)

    tampered_observation = json.loads(completed_payload)
    tampered_observation["governed_finalize"]["observed_at_utc"] = (
        "2026-07-18T13:59:58Z"
    )
    write_json(state, tampered_observation)
    state.chmod(0o600)
    observation_rejected = run_tool(
        RESTORE_STATE,
        "verify-complete",
        "--state",
        state,
        "--public-key",
        attestation_public_key,
        "--backup-id",
        backup_id,
        "--source-commit",
        SOURCE_COMMIT,
        "--manifest-sha256",
        manifest_sha256,
        "--manifest-signing-key-id",
        manifest_signing_key_id,
        "--attestation-key-id",
        attestation_key_id,
        check=False,
    )
    assert observation_rejected.returncode == 2
    state.write_bytes(completed_payload)
    state.chmod(0o600)

    replay = run_tool(
        RESTORE_STATE,
        "finalize",
        "--state",
        state,
        "--backup-id",
        backup_id,
        "--source-commit",
        SOURCE_COMMIT,
        "--manifest-sha256",
        manifest_sha256,
        "--manifest-signing-key-id",
        manifest_signing_key_id,
        "--attestation-key-id",
        attestation_key_id,
        "--qdrant-evidence",
        qdrant_evidence,
        "--running-images-evidence",
        running_images_evidence,
        "--readyz-evidence",
        readyz_evidence,
        "--private-key",
        attestation_private_key,
        "--public-key",
        attestation_public_key,
        "--observed-at-utc",
        "2026-07-18T13:59:59Z",
        "--completed-at-utc",
        "2026-07-18T14:00:00Z",
        check=False,
    )
    assert replay.returncode == 2


def test_restore_state_snapshots_private_input_without_following_links(
    tmp_path: Path,
) -> None:
    source = tmp_path / "production.env"
    source.write_text(
        "APP_ENV=prod\nSECRET_FILE=/run/secrets/example\n",
        encoding="utf-8",
    )
    source.chmod(0o600)
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir(mode=0o700)
    snapshot = snapshot_root / "compose.env"

    result = run_tool(
        RESTORE_STATE,
        "snapshot-private-file",
        "--source",
        source,
        "--output",
        snapshot,
    )

    assert result.returncode == 0
    assert snapshot.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600

    linked_source = tmp_path / "linked.env"
    linked_source.symlink_to(source)
    rejected_link = run_tool(
        RESTORE_STATE,
        "snapshot-private-file",
        "--source",
        linked_source,
        "--output",
        snapshot_root / "linked-copy.env",
        check=False,
    )
    assert rejected_link.returncode == 2

    source.chmod(0o640)
    rejected_permissions = run_tool(
        RESTORE_STATE,
        "snapshot-private-file",
        "--source",
        source,
        "--output",
        snapshot_root / "permissive-copy.env",
        check=False,
    )
    assert rejected_permissions.returncode == 2
    assert "owner-only" in rejected_permissions.stderr


def test_pending_restore_state_generates_a_unique_uncontrolled_challenge(
    tmp_path: Path,
) -> None:
    _manifest_private_key, manifest_public_key = make_manifest_signing_keys(
        tmp_path / "identity"
    )
    _attestation_private_key, attestation_public_key = make_restore_attestation_keys(
        tmp_path / "identity"
    )
    identity = (
        "--backup-id",
        "auris-flow-20260718T120000Z-0123456789ab",
        "--backup-created-at-utc",
        "2026-07-18T12:00:00Z",
        "--source-commit",
        SOURCE_COMMIT,
        "--manifest-sha256",
        "a" * 64,
        "--manifest-signing-key-id",
        key_id(manifest_public_key),
        "--attestation-key-id",
        key_id(attestation_public_key),
        "--pending-at-utc",
        "2026-07-18T13:00:00Z",
    )
    states = [tmp_path / "restore-state-a.json", tmp_path / "restore-state-b.json"]

    for state in states:
        created = run_tool(
            RESTORE_STATE,
            "create-pending",
            "--output",
            state,
            *identity,
            check=False,
        )
        assert created.returncode == 3

    challenges = {
        json.loads(state.read_text(encoding="utf-8"))["restore_challenge"]
        for state in states
    }
    assert len(challenges) == 2
    assert all(re.fullmatch(r"[0-9a-f]{64}", challenge) for challenge in challenges)


def test_restore_state_compares_rfc3339_timestamps_chronologically(
    tmp_path: Path,
) -> None:
    state = tmp_path / "restore-state.json"
    _manifest_private_key, manifest_public_key = make_manifest_signing_keys(state)
    attestation_private_key, attestation_public_key = make_restore_attestation_keys(
        state
    )
    qdrant_evidence, running_images_evidence, readyz_evidence = (
        make_restore_finalize_evidence(tmp_path)
    )
    identity = (
        "--backup-id",
        "auris-flow-20260718T120000Z-0123456789ab",
        "--source-commit",
        SOURCE_COMMIT,
        "--manifest-sha256",
        "a" * 64,
        "--manifest-signing-key-id",
        key_id(manifest_public_key),
        "--attestation-key-id",
        key_id(attestation_public_key),
    )

    pending = run_tool(
        RESTORE_STATE,
        "create-pending",
        "--output",
        state,
        "--backup-created-at-utc",
        "2026-07-18T12:00:00Z",
        "--pending-at-utc",
        "2026-07-18T12:00:00.500000Z",
        *identity,
        check=False,
    )
    assert pending.returncode == 3
    pending_payload = state.read_bytes()

    stale_observation = run_tool(
        RESTORE_STATE,
        "finalize",
        "--state",
        state,
        "--qdrant-evidence",
        qdrant_evidence,
        "--running-images-evidence",
        running_images_evidence,
        "--readyz-evidence",
        readyz_evidence,
        "--private-key",
        attestation_private_key,
        "--public-key",
        attestation_public_key,
        "--observed-at-utc",
        "2026-07-18T12:00:00.250000Z",
        "--completed-at-utc",
        "2026-07-18T12:00:00.750000Z",
        *identity,
        check=False,
    )
    assert stale_observation.returncode == 2
    assert "observation predates" in stale_observation.stderr
    assert state.read_bytes() == pending_payload

    future_observation = run_tool(
        RESTORE_STATE,
        "finalize",
        "--state",
        state,
        "--qdrant-evidence",
        qdrant_evidence,
        "--running-images-evidence",
        running_images_evidence,
        "--readyz-evidence",
        readyz_evidence,
        "--private-key",
        attestation_private_key,
        "--public-key",
        attestation_public_key,
        "--observed-at-utc",
        "2026-07-18T12:00:00.875000Z",
        "--completed-at-utc",
        "2026-07-18T12:00:00.750000Z",
        *identity,
        check=False,
    )
    assert future_observation.returncode == 2
    assert "observation postdates" in future_observation.stderr
    assert state.read_bytes() == pending_payload

    finalized = run_tool(
        RESTORE_STATE,
        "finalize",
        "--state",
        state,
        "--qdrant-evidence",
        qdrant_evidence,
        "--running-images-evidence",
        running_images_evidence,
        "--readyz-evidence",
        readyz_evidence,
        "--private-key",
        attestation_private_key,
        "--public-key",
        attestation_public_key,
        "--observed-at-utc",
        "2026-07-18T12:00:00.625000Z",
        "--completed-at-utc",
        "2026-07-18T12:00:00.750000Z",
        *identity,
        check=False,
    )
    assert finalized.returncode == 0, finalized.stderr


def test_signed_complete_restore_state_rejects_tampering(tmp_path: Path) -> None:
    state = tmp_path / "restore-state.json"
    _manifest_private_key, manifest_public_key = make_manifest_signing_keys(state)
    attestation_private_key, attestation_public_key = make_restore_attestation_keys(
        state
    )
    qdrant_evidence, running_images_evidence, readyz_evidence = (
        make_restore_finalize_evidence(tmp_path)
    )
    identity = (
        "--backup-id",
        "auris-flow-20260718T120000Z-0123456789ab",
        "--source-commit",
        SOURCE_COMMIT,
        "--manifest-sha256",
        "a" * 64,
        "--manifest-signing-key-id",
        key_id(manifest_public_key),
        "--attestation-key-id",
        key_id(attestation_public_key),
    )
    pending = run_tool(
        RESTORE_STATE,
        "create-pending",
        "--output",
        state,
        "--backup-created-at-utc",
        "2026-07-18T12:00:00Z",
        "--pending-at-utc",
        "2026-07-18T13:00:00Z",
        *identity,
        check=False,
    )
    assert pending.returncode == 3
    run_tool(
        RESTORE_STATE,
        "finalize",
        "--state",
        state,
        "--qdrant-evidence",
        qdrant_evidence,
        "--running-images-evidence",
        running_images_evidence,
        "--readyz-evidence",
        readyz_evidence,
        "--private-key",
        attestation_private_key,
        "--public-key",
        attestation_public_key,
        "--observed-at-utc",
        "2026-07-18T13:59:59Z",
        "--completed-at-utc",
        "2026-07-18T14:00:00Z",
        *identity,
    )

    tampered = json.loads(state.read_text(encoding="utf-8"))
    tampered["governed_finalize"]["qdrant_evidence"]["collections"] = {
        "knowledge_chunks": 4
    }
    write_json(state, tampered)
    state.chmod(0o600)

    rejected = run_tool(
        RESTORE_STATE,
        "verify-complete",
        "--state",
        state,
        "--public-key",
        attestation_public_key,
        *identity,
        check=False,
    )
    assert rejected.returncode == 2
    assert "evidence" in rejected.stderr or "signature" in rejected.stderr


def test_restore_state_finalize_is_single_winner_under_concurrency(
    tmp_path: Path,
) -> None:
    state = tmp_path / "restore-state.json"
    _manifest_private_key, manifest_public_key = make_manifest_signing_keys(state)
    attestation_private_key, attestation_public_key = make_restore_attestation_keys(
        state
    )
    qdrant_evidence, running_images_evidence, readyz_evidence = (
        make_restore_finalize_evidence(tmp_path)
    )
    identity = (
        "--backup-id",
        "auris-flow-20260718T120000Z-0123456789ab",
        "--source-commit",
        SOURCE_COMMIT,
        "--manifest-sha256",
        "a" * 64,
        "--manifest-signing-key-id",
        key_id(manifest_public_key),
        "--attestation-key-id",
        key_id(attestation_public_key),
    )
    pending = run_tool(
        RESTORE_STATE,
        "create-pending",
        "--output",
        state,
        "--backup-created-at-utc",
        "2026-07-18T12:00:00Z",
        "--pending-at-utc",
        "2026-07-18T13:00:00Z",
        *identity,
        check=False,
    )
    assert pending.returncode == 3
    command = [
        sys.executable,
        str(RESTORE_STATE),
        "finalize",
        "--state",
        str(state),
        "--qdrant-evidence",
        str(qdrant_evidence),
        "--running-images-evidence",
        str(running_images_evidence),
        "--readyz-evidence",
        str(readyz_evidence),
        "--private-key",
        str(attestation_private_key),
        "--public-key",
        str(attestation_public_key),
        "--observed-at-utc",
        "2026-07-18T13:59:59Z",
        "--completed-at-utc",
        "2026-07-18T14:00:00Z",
        *(str(value) for value in identity),
    ]
    processes = [
        subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _index in range(2)
    ]
    completed = [process.communicate(timeout=10) for process in processes]

    assert sorted(process.returncode for process in processes) == [0, 2]
    assert sum('"status": "complete"' in stdout for stdout, _stderr in completed) == 1
    verified = run_tool(
        RESTORE_STATE,
        "verify-complete",
        "--state",
        state,
        "--public-key",
        attestation_public_key,
        *identity,
    )
    assert json.loads(verified.stdout)["status"] == "complete"


def test_restore_drill_separates_one_shots_and_bounds_compose_commands() -> None:
    source = (SCRIPTS / "verify-backup.sh").read_text(encoding="utf-8")

    assert 'DEADLINE_RUNNER="${REPOSITORY_ROOT}/scripts/run_with_deadline.py"' in source
    assert "compose_drill_with_deadline()" in source
    assert "up --detach --no-deps --wait" in source
    assert '--wait-timeout "${DRILL_WAIT_TIMEOUT}"' in source
    assert "mysql redis minio qdrant" in source
    assert (
        "pull mysql db-bootstrap redis minio minio-volume-init minio-bootstrap"
        in source
    )
    assert "run --rm --no-deps minio-volume-init" in source
    assert "run --rm --no-deps db-bootstrap" in source
    assert "run --rm --no-deps minio-bootstrap" in source
    assert source.index("run --rm --no-deps minio-volume-init") < source.index(
        "up --detach --no-deps --wait"
    )
    assert (
        "up -d --wait mysql db-bootstrap redis minio minio-bootstrap qdrant"
        not in source
    )
    assert 'compose_drill_with_deadline "${DRILL_CLEANUP_TIMEOUT}"' in source
    assert 'local status="$1" cleanup_failed=0' in source
    assert 'if [ "${status}" -eq 0 ] && [ "${cleanup_failed}" -ne 0 ]' in source


@pytest.mark.parametrize(
    "relative_path",
    [
        "RELEASE_CHECKLIST.md",
        "production/README.md",
        "production/deployment-bundle.README.md",
        "doc/runbooks/upgrade-rollback.md",
        "doc/runbooks/release-supply-chain.md",
        "doc/runbooks/backup-restore.md",
    ],
)
def test_operator_docs_do_not_mix_one_shots_into_detached_wait(
    relative_path: str,
) -> None:
    source = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
    normalized = " ".join(source.split())

    assert "up -d --wait mysql db-bootstrap" not in normalized
    assert "up -d mysql db-bootstrap" not in normalized
    assert "up -d --wait`" not in source
    if relative_path == "RELEASE_CHECKLIST.md":
        assert "production/README.md" in source
        assert "foreground" in source
    else:
        assert "run --rm --no-deps db-bootstrap" in normalized
        assert "run --rm --no-deps minio-volume-init" in normalized
        assert "run --rm --no-deps minio-bootstrap" in normalized


def test_backup_manifest_binds_release_metadata_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    root = make_backup_root(tmp_path)
    create_manifest(root)
    document = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

    assert document["schema_version"] == "auris-flow.backup-manifest/v4"
    assert document["source"]["git_commit"] == SOURCE_COMMIT
    assert document["source"]["release_version"] == RELEASE_TAG
    assert document["source"]["release_metadata"] == make_release_metadata()
    assert (
        document["source"]["release_metadata_sha256"]
        == hashlib.sha256(
            (root / "metadata" / "release-metadata.json").read_bytes()
        ).hexdigest()
    )

    metadata_path = root / "metadata" / "release-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["source_commit"] = "f" * 40
    write_json(metadata_path, metadata)
    rejected = verify_manifest(root, check=False)
    assert rejected.returncode == 2
    assert "checksum mismatch" in rejected.stderr


def test_release_bundle_real_assembly_unpack_and_readme_contract(
    tmp_path: Path,
) -> None:
    compose_path = tmp_path / "compose.release.json"
    lock_path = tmp_path / "images.lock.json"
    write_json(
        compose_path,
        {
            "name": "auris-flow",
            "services": {"bff": {"image": TEST_IMAGE}},
            "volumes": {},
        },
    )
    write_json(
        lock_path,
        {
            "schema_version": "auris.release-image-lock.v1",
            "release_tag": RELEASE_TAG,
            "source_commit": SOURCE_COMMIT,
            "images": {"bff": TEST_IMAGE},
        },
    )
    bundle = tmp_path / "deployment"
    assembled = subprocess.run(
        [
            sys.executable,
            RELEASE_BUNDLE,
            "assemble",
            "--repository-root",
            REPOSITORY_ROOT,
            "--output",
            bundle,
            "--rendered-compose",
            compose_path,
            "--image-lock",
            lock_path,
            "--release-tag",
            RELEASE_TAG,
            "--source-commit",
            SOURCE_COMMIT,
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert assembled.returncode == 0, assembled.stderr

    archive_path = tmp_path / "auris-flow-v1.0.0-deployment.tar"
    with tarfile.open(archive_path, "w") as archive:
        archive.add(bundle, arcname="auris-flow-v1.0.0-deployment", recursive=True)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(archive_path, "r:") as archive:
        archive.extractall(extracted, filter="data")
    root = extracted / "auris-flow-v1.0.0-deployment"

    verified = subprocess.run(
        [
            sys.executable,
            root / "scripts/release_bundle.py",
            "verify",
            "--bundle-root",
            root,
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr
    release_metadata_path = root / "production/release-metadata.json"
    release_metadata = json.loads(release_metadata_path.read_text(encoding="utf-8"))
    assert release_metadata["schema_version"] == "auris.release-deployment-metadata.v3"
    manifested_paths = [item["path"] for item in release_metadata["members"]]
    assert manifested_paths == sorted(manifested_paths)
    assert len(manifested_paths) == len(set(manifested_paths))
    assert "production/scripts/restore.sh" in manifested_paths
    assert "production/scripts/finalize-restore.sh" in manifested_paths
    assert "production/backup/backup_restore_evidence.py" in manifested_paths
    assert "production/backup/restore_state.py" in manifested_paths
    assert "scripts/verify_backup_restore_gate.py" in manifested_paths
    assert "doc/runbooks/backup-restore.md" in manifested_paths
    assert "VERSION" in manifested_paths
    assert "production/release-metadata.json" not in manifested_paths
    assert "production/release-metadata.sigstore.json" not in manifested_paths
    assert all(
        set(item) == {"path", "sha256", "type", "mode"}
        and item["type"] == "regular-file"
        and item["mode"] in {"0600", "0644", "0755"}
        for item in release_metadata["members"]
    )
    assert (root / "production/compose.yaml").read_bytes() == compose_path.read_bytes()
    assert (root / "VERSION").read_text(encoding="utf-8") == "1.0.0\n"
    assert not (root / "production/compose.release.json").exists()
    assert not (root / ".git").exists()
    assert (root / "production/compose.oidc-confidential.yaml").is_file()
    assert (root / "production/restore-compatibility.json").is_file()
    assert (root / "scripts/run_with_deadline.py").is_file()

    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "--file production/compose.yaml" in readme
    assert "python3 scripts/release_bundle.py verify --bundle-root ." in readme
    assert "--verify-signature" in readme
    assert "release-metadata.sigstore.json" in readme
    assert "doc/runbooks/backup-restore.md" in readme
    assert "doc/runbooks/key-rotation.md" in readme
    assert "doc/runbooks/security-incident-response.md" in readme
    assert "build/release" not in readme
    assert "compose.release.json" not in readme
    assert "images.lock.env" not in readme

    release_bundle = load_release_bundle()
    assert release_bundle._official_release_workflow_identity("v1.0.0") == (
        "https://github.com/g5n-dev/auris_flow/.github/workflows/"
        "release-images.yml@refs/tags/v1.0.0"
    )
    commands: list[tuple[str, ...]] = []

    def fake_cosign(
        command: tuple[str, ...], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "verified", "")

    canonical_signature = root / "production/release-metadata.sigstore.json"
    assert not canonical_signature.exists()
    with pytest.raises(release_bundle.ReleaseBundleError, match="Sigstore bundle"):
        release_bundle.verify_bundle_signature(root, run=fake_cosign)
    assert commands == []

    downloaded_signature = (
        extracted / "auris-flow-v1.0.0-release-metadata.sigstore.json"
    )
    downloaded_signature.write_text("{}\n", encoding="utf-8")
    shutil.copyfile(downloaded_signature, canonical_signature)
    canonical_signature.chmod(0o444)
    signed = release_bundle.verify_bundle_signature(root, run=fake_cosign)
    assert signed["schema_version"] == "auris.release-deployment-metadata.v3"
    assert commands[0][0:2] == ("cosign", "verify-blob")
    assert (
        "https://github.com/g5n-dev/auris_flow/.github/workflows/"
        "release-images.yml@refs/tags/v1.0.0"
    ) in commands[0]

    with pytest.raises(
        release_bundle.ReleaseBundleError,
        match="outside the signed restore compatibility policy",
    ):
        release_bundle.verify_restore_source(
            bundle_root=root,
            backup_release_tag="v0.9.0",
            backup_source_commit="f" * 40,
            backup_metadata_sha256="e" * 64,
        )

    for relative_path in (
        "production/scripts/restore.sh",
        "doc/runbooks/backup-restore.md",
    ):
        target = root / relative_path
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\nrelease-bundle-tamper\n")
            with pytest.raises(
                release_bundle.ReleaseBundleError,
                match="bundle member checksum",
            ):
                release_bundle.verify_bundle(root)
        finally:
            target.write_bytes(original)

    executable = root / "production/scripts/restore.sh"
    original_mode = stat.S_IMODE(executable.stat().st_mode)
    try:
        executable.chmod(original_mode ^ stat.S_IXUSR)
        with pytest.raises(
            release_bundle.ReleaseBundleError,
            match="bundle member mode",
        ):
            release_bundle.verify_bundle(root)
    finally:
        executable.chmod(original_mode)

    hard_link_member = root / "doc/runbooks/security-incident-response.md"
    parked_hard_link_member = tmp_path / "security-incident-response.parked.md"
    hard_link_member.rename(parked_hard_link_member)
    os.link(parked_hard_link_member, hard_link_member)
    try:
        with pytest.raises(
            release_bundle.ReleaseBundleError,
            match="hard links are forbidden",
        ):
            release_bundle.verify_bundle(root)
    finally:
        hard_link_member.unlink()
        parked_hard_link_member.rename(hard_link_member)

    runbook_directory = root / "doc/runbooks"
    original_directory_mode = stat.S_IMODE(runbook_directory.stat().st_mode)
    try:
        runbook_directory.chmod(0o777)
        with pytest.raises(
            release_bundle.ReleaseBundleError,
            match="bundle directory mode",
        ):
            release_bundle.verify_bundle(root)
    finally:
        runbook_directory.chmod(original_directory_mode)

    missing = root / "doc/runbooks/key-rotation.md"
    parked = tmp_path / "key-rotation.parked.md"
    missing.rename(parked)
    try:
        with pytest.raises(
            release_bundle.ReleaseBundleError,
            match="missing bundle member",
        ):
            release_bundle.verify_bundle(root)
    finally:
        parked.rename(missing)

    unexpected = root / "unexpected-release-member.txt"
    unexpected.write_text("not signed\n", encoding="utf-8")
    try:
        with pytest.raises(
            release_bundle.ReleaseBundleError,
            match="unexpected bundle member",
        ):
            release_bundle.verify_bundle(root)
    finally:
        unexpected.unlink()

    original_metadata = release_metadata_path.read_bytes()
    for mutation, expected_error in (
        (
            lambda document: document["members"].append(document["members"][0]),
            "duplicate bundle member path",
        ),
        (
            lambda document: document["members"][0].__setitem__(
                "path", "../escaped-release-member"
            ),
            "unsafe bundle member path",
        ),
    ):
        mutated_metadata = json.loads(original_metadata)
        mutation(mutated_metadata)
        write_json(release_metadata_path, mutated_metadata)
        try:
            with pytest.raises(release_bundle.ReleaseBundleError, match=expected_error):
                release_bundle.verify_bundle(root)
        finally:
            release_metadata_path.write_bytes(original_metadata)


def test_release_bundle_rejects_certificate_identity_override() -> None:
    release_bundle = load_release_bundle()

    with pytest.raises(SystemExit):
        release_bundle.parse_args(
            [
                "verify",
                "--verify-signature",
                "--certificate-identity",
                (
                    "https://github.com/attacker/repo/.github/workflows/"
                    "release-images.yml@refs/tags/v1.0.0"
                ),
            ]
        )


def test_running_image_validation_requires_config_and_content_digest() -> None:
    release_bundle = load_release_bundle()
    repo_digest = "ghcr.io/auris-flow/auris-flow-bff@sha256:" + ("a" * 64)
    release_bundle.validate_running_image(
        expected=TEST_IMAGE,
        configured=TEST_IMAGE,
        image_id="sha256:" + ("d" * 64),
        repo_digests=[repo_digest],
    )

    with pytest.raises(release_bundle.ReleaseBundleError, match="unexpected image"):
        release_bundle.validate_running_image(
            expected=TEST_IMAGE,
            configured="ghcr.io/auris-flow/auris-flow-bff:v1.0.0",
            image_id="sha256:" + ("d" * 64),
            repo_digests=[repo_digest],
        )
    with pytest.raises(release_bundle.ReleaseBundleError, match="release digest"):
        release_bundle.validate_running_image(
            expected=TEST_IMAGE,
            configured=TEST_IMAGE,
            image_id="sha256:" + ("d" * 64),
            repo_digests=["ghcr.io/auris-flow/auris-flow-bff@sha256:" + ("e" * 64)],
        )


def test_running_image_evidence_covers_every_running_release_service(
    tmp_path: Path,
) -> None:
    release_bundle = load_release_bundle()
    images = {**AUTHORITY_IMAGES, "bff": TEST_IMAGE}
    compose_path = tmp_path / "compose.json"
    lock_path = tmp_path / "images.lock.json"
    write_json(
        compose_path,
        {"services": {service: {"image": image} for service, image in images.items()}},
    )
    write_json(
        lock_path,
        {
            "schema_version": "auris.release-image-lock.v1",
            "release_tag": RELEASE_TAG,
            "source_commit": SOURCE_COMMIT,
            "images": images,
        },
    )
    bundle = tmp_path / "bundle"
    release_bundle.assemble_bundle(
        repository_root=REPOSITORY_ROOT,
        output_root=bundle,
        rendered_compose=compose_path,
        image_lock_file=lock_path,
        release_tag=RELEASE_TAG,
        source_commit=SOURCE_COMMIT,
    )
    image_ids = {
        service: f"sha256:{index:064x}"
        for index, service in enumerate(sorted(images), start=1)
    }

    def fake_docker(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if "compose" in command:
            records = [
                {"ID": f"container-{service}", "Service": service, "State": "running"}
                for service in sorted(images)
            ]
            return subprocess.CompletedProcess(command, 0, json.dumps(records), "")
        if command[1] == "inspect":
            service = command[2].removeprefix("container-")
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    [
                        {
                            "Config": {"Image": images[service]},
                            "Image": image_ids[service],
                        }
                    ]
                ),
                "",
            )
        service = next(
            service for service, image_id in image_ids.items() if image_id == command[3]
        )
        reference = images[service]
        repository = release_bundle._repository_without_tag(reference)
        digest = reference.rsplit("@", 1)[1]
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps([{"RepoDigests": [f"{repository}@{digest}"]}]),
            "",
        )

    evidence = release_bundle.verify_running_images(
        bundle_root=bundle,
        project_directory=bundle / "production",
        env_file=bundle / "production/.env.example",
        project_name="auris-flow",
        services=("mysql", "minio", "qdrant", "redis"),
        include_all_running=True,
        run=fake_docker,
    )

    assert evidence["verification_scope"] == "all-running-release-services"
    assert evidence["images"] == images
    assert evidence["images"]["bff"] == TEST_IMAGE
