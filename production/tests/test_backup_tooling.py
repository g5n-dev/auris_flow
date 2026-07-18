from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKUP_TOOLS = REPOSITORY_ROOT / "production" / "backup"
SCRIPTS = REPOSITORY_ROOT / "production" / "scripts"
MANIFEST = BACKUP_TOOLS / "manifest.py"
MINIO = BACKUP_TOOLS / "minio_versions.py"
QDRANT = BACKUP_TOOLS / "qdrant_snapshots.py"
MYSQL_DUMP = BACKUP_TOOLS / "mysql_dump.py"


def run_tool(
    *arguments: object, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, *(str(argument) for argument in arguments)],
        capture_output=True,
        check=False,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr)
    return result


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def make_minio_plan(path: Path) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "auris-flow.minio-version-plan/v1",
        "bucket": "auris-flow",
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
    write_json(
        root / "qdrant" / "snapshots.json",
        {
            "schema_version": "auris-flow.qdrant-snapshots/v1",
            "qdrant_version": "1.14.1",
            "authority": "derived-rebuildable-from-mysql-and-minio",
            "collections": [],
            "aliases": [],
        },
    )
    return root


def create_manifest(root: Path) -> None:
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
        "0123456789abcdef0123456789abcdef01234567",
        "--release-version",
        "v1.0.0",
        "--counts",
        root / "metadata" / "counts.json",
        "--tool-versions",
        root / "metadata" / "tool-versions.json",
    )


def test_manifest_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    root = make_backup_root(tmp_path)
    create_manifest(root)
    verified = run_tool(MANIFEST, "verify", "--root", root)
    assert json.loads(verified.stdout)["status"] == "verified"

    (root / "mysql" / "table-counts.tsv").write_text(
        "auris_flow.audit_logs\t4\n", encoding="utf-8"
    )
    rejected = run_tool(MANIFEST, "verify", "--root", root, check=False)
    assert rejected.returncode == 2
    assert "checksum mismatch" in rejected.stderr


def test_offline_verification_wrapper_accepts_a_complete_backup(tmp_path: Path) -> None:
    root = make_backup_root(tmp_path)
    create_manifest(root)
    result = subprocess.run(
        [SCRIPTS / "verify-backup.sh", "--backup", root],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Offline backup verification passed" in result.stdout


def test_manifest_rejects_symlinks_and_unexpected_files(tmp_path: Path) -> None:
    root = make_backup_root(tmp_path)
    create_manifest(root)
    (root / "unexpected.txt").write_text("not declared\n", encoding="utf-8")
    rejected = run_tool(MANIFEST, "verify", "--root", root, check=False)
    assert rejected.returncode == 2
    assert "artifact set mismatch" in rejected.stderr

    (root / "unexpected.txt").unlink()
    target = tmp_path / "target"
    target.write_text("x", encoding="utf-8")
    os.symlink(target, root / "unsafe-link")
    rejected_link = run_tool(MANIFEST, "verify", "--root", root, check=False)
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
                    version="old-content",
                    timestamp="2026-07-18T12:00:00Z",
                    size=7,
                    etag="abcdef",
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
    assert document["summary"] == {
        "content_bytes": 7,
        "delete_markers": 1,
        "object_keys": 1,
        "versions": 2,
    }

    restore_shell = tmp_path / "restore.sh"
    run_tool(MINIO, "emit-restore-shell", "--plan", plan, "--output", restore_shell)
    subprocess.run(["bash", "-n", restore_shell], check=True)
    shell_text = restore_shell.read_text(encoding="utf-8")
    assert "voice'\"'\"'s.wav" in shell_text
    assert "mc --config-dir /tmp/auris-flow-mc rm --quiet --force" in shell_text

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
                    version="target-content",
                    timestamp="2026-07-18T13:00:00Z",
                    size=7,
                    etag="abcdef",
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    compared = run_tool(MINIO, "compare-listing", "--plan", plan, "--listing", restored)
    assert json.loads(compared.stdout)["status"] == "verified"

    restored.write_text(
        restored.read_text(encoding="utf-8").replace("abcdef", "changed")
    )
    mismatch = run_tool(
        MINIO, "compare-listing", "--plan", plan, "--listing", restored, check=False
    )
    assert mismatch.returncode == 2


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
            "schema_version": "auris-flow.qdrant-snapshots/v1",
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


@pytest.mark.parametrize("script_name", ["backup.sh", "restore.sh", "verify-backup.sh"])
def test_shell_scripts_are_strict_and_parse(script_name: str) -> None:
    script = SCRIPTS / script_name
    subprocess.run(["bash", "-n", script], check=True)
    source = script.read_text(encoding="utf-8")
    assert "set -euo pipefail" in source
    assert "rm -rf" not in source
    assert "/Users/" not in source
    assert "/home/" not in source


def test_backup_and_restore_encode_authority_and_fail_closed_invariants() -> None:
    backup = (SCRIPTS / "backup.sh").read_text(encoding="utf-8")
    assert "--single-transaction" in backup
    assert "--routines --triggers --events" in backup
    assert "--storage-boundary must explicitly be encrypted-external" in backup
    assert "writer service" in backup
    assert "mc ls --recursive --versions" in backup
    assert "auris_backup_last_success_timestamp_seconds" in backup
    assert "auris_backup.prom" in backup

    restore = (SCRIPTS / "restore.sh").read_text(encoding="utf-8")
    assert (
        restore.index('RESTORE_STEP="mysql-authority"')
        < restore.index('RESTORE_STEP="minio-authority"')
        < restore.index('RESTORE_STEP="qdrant-derived-index"')
    )
    assert "target MySQL contains rows" in restore
    assert "target MinIO bucket contains object versions" in restore
    assert "target Qdrant contains collections" in restore
    assert "Redis was not restored" in restore
