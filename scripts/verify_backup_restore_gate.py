#!/usr/bin/env python3
"""Validate real, commit-bound backup/restore release-gate evidence.

The validator deliberately does not create evidence.  The producer must be the
native-Linux restore drill in ``production/scripts/verify-backup.sh`` after a
signed backup has been restored into a new empty Compose project and that
project (including its volumes) has been removed successfully.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_SCHEMA = "auris.backup-restore-gate.v1"
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RELEASE_TAG_PATTERN = re.compile(
    r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-rc\.[1-9]\d*)?$"
)
BACKUP_ID_PATTERN = re.compile(r"^auris-flow-\d{8}T\d{6}Z-[0-9a-f]{12}$")
DRILL_PROJECT_PATTERN = re.compile(r"^auris-flow-restore-drill-[0-9a-f]{12}$")
WINDOWS_ABSOLUTE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
VIRTUALIZED_DOCKER_MARKERS = (
    "docker desktop",
    "orbstack",
    "colima",
    "rancher desktop",
)
OFFICIAL_RELEASE_WORKFLOW_PREFIX = (
    "https://github.com/g5n-dev/auris_flow/.github/workflows/"
    "release-images.yml@refs/tags/"
)
SIGSTORE_ISSUER = "https://token.actions.githubusercontent.com"
MAX_JSON_BYTES = 1024 * 1024
MAX_RELEASE_FILE_BYTES = 16 * 1024 * 1024
SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "private_key",
    "credential",
)
TOOL_BINDING_PATHS = (
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
TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "source_commit",
        "execution_environment",
        "producer",
        "release",
        "host",
        "backup",
        "restore",
        "cleanup",
        "tool_bindings",
        "verified_at",
    }
)


class FormalEvidenceError(RuntimeError):
    """A formal evidence trust or release-binding verification failure."""


def _exact_fields(
    value: object,
    expected: frozenset[str],
    *,
    label: str,
    errors: list[str],
) -> Mapping[str, Any] | None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return None
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        errors.append(f"{label} fields are invalid ({'; '.join(details)})")
    return value


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_timestamp(
    value: object, *, label: str, errors: list[str]
) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        errors.append(f"{label} must be an RFC3339 UTC timestamp ending in Z")
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        errors.append(f"{label} must be a valid RFC3339 UTC timestamp")
        return None
    if parsed.microsecond:
        errors.append(f"{label} must use whole-second precision")
        return None
    return parsed


def _validate_interval(
    value: Mapping[str, Any],
    *,
    label: str,
    errors: list[str],
    prefix: str = "",
) -> tuple[datetime | None, datetime | None]:
    started_field = f"{prefix}started_at"
    completed_field = f"{prefix}completed_at"
    duration_field = f"{prefix}duration_seconds"
    started = _parse_timestamp(
        value.get(started_field), label=f"{label}.{started_field}", errors=errors
    )
    completed = _parse_timestamp(
        value.get(completed_field), label=f"{label}.{completed_field}", errors=errors
    )
    duration = value.get(duration_field)
    if not _nonnegative_int(duration):
        errors.append(f"{label}.{duration_field} must be a non-negative integer")
    if started is not None and completed is not None:
        actual_duration = int((completed - started).total_seconds())
        if actual_duration < 0:
            errors.append(f"{label} time ordering is invalid")
        elif _nonnegative_int(duration) and duration != actual_duration:
            errors.append(f"{label}.{duration_field} does not match its timestamps")
    return started, completed


def _read_regular_bytes(
    path: Path,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} must be a regular file and not a symlink") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ValueError(f"{label} must be a bounded regular file")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(payload) > max_bytes:
            raise ValueError(f"{label} exceeds the size limit")
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"{label} changed while it was read")
        return payload
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(
        _read_regular_bytes(
            path,
            max_bytes=MAX_RELEASE_FILE_BYTES,
            label=f"bound file {path.name}",
        )
    ).hexdigest()


def _scan_for_sensitive_content(
    value: object,
    *,
    path: str,
    errors: list[str],
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                errors.append(f"evidence contains a sensitive field: {path}.{key}")
            _scan_for_sensitive_content(child, path=f"{path}.{key}", errors=errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_for_sensitive_content(child, path=f"{path}[{index}]", errors=errors)
    elif isinstance(value, str):
        if value.startswith("/") or WINDOWS_ABSOLUTE_PATTERN.match(value):
            errors.append(f"evidence contains an absolute path: {path}")


def validate_evidence(
    evidence: object,
    *,
    root: Path,
    expected_commit: str,
    expected_release_tag: str | None = None,
) -> list[str]:
    """Return all fail-closed validation errors for one evidence document."""

    errors: list[str] = []
    if not COMMIT_PATTERN.fullmatch(expected_commit):
        return ["expected_commit must be a complete lowercase Git object id"]
    if expected_release_tag is not None and not RELEASE_TAG_PATTERN.fullmatch(
        expected_release_tag
    ):
        return ["expected_release_tag must be a supported SemVer release tag"]
    document = _exact_fields(
        evidence, TOP_LEVEL_FIELDS, label="backup/restore evidence", errors=errors
    )
    if document is None:
        return errors
    _scan_for_sensitive_content(document, path="$", errors=errors)

    if document.get("schema_version") != EVIDENCE_SCHEMA:
        errors.append(f"schema_version must be {EVIDENCE_SCHEMA}")
    if document.get("status") != "ok":
        errors.append("status must be ok")
    if document.get("source_commit") != expected_commit:
        errors.append("source_commit does not match the release commit")
    if document.get("execution_environment") != "native-linux-compose":
        errors.append("execution_environment must be native-linux-compose")
    if document.get("producer") != "production/scripts/verify-backup.sh":
        errors.append("producer must identify production/scripts/verify-backup.sh")

    release = _exact_fields(
        document.get("release"),
        frozenset(
            {
                "release_tag",
                "signed_release_metadata_verified",
                "release_metadata_sha256",
                "compose_sha256",
                "image_lock_sha256",
            }
        ),
        label="release",
        errors=errors,
    )
    if release is not None:
        if not isinstance(
            release.get("release_tag"), str
        ) or not RELEASE_TAG_PATTERN.fullmatch(release["release_tag"]):
            errors.append("release.release_tag is invalid")
        elif (
            expected_release_tag is not None
            and release["release_tag"] != expected_release_tag
        ):
            errors.append("release.release_tag does not match the expected release")
        if release.get("signed_release_metadata_verified") is not True:
            errors.append("signed release metadata was not verified")
        for name in (
            "release_metadata_sha256",
            "compose_sha256",
            "image_lock_sha256",
        ):
            if not isinstance(release.get(name), str) or not SHA256_PATTERN.fullmatch(
                release[name]
            ):
                errors.append(f"release.{name} is invalid")

    host = _exact_fields(
        document.get("host"),
        frozenset(
            {
                "platform",
                "native_linux",
                "docker_context",
                "docker_ostype",
                "docker_operating_system",
                "rootless",
            }
        ),
        label="host",
        errors=errors,
    )
    if host is not None:
        if host.get("platform") != "linux" or host.get("native_linux") is not True:
            errors.append(
                "backup/restore release evidence requires a native Linux host"
            )
        if host.get("docker_context") != "default":
            errors.append("Docker context must be default")
        if host.get("docker_ostype") != "linux":
            errors.append("Docker OSType must be linux")
        operating_system = host.get("docker_operating_system")
        if not isinstance(operating_system, str) or not operating_system.strip():
            errors.append("Docker operating-system observation is missing")
        elif any(
            marker in operating_system.casefold()
            for marker in VIRTUALIZED_DOCKER_MARKERS
        ):
            errors.append("Docker Desktop or another VM-backed runtime is forbidden")
        if host.get("rootless") is not False:
            errors.append("rootless Docker is forbidden for formal release evidence")

    backup = _exact_fields(
        document.get("backup"),
        frozenset(
            {
                "backup_id",
                "manifest_sha256",
                "manifest_signature_verified",
                "storage_boundary",
                "off_host_retained",
                "source_project",
                "verification_started_at",
                "verification_completed_at",
                "verification_duration_seconds",
                "authority_counts",
            }
        ),
        label="backup",
        errors=errors,
    )
    backup_started: datetime | None = None
    backup_completed: datetime | None = None
    if backup is not None:
        if not isinstance(
            backup.get("backup_id"), str
        ) or not BACKUP_ID_PATTERN.fullmatch(backup["backup_id"]):
            errors.append("backup.backup_id is invalid")
        if not isinstance(
            backup.get("manifest_sha256"), str
        ) or not SHA256_PATTERN.fullmatch(backup["manifest_sha256"]):
            errors.append("backup.manifest_sha256 is invalid")
        if backup.get("manifest_signature_verified") is not True:
            errors.append("backup manifest signature was not verified")
        boundary = backup.get("storage_boundary")
        retained = backup.get("off_host_retained")
        if (boundary, retained) not in {
            ("encrypted-external", True),
            ("ephemeral-ci-drill", False),
        }:
            errors.append("backup storage boundary and retention claim are invalid")
        if backup.get("source_project") != "auris-flow":
            errors.append("backup source project must be auris-flow")
        backup_started, backup_completed = _validate_interval(
            backup, label="backup", errors=errors, prefix="verification_"
        )
        counts = _exact_fields(
            backup.get("authority_counts"),
            frozenset({"mysql", "minio", "qdrant"}),
            label="backup.authority_counts",
            errors=errors,
        )
        if counts is not None:
            mysql = _exact_fields(
                counts.get("mysql"),
                frozenset({"business_rows_total", "tables_total", "rows_total"}),
                label="backup.authority_counts.mysql",
                errors=errors,
            )
            if mysql is not None and (
                not _positive_int(mysql.get("tables_total"))
                or not _positive_int(mysql.get("rows_total"))
                or not _positive_int(mysql.get("business_rows_total"))
            ):
                errors.append(
                    "restore proof must contain non-empty MySQL business authority data"
                )
            minio = _exact_fields(
                counts.get("minio"),
                frozenset({"object_keys", "versions", "content_bytes"}),
                label="backup.authority_counts.minio",
                errors=errors,
            )
            if minio is not None and not all(
                _positive_int(minio.get(field))
                for field in ("object_keys", "versions", "content_bytes")
            ):
                errors.append(
                    "restore proof must contain non-empty MinIO authority data"
                )
            qdrant = _exact_fields(
                counts.get("qdrant"),
                frozenset({"collections", "points_total"}),
                label="backup.authority_counts.qdrant",
                errors=errors,
            )
            if qdrant is not None and not all(
                _positive_int(qdrant.get(field))
                for field in ("collections", "points_total")
            ):
                errors.append(
                    "restore proof must contain non-empty Qdrant derived data"
                )

    restore = _exact_fields(
        document.get("restore"),
        frozenset(
            {
                "project_name",
                "network_subnet",
                "edge_internal_ip",
                "started_at",
                "completed_at",
                "duration_seconds",
                "qdrant_mode",
                "empty_target_verified",
                "consistency",
            }
        ),
        label="restore",
        errors=errors,
    )
    restore_started: datetime | None = None
    restore_completed: datetime | None = None
    if restore is not None:
        project = restore.get("project_name")
        if not isinstance(project, str) or not DRILL_PROJECT_PATTERN.fullmatch(project):
            errors.append("restore must use a random isolated Compose project")
        network_subnet_value = restore.get("network_subnet")
        edge_internal_ip_value = restore.get("edge_internal_ip")
        try:
            if not isinstance(network_subnet_value, str) or not isinstance(
                edge_internal_ip_value, str
            ):
                raise ValueError
            restore_network = ipaddress.ip_network(network_subnet_value, strict=True)
            edge_internal_ip = ipaddress.ip_address(edge_internal_ip_value)
        except (TypeError, ValueError):
            errors.append("restore network allocation is invalid")
        else:
            if (
                not isinstance(restore_network, ipaddress.IPv4Network)
                or restore_network.prefixlen != 24
                or not restore_network.is_private
                or not isinstance(edge_internal_ip, ipaddress.IPv4Address)
                or edge_internal_ip != restore_network.network_address + 10
            ):
                errors.append("restore network allocation is invalid")
        restore_started, restore_completed = _validate_interval(
            restore, label="restore", errors=errors
        )
        if (
            backup_completed is not None
            and restore_started is not None
            and restore_started < backup_completed
        ):
            errors.append("backup/restore time ordering is invalid")
        if restore.get("qdrant_mode") != "snapshot":
            errors.append("formal restore drill must use Qdrant snapshot mode")
        empty_target = _exact_fields(
            restore.get("empty_target_verified"),
            frozenset({"mysql", "minio", "qdrant"}),
            label="restore.empty_target_verified",
            errors=errors,
        )
        if empty_target is not None and any(
            empty_target.get(name) is not True for name in ("mysql", "minio", "qdrant")
        ):
            errors.append("restore target was not proven empty")
        consistency = _exact_fields(
            restore.get("consistency"),
            frozenset(
                {
                    "mysql_counts_match",
                    "minio_versions_and_sha256_match",
                    "qdrant_fingerprints_match",
                }
            ),
            label="restore.consistency",
            errors=errors,
        )
        if consistency is not None:
            if consistency.get("mysql_counts_match") is not True:
                errors.append("restored MySQL counts were not verified")
            if consistency.get("minio_versions_and_sha256_match") is not True:
                errors.append("restored MinIO versions/SHA-256 were not verified")
            if consistency.get("qdrant_fingerprints_match") is not True:
                errors.append("restored Qdrant fingerprints were not verified")

    cleanup = _exact_fields(
        document.get("cleanup"),
        frozenset(
            {
                "started_at",
                "completed_at",
                "duration_seconds",
                "containers_removed",
                "volumes_removed",
                "networks_removed",
            }
        ),
        label="cleanup",
        errors=errors,
    )
    cleanup_started: datetime | None = None
    cleanup_completed: datetime | None = None
    if cleanup is not None:
        cleanup_started, cleanup_completed = _validate_interval(
            cleanup, label="cleanup", errors=errors
        )
        if (
            restore_completed is not None
            and cleanup_started is not None
            and cleanup_started < restore_completed
        ):
            errors.append("restore/cleanup time ordering is invalid")
        if cleanup.get("containers_removed") is not True:
            errors.append("isolated restore project containers were not removed")
        if cleanup.get("volumes_removed") is not True:
            errors.append("isolated restore project volumes were not removed")
        if cleanup.get("networks_removed") is not True:
            errors.append("isolated restore project networks were not removed")

    bindings = _exact_fields(
        document.get("tool_bindings"),
        frozenset(TOOL_BINDING_PATHS),
        label="tool_bindings",
        errors=errors,
    )
    repository_root = root.resolve()
    if bindings is not None:
        for relative in TOOL_BINDING_PATHS:
            claimed = bindings.get(relative)
            if not isinstance(claimed, str) or not SHA256_PATTERN.fullmatch(claimed):
                errors.append(f"tool binding is invalid: {relative}")
                continue
            path = repository_root / relative
            try:
                if not path.is_file() or path.is_symlink():
                    raise OSError
                actual = _sha256_file(path)
            except OSError:
                errors.append(f"bound release tool is missing or unsafe: {relative}")
                continue
            if actual != claimed:
                errors.append(f"tool binding does not match: {relative}")

    verified_at = _parse_timestamp(
        document.get("verified_at"), label="verified_at", errors=errors
    )
    if (
        cleanup_completed is not None
        and verified_at is not None
        and verified_at < cleanup_completed
    ):
        errors.append("cleanup/verification time ordering is invalid")
    return errors


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_from_bytes(payload: bytes, *, label: str) -> object:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid JSON") from exc


def _load_json(path: Path) -> object:
    return _json_from_bytes(
        _read_regular_bytes(
            path,
            max_bytes=MAX_JSON_BYTES,
            label="backup/restore evidence",
        ),
        label="backup/restore evidence",
    )


def validate_release_bindings(
    evidence: object,
    *,
    release_bundle_root: Path,
    expected_commit: str,
    expected_release_tag: str,
) -> list[str]:
    """Bind evidence claims to the exact signed deployment files being released."""

    errors: list[str] = []
    if not COMMIT_PATTERN.fullmatch(expected_commit):
        return ["expected_commit must be a complete lowercase Git object id"]
    if not RELEASE_TAG_PATTERN.fullmatch(expected_release_tag):
        return ["expected_release_tag must be a supported SemVer release tag"]
    if not release_bundle_root.is_dir() or release_bundle_root.is_symlink():
        return ["release bundle root must be a real directory"]
    document = evidence if isinstance(evidence, dict) else {}
    release = document.get("release")
    if not isinstance(release, dict):
        return ["release evidence binding is missing"]
    production = release_bundle_root / "production"
    metadata_path = production / "release-metadata.json"
    compose_path = production / "compose.yaml"
    image_lock_path = production / "images.lock.json"
    try:
        metadata_bytes = _read_regular_bytes(
            metadata_path,
            max_bytes=MAX_JSON_BYTES,
            label="release metadata",
        )
        compose_bytes = _read_regular_bytes(
            compose_path,
            max_bytes=MAX_RELEASE_FILE_BYTES,
            label="release Compose",
        )
        image_lock_bytes = _read_regular_bytes(
            image_lock_path,
            max_bytes=MAX_JSON_BYTES,
            label="release image lock",
        )
        metadata = _json_from_bytes(metadata_bytes, label="release metadata")
    except ValueError as exc:
        return [str(exc)]
    if not isinstance(metadata, dict):
        return ["release metadata must be a JSON object"]
    if metadata.get("release_tag") != expected_release_tag:
        errors.append("release metadata tag does not match the expected release")
    if metadata.get("source_commit") != expected_commit:
        errors.append("release metadata commit does not match the expected source")
    if release.get("release_tag") != expected_release_tag:
        errors.append("evidence release tag does not match the expected release")
    actual_metadata_sha256 = hashlib.sha256(metadata_bytes).hexdigest()
    actual_compose_sha256 = hashlib.sha256(compose_bytes).hexdigest()
    actual_image_lock_sha256 = hashlib.sha256(image_lock_bytes).hexdigest()
    if release.get("release_metadata_sha256") != actual_metadata_sha256:
        errors.append("release metadata digest does not match the evidence")
    if release.get("compose_sha256") != actual_compose_sha256:
        errors.append("release Compose digest does not match the evidence")
    if release.get("image_lock_sha256") != actual_image_lock_sha256:
        errors.append("release image-lock digest does not match the evidence")
    compose_binding = metadata.get("compose")
    if (
        not isinstance(compose_binding, dict)
        or compose_binding.get("path") != "production/compose.yaml"
        or compose_binding.get("sha256") != actual_compose_sha256
    ):
        errors.append("release metadata Compose binding is invalid")
    image_lock_binding = metadata.get("image_lock")
    if (
        not isinstance(image_lock_binding, dict)
        or image_lock_binding.get("path") != "production/images.lock.json"
        or image_lock_binding.get("sha256") != actual_image_lock_sha256
    ):
        errors.append("release metadata image-lock binding is invalid")
    return errors


def verify_sigstore_attestation(
    *,
    evidence_path: Path,
    signature_bundle: Path,
    release_tag: str,
    cosign_binary: str = "cosign",
    run: Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Verify evidence was signed by the exact official tag workflow identity."""

    if not RELEASE_TAG_PATTERN.fullmatch(release_tag):
        raise FormalEvidenceError("release tag is invalid for Sigstore verification")
    try:
        _read_regular_bytes(
            evidence_path,
            max_bytes=MAX_JSON_BYTES,
            label="backup/restore evidence",
        )
        _read_regular_bytes(
            signature_bundle,
            max_bytes=MAX_JSON_BYTES,
            label="backup/restore Sigstore bundle",
        )
    except ValueError as exc:
        raise FormalEvidenceError(str(exc)) from exc
    command = (
        cosign_binary,
        "verify-blob",
        "--bundle",
        str(signature_bundle),
        "--certificate-identity",
        OFFICIAL_RELEASE_WORKFLOW_PREFIX + release_tag,
        "--certificate-oidc-issuer",
        SIGSTORE_ISSUER,
        str(evidence_path),
    )
    try:
        completed = (
            run(command)
            if run is not None
            else subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FormalEvidenceError(
            "Cosign is required to verify backup/restore evidence"
        ) from exc
    if completed.returncode != 0:
        raise FormalEvidenceError(
            "backup/restore evidence Sigstore verification failed"
        )


def verify_signed_release_bundle(
    release_bundle_root: Path,
    *,
    run: Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Verify an external bundle with this checkout's trusted verifier."""

    if not release_bundle_root.is_dir() or release_bundle_root.is_symlink():
        raise FormalEvidenceError("release bundle root must be a real directory")
    verifier = ROOT / "scripts" / "release_bundle.py"
    try:
        _read_regular_bytes(
            verifier,
            max_bytes=MAX_RELEASE_FILE_BYTES,
            label="release bundle verifier",
        )
        command = (
            sys.executable,
            str(verifier),
            "verify",
            "--bundle-root",
            str(release_bundle_root),
            "--verify-signature",
        )
        completed = (
            run(command)
            if run is not None
            else subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        )
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        raise FormalEvidenceError(
            "signed release bundle verification could not run"
        ) from exc
    if completed.returncode != 0:
        raise FormalEvidenceError("signed release bundle verification failed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate native-Linux backup/restore release evidence."
    )
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-release-tag")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--formal",
        action="store_true",
        help="Require tag-bound Sigstore evidence and signed deployment bindings",
    )
    parser.add_argument("--signature-bundle", type=Path)
    parser.add_argument("--release-bundle-root", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.formal and (
            args.expected_release_tag is None
            or args.signature_bundle is None
            or args.release_bundle_root is None
        ):
            raise ValueError(
                "--formal requires --expected-release-tag, --signature-bundle, "
                "and --release-bundle-root"
            )
        validation_root = args.release_bundle_root if args.formal else args.root
        if validation_root is None:
            raise ValueError("release validation root is missing")
        if validation_root.is_symlink():
            raise ValueError("release validation root must not be a symlink")
        evidence = _load_json(args.artifact)
        errors = validate_evidence(
            evidence,
            root=validation_root.resolve(),
            expected_commit=args.expected_commit,
            expected_release_tag=args.expected_release_tag,
        )
        if args.formal:
            assert args.expected_release_tag is not None
            assert args.release_bundle_root is not None
            assert args.signature_bundle is not None
            errors.extend(
                validate_release_bindings(
                    evidence,
                    release_bundle_root=args.release_bundle_root,
                    expected_commit=args.expected_commit,
                    expected_release_tag=args.expected_release_tag,
                )
            )
            if not errors:
                verify_signed_release_bundle(args.release_bundle_root)
                verify_sigstore_attestation(
                    evidence_path=args.artifact,
                    signature_bundle=args.signature_bundle,
                    release_tag=args.expected_release_tag,
                )
    except FormalEvidenceError as exc:
        errors = [str(exc)]
    except ValueError as exc:
        errors = [str(exc)]
    if errors:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "release_evidence": False,
                    "blockers": errors,
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {"status": "ok", "release_evidence": True},
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
