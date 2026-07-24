from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]
EMITTER = ROOT / "production" / "backup" / "backup_restore_evidence.py"
VERIFY_BACKUP = ROOT / "production" / "scripts" / "verify-backup.sh"
SOURCE_COMMIT = "1" * 40
SHA256 = "a" * 64


def _load_emitter() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "backup_restore_evidence", EMITTER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _signed_manifest() -> dict[str, object]:
    return {
        "schema_version": "auris-flow.backup-manifest/v4",
        "backup_id": "auris-flow-20260724T080000Z-111111111111",
        "created_at_utc": "2026-07-24T08:00:00Z",
        "source": {
            "git_commit": SOURCE_COMMIT,
            "release_version": "v1.0.0-rc.1",
            "release_metadata": {
                "schema_version": "auris.release-deployment-metadata.v3",
                "release_tag": "v1.0.0-rc.1",
                "source_commit": SOURCE_COMMIT,
                "compose": {
                    "path": "production/compose.yaml",
                    "sha256": SHA256,
                },
                "image_lock": {
                    "path": "production/images.lock.json",
                    "sha256": SHA256,
                },
                "restore_policy": {
                    "path": "production/restore-compatibility.json",
                    "sha256": SHA256,
                },
                "images": {"mysql": f"example.invalid/mysql@sha256:{SHA256}"},
                "members": [],
            },
            "release_metadata_sha256": SHA256,
            "running_images": {},
            "running_images_sha256": SHA256,
        },
        "storage_boundary": {
            "mode": "ephemeral-ci-drill",
            "operator_assertion": "ephemeral-runner-recovery-drill-not-retained",
            "contains_sensitive_data": True,
            "repository_never_contains_backup_payloads": True,
        },
        "tenant_independent_counts": {
            "mysql": {
                "tables": {
                    "auris_flow.json_resources": 3,
                    "auris_flow.jobs": 2,
                },
                "rows_total": 5,
            },
            "minio": {
                "object_keys": 1,
                "versions": 2,
                "delete_markers": 0,
                "content_bytes": 4096,
            },
            "qdrant": {
                "included": True,
                "collections": {"auris-flow-audio": 2},
                "points_total": 2,
            },
            "redis": {"included": False, "authoritative": False},
        },
    }


def _verified_manifest() -> dict[str, object]:
    return {
        "status": "verified",
        "backup_id": "auris-flow-20260724T080000Z-111111111111",
        "artifact_count": 12,
        "git_commit": SOURCE_COMMIT,
        "release_version": "v1.0.0-rc.1",
        "release_metadata_sha256": SHA256,
        "running_images_sha256": SHA256,
        "created_at_utc": "2026-07-24T08:00:00Z",
        "manifest_sha256": SHA256,
        "signing_key_id": SHA256,
        "restore_attestation_key_id": "b" * 64,
    }


def _docker_context() -> list[dict[str, object]]:
    return [
        {
            "Name": "default",
            "Endpoints": {
                "docker": {"Host": "unix:///var/run/docker.sock"}
            },
        }
    ]


def _docker_info() -> dict[str, object]:
    return {
        "OSType": "linux",
        "OperatingSystem": "Ubuntu 24.04.2 LTS",
        "SecurityOptions": ["name=apparmor", "name=seccomp,profile=builtin"],
    }


def _build(module: ModuleType) -> dict[str, object]:
    return _build_with_manifest(module, _signed_manifest())


def _build_with_manifest(
    module: ModuleType,
    manifest: dict[str, object],
) -> dict[str, object]:
    return module.build_evidence(
        root=ROOT,
        platform_name="linux",
        signed_manifest=manifest,
        verified_manifest=_verified_manifest(),
        docker_context=_docker_context(),
        docker_info=_docker_info(),
        drill_project="auris-flow-restore-drill-a1b2c3d4e5f6",
        restore_subnet="172.31.49.0/24",
        edge_internal_ip="172.31.49.10",
        backup_verification_started_at="2026-07-24T08:00:01Z",
        backup_verification_completed_at="2026-07-24T08:00:04Z",
        restore_started_at="2026-07-24T08:00:05Z",
        restore_completed_at="2026-07-24T08:02:05Z",
        cleanup_started_at="2026-07-24T08:02:06Z",
        cleanup_completed_at="2026-07-24T08:02:11Z",
        verified_at="2026-07-24T08:02:12Z",
    )


