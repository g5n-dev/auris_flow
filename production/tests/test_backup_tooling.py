from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKUP_TOOLS = REPOSITORY_ROOT / "production" / "backup"
SCRIPTS = REPOSITORY_ROOT / "production" / "scripts"
MANIFEST = BACKUP_TOOLS / "manifest.py"
MINIO = BACKUP_TOOLS / "minio_versions.py"
QDRANT = BACKUP_TOOLS / "qdrant_snapshots.py"
MYSQL_DUMP = BACKUP_TOOLS / "mysql_dump.py"
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


def test_restore_snapshot_is_no_follow_private_and_source_independent(
    tmp_path: Path,
) -> None:
    source = make_backup_root(tmp_path)
    create_manifest(source)
    snapshot_root = tmp_path / "snapshot-root"
    snapshot_root.mkdir(mode=0o700)

    created = run_tool(
        MANIFEST,
        "snapshot",
        "--source",
        source,
        "--snapshot-root",
        snapshot_root,
    )
    assert json.loads(created.stdout)["status"] == "created"
    snapshot = snapshot_root / "backup"
    assert run_tool(MANIFEST, "verify", "--root", snapshot).returncode == 0

    (source / "mysql" / "table-counts.tsv").write_text(
        "auris_flow.audit_logs\t999\n", encoding="utf-8"
    )
    assert run_tool(MANIFEST, "verify", "--root", snapshot).returncode == 0
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


def test_restore_snapshot_rejects_symlinked_backup_members(tmp_path: Path) -> None:
    source = make_backup_root(tmp_path)
    target = tmp_path / "outside"
    target.write_text("outside\n", encoding="utf-8")
    os.symlink(target, source / "unsafe-link")
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
    assert "symlink or special file" in rejected.stderr


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
    assert "verify-running-images" in backup
    assert "release-metadata.sigstore.json" in backup
    assert "--all-running-release-services" in backup
    assert "--verify-signature" in backup
    assert "db-bootstrap minio-bootstrap identity-bootstrap" in backup
    assert '--project-name "${PRODUCTION_PROJECT_NAME}"' in backup
    assert '--docker-context "${DOCKER_CONTEXT_NAME}"' in backup
    assert "paths_overlap" in backup
    assert "AURIS_SOURCE_COMMIT is forbidden" in backup
    assert "git -C" not in backup
    assert 'COMPOSE_FILE="${PRODUCTION_ROOT}/compose.yaml"' in backup

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
    assert "release_bundle.py" in restore
    assert "identity" in restore
    assert "--allow-release-migration-from" in restore
    assert "verify-restore-source" in restore
    assert 'manifest.py" snapshot' in restore
    assert 'BACKUP_ROOT="${RESTORE_SNAPSHOT_ROOT}/backup"' in restore
    assert "git -C" not in restore
    assert "verify-running-images" in restore
    assert "--all-running-release-services" in restore
    assert "--verify-signature" in restore
    assert "db-bootstrap minio-bootstrap identity-bootstrap" in restore
    assert '--project-name "${PRODUCTION_PROJECT_NAME}"' in restore
    assert '--docker-context "${DOCKER_CONTEXT_NAME}"' in restore
    assert "paths_overlap" in restore

    verify_backup = (SCRIPTS / "verify-backup.sh").read_text(encoding="utf-8")
    assert 'compose_drill_with_deadline "${DRILL_PULL_TIMEOUT}"' in verify_backup
    assert "compose_drill build" not in verify_backup
    assert "verify-running-images" in verify_backup
    assert "--verify-signature" in verify_backup
    assert '--project-name "${DRILL_PROJECT}"' in verify_backup
    assert '--docker-context "${DOCKER_CONTEXT_NAME}"' in verify_backup


def test_restore_drill_separates_one_shots_and_bounds_compose_commands() -> None:
    source = (SCRIPTS / "verify-backup.sh").read_text(encoding="utf-8")

    assert 'DEADLINE_RUNNER="${REPOSITORY_ROOT}/scripts/run_with_deadline.py"' in source
    assert "compose_drill_with_deadline()" in source
    assert "up --detach --no-deps --wait" in source
    assert '--wait-timeout "${DRILL_WAIT_TIMEOUT}"' in source
    assert "mysql redis minio qdrant" in source
    assert "run --rm --no-deps db-bootstrap" in source
    assert "run --rm --no-deps minio-bootstrap" in source
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
        assert "run --rm --no-deps minio-bootstrap" in normalized


def test_backup_manifest_binds_release_metadata_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    root = make_backup_root(tmp_path)
    create_manifest(root)
    document = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

    assert document["schema_version"] == "auris-flow.backup-manifest/v2"
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
    rejected = run_tool(MANIFEST, "verify", "--root", root, check=False)
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
    assert "doc/runbooks/backup-restore.md" in manifested_paths
    assert "production/release-metadata.json" not in manifested_paths
    assert "production/release-metadata.sigstore.json" not in manifested_paths
    assert all(
        set(item) == {"path", "sha256", "type", "mode"}
        and item["type"] == "regular-file"
        and item["mode"] in {"0600", "0644", "0755"}
        for item in release_metadata["members"]
    )
    assert (root / "production/compose.yaml").read_bytes() == compose_path.read_bytes()
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
        "https://github.com/auris-flow/auris-flow/.github/workflows/"
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