def test_builds_commit_bound_native_linux_evidence_from_signed_backup() -> None:
    module = _load_emitter()

    evidence = _build(module)

    assert evidence["source_commit"] == SOURCE_COMMIT
    assert evidence["host"] == {
        "platform": "linux",
        "native_linux": True,
        "docker_context": "default",
        "docker_ostype": "linux",
        "docker_operating_system": "Ubuntu 24.04.2 LTS",
        "rootless": False,
    }
    backup = evidence["backup"]
    assert isinstance(backup, dict)
    assert backup["verification_duration_seconds"] == 3
    assert backup["storage_boundary"] == "ephemeral-ci-drill"
    assert backup["off_host_retained"] is False
    assert backup["authority_counts"] == {
        "mysql": {
            "business_rows_total": 3,
            "tables_total": 2,
            "rows_total": 5,
        },
        "minio": {
            "object_keys": 1,
            "versions": 2,
            "content_bytes": 4096,
        },
        "qdrant": {"collections": 1, "points_total": 2},
    }
    bindings = evidence["tool_bindings"]
    assert isinstance(bindings, dict)
    assert (
        "production/backup/backup_restore_evidence.py" in bindings
    )


@pytest.mark.parametrize(
    ("context", "info", "error_fragment"),
    [
        (
            [{"Name": "default", "Endpoints": {"docker": {"Host": "tcp://host:2375"}}}],
            _docker_info(),
            "rootful Docker socket",
        ),
        (
            _docker_context(),
            {**_docker_info(), "OperatingSystem": "Docker Desktop"},
            "VM-backed",
        ),
        (
            _docker_context(),
            {**_docker_info(), "SecurityOptions": ["name=rootless"]},
            "rootless",
        ),
    ],
)
def test_rejects_remote_desktop_and_rootless_docker(
    context: list[dict[str, object]],
    info: dict[str, object],
    error_fragment: str,
) -> None:
    module = _load_emitter()

    with pytest.raises(module.EvidenceError, match=error_fragment):
        module.build_evidence(
            root=ROOT,
            platform_name="linux",
            signed_manifest=_signed_manifest(),
            verified_manifest=_verified_manifest(),
            docker_context=context,
            docker_info=info,
            drill_project="auris-flow-restore-drill-a1b2c3d4e5f6",
            restore_subnet="172.31.49.0/24",
            edge_internal_ip="172.31.49.10",
            backup_verification_started_at="2026-07-24T08:00:01Z",
            backup_verification_completed_at="2026-07-24T08:00:04Z",
            restore_started_at="2026-07-24T08:00:05Z",
            restore_completed_at="2026-07-24T08:02:05Z",
            cleanup_started_at="2026-07-24T08:02:06Z",
            cleanup_completed_at="2026-07-24T08:02:11Z",
            verified_at="2026-07-24T08:02:12Z",
        )


def test_rejects_manifest_summary_mismatch_and_empty_authority_data() -> None:
    module = _load_emitter()
    manifest = _signed_manifest()
    source = manifest["source"]
    counts = manifest["tenant_independent_counts"]
    assert isinstance(source, dict)
    assert isinstance(counts, dict)
    source["git_commit"] = "2" * 40
    mysql = counts["mysql"]
    assert isinstance(mysql, dict)
    mysql["rows_total"] = 0

    with pytest.raises(module.EvidenceError):
        module.build_evidence(
            root=ROOT,
            platform_name="linux",
            signed_manifest=manifest,
            verified_manifest=_verified_manifest(),
            docker_context=_docker_context(),
            docker_info=_docker_info(),
            drill_project="auris-flow-restore-drill-a1b2c3d4e5f6",
            restore_subnet="172.31.49.0/24",
            edge_internal_ip="172.31.49.10",
            backup_verification_started_at="2026-07-24T08:00:01Z",
            backup_verification_completed_at="2026-07-24T08:00:04Z",
            restore_started_at="2026-07-24T08:00:05Z",
            restore_completed_at="2026-07-24T08:02:05Z",
            cleanup_started_at="2026-07-24T08:02:06Z",
            cleanup_completed_at="2026-07-24T08:02:11Z",
            verified_at="2026-07-24T08:02:12Z",
        )


def test_rejects_false_or_inconsistent_storage_boundary_claims() -> None:
    module = _load_emitter()
    manifest = _signed_manifest()
    boundary = manifest["storage_boundary"]
    assert isinstance(boundary, dict)
    boundary["mode"] = "encrypted-external"

    with pytest.raises(module.EvidenceError, match="storage boundary"):
        _build_with_manifest(module, manifest)


def test_rejects_migration_metadata_without_business_resource_rows() -> None:
    module = _load_emitter()
    manifest = _signed_manifest()
    counts = manifest["tenant_independent_counts"]
    assert isinstance(counts, dict)
    mysql = counts["mysql"]
    assert isinstance(mysql, dict)
    mysql["tables"] = {
        "auris_flow.alembic_version": 1,
        "dagster.runs": 4,
    }
    mysql["rows_total"] = 5

    with pytest.raises(
        module.EvidenceError,
        match="json_resources business rows",
    ):
        module.build_evidence(
            root=ROOT,
            platform_name="linux",
            signed_manifest=manifest,
            verified_manifest=_verified_manifest(),
            docker_context=_docker_context(),
            docker_info=_docker_info(),
            drill_project="auris-flow-restore-drill-a1b2c3d4e5f6",
            restore_subnet="172.31.49.0/24",
            edge_internal_ip="172.31.49.10",
            backup_verification_started_at="2026-07-24T08:00:01Z",
            backup_verification_completed_at="2026-07-24T08:00:04Z",
            restore_started_at="2026-07-24T08:00:05Z",
            restore_completed_at="2026-07-24T08:02:05Z",
            cleanup_started_at="2026-07-24T08:02:06Z",
            cleanup_completed_at="2026-07-24T08:02:11Z",
            verified_at="2026-07-24T08:02:12Z",
        )


def test_atomic_publish_validates_and_refuses_to_replace_existing_file(
    tmp_path: Path,
) -> None:
    module = _load_emitter()
    evidence = _build(module)
    output = tmp_path / "backup-restore-gate.json"

    module.publish_validated_evidence(
        evidence=evidence,
        root=ROOT,
        output=output,
    )

    assert json.loads(output.read_text(encoding="utf-8")) == evidence
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(module.EvidenceError, match="already exists"):
        module.publish_validated_evidence(
            evidence=evidence,
            root=ROOT,
            output=output,
        )
    assert not list(tmp_path.glob(".backup-restore-gate.json.*"))


def test_atomic_publish_removes_link_when_directory_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_emitter()
    evidence = _build(module)
    output = tmp_path / "backup-restore-gate.json"
    real_fsync = module.os.fsync
    calls = 0

    def fail_directory_fsync(file_descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory fsync failure")
        real_fsync(file_descriptor)

    monkeypatch.setattr(module.os, "fsync", fail_directory_fsync)

    with pytest.raises(module.EvidenceError):
        module.publish_validated_evidence(
            evidence=evidence,
            root=ROOT,
            output=output,
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".backup-restore-gate.json.*"))


def test_evidence_output_requires_drill_and_cleanup_before_backup_access(
    tmp_path: Path,
) -> None:
    output = tmp_path / "backup-restore-gate.json"

    result = subprocess.run(
        [
            VERIFY_BACKUP,
            "--backup",
            "/missing-backup",
            "--evidence-output",
            output,
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert "--evidence-output requires --drill and --cleanup-on-success" in result.stderr
    assert not output.exists()


def test_verify_backup_publishes_only_after_exact_cleanup_is_proven() -> None:
    source = VERIFY_BACKUP.read_text(encoding="utf-8")

    assert "--evidence-output ABSOLUTE_FILE" in source
    cleanup = source.index('down --volumes --remove-orphans')
    no_containers = source.index(
        'label=com.docker.compose.project=${DRILL_PROJECT}'
    )
    no_volumes = source.index(
        'volume ls --quiet --filter'
    )
    no_networks = source.index(
        'network ls --quiet --filter'
    )
    emit = source.index('"${BACKUP_EVIDENCE_TOOL}" emit-gate')
    assert cleanup < no_containers < no_volumes < no_networks < emit
    assert "verify_backup_restore_gate.py" in source


def test_verify_backup_allocates_one_non_overlapping_network_for_all_drill_steps() -> (
    None
):
    source = VERIFY_BACKUP.read_text(encoding="utf-8")

    allocation = source.index('"${RESTORE_NETWORK_ALLOCATOR}"')
    subnet_export = source.index(
        'export AURIS_INTERNAL_SUBNET="${DRILL_INTERNAL_SUBNET}"'
    )
    edge_export = source.index(
        'export AURIS_EDGE_INTERNAL_IP="${DRILL_EDGE_INTERNAL_IP}"'
    )
    compose = source.index("compose_drill_with_deadline()")
    restore = source.index('"${SCRIPT_DIR}/restore.sh"')

    assert "docker_networks_json" in source
    assert 'network inspect "${network_ids[@]}"' in source
    assert "ip -json -4 route show table all" in source
    assert "--host-route" in source
    assert allocation < subnet_export < compose < restore
    assert allocation < edge_export < compose
    assert '--restore-subnet "${DRILL_INTERNAL_SUBNET}"' in source
    assert '--edge-internal-ip "${DRILL_EDGE_INTERNAL_IP}"' in source
