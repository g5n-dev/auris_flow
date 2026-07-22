#!/usr/bin/env python3
"""Create and verify externally signed Auris Flow backup manifests.

Manifest processing uses Python's standard library. Ed25519 signing and
verification require OpenSSL on the recovery host and a deployment trust key
that is not stored in the backup.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = "auris-flow.backup-manifest/v3"
RELEASE_METADATA_SCHEMA = "auris.release-deployment-metadata.v3"
IMAGE_LOCK_SCHEMA = "auris.release-image-lock.v1"
REQUIRED_AUTHORITIES = ("mysql", "minio")
MANIFEST_NAME = "manifest.json"
CHECKSUM_NAME = "manifest.sha256"
SIGNATURE_NAME = "manifest.signature.json"
SIGNATURE_SCHEMA = "auris-flow.backup-manifest-signature/v1"
SIGNATURE_ALGORITHM = "ed25519"
SIGNATURE_PURPOSE = "auris-flow-production-backup-manifest"
RESTORE_ATTESTATION_DELEGATION_SCHEMA = "auris-flow.restore-attestation-delegation/v1"
RESTORE_ATTESTATION_PURPOSE = "auris-flow-restore-completion"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
RELEASE_TAG_RE = re.compile(
    r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-rc\.[1-9]\d*)?$"
)
BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
KEY_ID_RE = re.compile(r"^ed25519-sha256:[0-9a-f]{64}$")
IMAGE_REFERENCE_RE = re.compile(r"^[^\s@$]+(?::[^\s@]+)?@sha256:[0-9a-f]{64}$")
SNAPSHOT_MARKER = ".auris-flow-restore-snapshot"
SNAPSHOT_MARKER_VALUE = "auris-flow.restore-snapshot.v1\n"
RELEASE_MEMBER_MODES = frozenset({"0600", "0644", "0755"})
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")
CONTROL_NAMES = (MANIFEST_NAME, CHECKSUM_NAME, SIGNATURE_NAME)


@dataclass(frozen=True)
class VerificationBudgets:
    manifest_bytes: int
    checksum_bytes: int
    signature_bytes: int
    artifact_count: int
    signed_bytes: int


_BUDGET_SPECS = {
    "AURIS_BACKUP_MAX_MANIFEST_BYTES": (16 * 1024 * 1024, 64 * 1024 * 1024),
    "AURIS_BACKUP_MAX_CHECKSUM_BYTES": (256, 4096),
    "AURIS_BACKUP_MAX_SIGNATURE_BYTES": (16 * 1024, 64 * 1024),
    "AURIS_BACKUP_MAX_ARTIFACTS": (100_000, 1_000_000),
    "AURIS_BACKUP_MAX_SIGNED_BYTES": (4 * 1024**4, 64 * 1024**4),
}


class ManifestError(ValueError):
    """Raised when an untrusted backup does not satisfy the manifest schema."""


def _positive_budget(name: str) -> int:
    default, ceiling = _BUDGET_SPECS[name]
    raw = os.environ.get(name, str(default))
    if not re.fullmatch(r"[1-9][0-9]*", raw):
        raise ManifestError(f"{name} must be a positive integer")
    value = int(raw)
    if value > ceiling:
        raise ManifestError(f"{name} exceeds the hard safety ceiling {ceiling}")
    return value


def _verification_budgets() -> VerificationBudgets:
    return VerificationBudgets(
        manifest_bytes=_positive_budget("AURIS_BACKUP_MAX_MANIFEST_BYTES"),
        checksum_bytes=_positive_budget("AURIS_BACKUP_MAX_CHECKSUM_BYTES"),
        signature_bytes=_positive_budget("AURIS_BACKUP_MAX_SIGNATURE_BYTES"),
        artifact_count=_positive_budget("AURIS_BACKUP_MAX_ARTIFACTS"),
        signed_bytes=_positive_budget("AURIS_BACKUP_MAX_SIGNED_BYTES"),
    )


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ManifestError(f"missing {label}: {path.name}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ManifestError(f"{label} must be a regular file and not a symlink")


def _stable_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_directory_fd(path: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise ManifestError("backup root must be a real directory, not a symlink")
    return descriptor


def _read_regular_at_bounded(
    directory_fd: int,
    name: str,
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
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError as exc:
        raise ManifestError(f"missing {label}: {name}") from exc
    except OSError as exc:
        raise ManifestError(
            f"{label} must be a regular file and not a symlink"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ManifestError(f"{label} must be a regular file and not a symlink")
        if before.st_size > max_bytes:
            raise ManifestError(f"{name} exceeds the configured {label} byte budget")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            raise ManifestError(f"{name} exceeds the configured {label} byte budget")
        after = os.fstat(descriptor)
        if _stable_file_identity(before) != _stable_file_identity(after):
            raise ManifestError(f"{name} changed while its control data was read")
        return payload
    finally:
        os.close(descriptor)


def _json_from_bytes(payload: bytes, *, label: str) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ManifestError(f"invalid JSON in {label}") from exc


def _key_file(raw: str | Path, *, private: bool) -> Path:
    path = Path(raw)
    _regular_file(path, label="backup manifest signing key")
    metadata = path.stat()
    if metadata.st_size <= 0 or metadata.st_size > 16 * 1024:
        raise ManifestError("backup manifest signing key size is invalid")
    if private and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ManifestError(
            "backup manifest private key must not be group/world readable"
        )
    return path.resolve(strict=True)


def _require_external_key(root: Path, key: Path) -> None:
    try:
        key.relative_to(root)
    except ValueError:
        return
    raise ManifestError("backup manifest trust keys must be external to the backup")


def _run_openssl(arguments: list[str], *, label: str) -> bytes:
    try:
        completed = subprocess.run(
            ["openssl", *arguments],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise ManifestError("OpenSSL is required for backup manifest trust") from exc
    if completed.returncode != 0:
        raise ManifestError(f"OpenSSL rejected the {label}")
    return completed.stdout


def _ed25519_public_der(path: Path, *, private: bool) -> bytes:
    arguments = ["pkey"]
    if not private:
        arguments.append("-pubin")
    arguments.extend(["-in", str(path), "-pubout", "-outform", "DER"])
    public_der = _run_openssl(arguments, label="Ed25519 key")
    if len(public_der) != len(ED25519_SPKI_PREFIX) + 32 or not public_der.startswith(
        ED25519_SPKI_PREFIX
    ):
        raise ManifestError("backup manifest key must be Ed25519")
    return public_der


def _key_id(public_der: bytes) -> str:
    return f"ed25519-sha256:{hashlib.sha256(public_der).hexdigest()}"


def _validated_key_pair(
    private_key_raw: str | Path, public_key_raw: str | Path
) -> tuple[Path, Path, str]:
    private_key = _key_file(private_key_raw, private=True)
    public_key = _key_file(public_key_raw, private=False)
    private_public = _ed25519_public_der(private_key, private=True)
    trusted_public = _ed25519_public_der(public_key, private=False)
    if not hmac.compare_digest(private_public, trusted_public):
        raise ManifestError("backup manifest signing key pair does not match")
    return private_key, public_key, _key_id(trusted_public)


def _safe_root(raw: str | Path) -> Path:
    root = Path(raw)
    try:
        metadata = root.lstat()
    except FileNotFoundError as exc:
        raise ManifestError("backup root does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ManifestError("backup root must be a real directory, not a symlink")
    return root.resolve(strict=True)


def _safe_relative(raw: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise ManifestError("artifact path is not a safe POSIX relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ManifestError(f"unsafe artifact path: {raw!r}")
    if raw in {MANIFEST_NAME, CHECKSUM_NAME, SIGNATURE_NAME}:
        raise ManifestError(f"reserved artifact path: {raw}")
    return path


def _load_json(path: Path, *, label: str) -> Any:
    _regular_file(path, label=label)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"invalid JSON in {label}") from exc


def _validate_timestamp(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.endswith("Z"):
        raise ManifestError("created_at_utc must be an RFC3339 UTC timestamp")
    try:
        datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as exc:
        raise ManifestError("created_at_utc must be an RFC3339 UTC timestamp") from exc
    return raw


def _walk_artifacts(
    root: Path, *, budgets: VerificationBudgets | None = None
) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in directory_names:
            candidate = base / name
            if candidate.is_symlink():
                raise ManifestError(
                    f"symlink is forbidden in backup: {candidate.relative_to(root)}"
                )
        for name in file_names:
            candidate = base / name
            relative = candidate.relative_to(root).as_posix()
            if relative in {MANIFEST_NAME, CHECKSUM_NAME, SIGNATURE_NAME}:
                continue
            _safe_relative(relative)
            _regular_file(candidate, label=f"artifact {relative}")
            size_bytes = candidate.stat().st_size
            if budgets is not None:
                if len(artifacts) >= budgets.artifact_count:
                    raise ManifestError(
                        "backup artifact count exceeds the configured resource budget"
                    )
                total_bytes += size_bytes
                if total_bytes > budgets.signed_bytes:
                    raise ManifestError(
                        "backup signed artifact bytes exceed the configured resource budget"
                    )
            artifacts[relative] = {
                "path": relative,
                "sha256": _sha256_file(candidate),
                "size_bytes": size_bytes,
            }
    return dict(sorted(artifacts.items()))


def _open_relative_regular(root_fd: int, relative: PurePosixPath, *, label: str) -> int:
    directory_fd = os.dup(root_fd)
    try:
        for component in relative.parts[:-1]:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(relative.name, flags, dir_fd=directory_fd)
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        raise ManifestError(
            f"{label} must be a regular file and not a symlink"
        ) from exc
    finally:
        os.close(directory_fd)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise ManifestError(f"{label} must be a regular file and not a symlink")
    return descriptor


def _hash_signed_artifact(
    root_fd: int, relative: PurePosixPath, *, expected_size: int
) -> str:
    label = f"artifact {relative.as_posix()}"
    descriptor = _open_relative_regular(root_fd, relative, label=label)
    try:
        before = os.fstat(descriptor)
        if before.st_size != expected_size:
            raise ManifestError(f"artifact size mismatch: {relative.as_posix()}")
        digest = hashlib.sha256()
        remaining = expected_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ManifestError(f"artifact size mismatch: {relative.as_posix()}")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ManifestError(f"artifact size mismatch: {relative.as_posix()}")
        after = os.fstat(descriptor)
        if _stable_file_identity(before) != _stable_file_identity(after):
            raise ManifestError(
                f"artifact changed while being verified: {relative.as_posix()}"
            )
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _write_private_file(destination: Path, payload: bytes) -> None:
    descriptor = os.open(
        destination,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ManifestError("snapshot destination write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_signed_artifact(
    *,
    source_root_fd: int,
    relative: PurePosixPath,
    destination_root: Path,
    expected_size: int,
) -> None:
    label = f"artifact {relative.as_posix()}"
    source_fd = _open_relative_regular(source_root_fd, relative, label=label)
    try:
        before = os.fstat(source_fd)
        if before.st_size != expected_size:
            raise ManifestError(f"artifact size mismatch: {relative.as_posix()}")
        destination = destination_root
        for component in relative.parts[:-1]:
            destination /= component
            destination.mkdir(mode=0o700, exist_ok=True)
            if destination.is_symlink() or not destination.is_dir():
                raise ManifestError("snapshot destination directory is unsafe")
        destination /= relative.name
        destination_fd = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        try:
            remaining = expected_size
            while remaining:
                chunk = os.read(source_fd, min(1024 * 1024, remaining))
                if not chunk:
                    raise ManifestError(
                        f"artifact size mismatch: {relative.as_posix()}"
                    )
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise ManifestError(
                            "snapshot destination write made no progress"
                        )
                    view = view[written:]
                remaining -= len(chunk)
            if os.read(source_fd, 1):
                raise ManifestError(f"artifact size mismatch: {relative.as_posix()}")
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
        after = os.fstat(source_fd)
        if _stable_file_identity(before) != _stable_file_identity(after):
            raise ManifestError(
                f"snapshot source changed while being copied: {relative.as_posix()}"
            )
    finally:
        os.close(source_fd)


def snapshot_backup(args: argparse.Namespace) -> int:
    budgets = _verification_budgets()
    source = _safe_root(args.source)
    snapshot_root = _safe_root(args.snapshot_root)
    public_key = _key_file(args.public_key, private=False)
    _require_external_key(source, public_key)
    if (
        source == snapshot_root
        or source.is_relative_to(snapshot_root)
        or snapshot_root.is_relative_to(source)
    ):
        raise ManifestError("snapshot root must not overlap the backup source")
    if any(snapshot_root.iterdir()):
        raise ManifestError("snapshot root must start empty")
    os.chmod(snapshot_root, 0o700)
    marker = snapshot_root / SNAPSHOT_MARKER
    destination_root = snapshot_root / "backup"
    source_fd = _open_directory_fd(source)
    try:
        marker.write_text(SNAPSHOT_MARKER_VALUE, encoding="ascii")
        os.chmod(marker, 0o400)
        destination_root.mkdir(mode=0o700)

        control_payloads = _read_control_payloads(source_fd, budgets, signed=True)
        for name in CONTROL_NAMES:
            _write_private_file(destination_root / name, control_payloads[name])
        destination_fd = _open_directory_fd(destination_root)
        try:
            document, _statement, _payloads = _verified_controls_from_fd(
                destination_root,
                destination_fd,
                args.public_key,
                budgets,
            )
        finally:
            os.close(destination_fd)

        for artifact in document["artifacts"]:
            relative = _safe_relative(artifact["path"])
            _copy_signed_artifact(
                source_root_fd=source_fd,
                relative=relative,
                destination_root=destination_root,
                expected_size=artifact["size_bytes"],
            )
        _verify_trusted_backup(destination_root, args.public_key, budgets)
        destination_directories = sorted(
            (path for path in destination_root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for destination_directory in destination_directories:
            destination_directory.chmod(0o500)
        destination_root.chmod(0o500)
        print(
            json.dumps(
                {
                    "artifact_count": len(document["artifacts"]),
                    "snapshot": str(destination_root),
                    "status": "created",
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception:
        shutil.rmtree(snapshot_root, ignore_errors=True)
        raise
    finally:
        os.close(source_fd)


def destroy_snapshot(args: argparse.Namespace) -> int:
    snapshot_root = _safe_root(args.snapshot_root)
    marker = snapshot_root / SNAPSHOT_MARKER
    _regular_file(marker, label="restore snapshot marker")
    if marker.read_text(encoding="ascii") != SNAPSHOT_MARKER_VALUE:
        raise ManifestError("restore snapshot marker is invalid")
    if {entry.name for entry in snapshot_root.iterdir()} != {SNAPSHOT_MARKER, "backup"}:
        raise ManifestError("restore snapshot root contains unexpected entries")
    backup = snapshot_root / "backup"
    if backup.is_symlink() or not backup.is_dir():
        raise ManifestError("restore snapshot backup directory is unsafe")
    for directory, directory_names, file_names in os.walk(
        backup, topdown=True, followlinks=False
    ):
        base = Path(directory)
        if base.is_symlink():
            raise ManifestError("symlink is forbidden in restore snapshot")
        base.chmod(0o700)
        for name in (*directory_names, *file_names):
            if (base / name).is_symlink():
                raise ManifestError("symlink is forbidden in restore snapshot")
    shutil.rmtree(snapshot_root)
    print(json.dumps({"status": "destroyed"}))
    return 0


def _validate_counts(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError("tenant-independent counts must be a JSON object")
    forbidden_fragments = ("tenant_id", "project_id", "by_tenant", "by_project")

    def nested_keys(node: Any) -> list[str]:
        if isinstance(node, dict):
            result: list[str] = []
            for key, child in node.items():
                result.append(str(key).lower())
                result.extend(nested_keys(child))
            return result
        if isinstance(node, list):
            return [key for child in node for key in nested_keys(child)]
        return []

    serialized_keys = " ".join(nested_keys(value))
    if any(fragment in serialized_keys for fragment in forbidden_fragments):
        raise ManifestError(
            "counts must be deployment-wide and must not be grouped by tenant/project"
        )
    return value


def _validate_release_metadata(value: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "release_tag",
        "source_commit",
        "compose",
        "image_lock",
        "restore_policy",
        "images",
        "members",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ManifestError("release metadata has missing or unexpected fields")
    if value.get("schema_version") != RELEASE_METADATA_SCHEMA:
        raise ManifestError("release metadata schema is not supported")
    release_tag = value.get("release_tag")
    source_commit = value.get("source_commit")
    if not isinstance(release_tag, str) or not RELEASE_TAG_RE.fullmatch(release_tag):
        raise ManifestError("release metadata tag is invalid")
    if not isinstance(source_commit, str) or not COMMIT_RE.fullmatch(source_commit):
        raise ManifestError("release metadata source commit is invalid")
    bindings: dict[str, dict[str, str]] = {}
    for key, expected_path in (
        ("compose", "production/compose.yaml"),
        ("image_lock", "production/images.lock.json"),
        ("restore_policy", "production/restore-compatibility.json"),
    ):
        binding = value.get(key)
        if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
            raise ManifestError(f"release metadata {key} binding is invalid")
        digest = binding.get("sha256")
        if (
            binding.get("path") != expected_path
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
        ):
            raise ManifestError(f"release metadata {key} binding is invalid")
        bindings[key] = {"path": expected_path, "sha256": digest}
    raw_images = value.get("images")
    if not isinstance(raw_images, dict) or not raw_images:
        raise ManifestError("release metadata images must be a non-empty map")
    images: dict[str, str] = {}
    for service, reference in sorted(raw_images.items()):
        if not isinstance(service, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]*", service
        ):
            raise ManifestError("release metadata service name is invalid")
        if not isinstance(reference, str) or not IMAGE_REFERENCE_RE.fullmatch(
            reference
        ):
            raise ManifestError("release metadata image is not digest-pinned")
        tagged = reference.split("@", 1)[0].rsplit("/", 1)[-1]
        if ":" not in tagged or tagged.rsplit(":", 1)[1].casefold() == "latest":
            raise ManifestError("release metadata image tag is missing or mutable")
        images[service] = reference
    raw_members = value.get("members")
    if not isinstance(raw_members, list) or not raw_members:
        raise ManifestError("release metadata members must be a non-empty list")
    members: list[dict[str, str]] = []
    member_paths: set[str] = set()
    for raw_member in raw_members:
        if not isinstance(raw_member, dict) or set(raw_member) != {
            "path",
            "sha256",
            "type",
            "mode",
        }:
            raise ManifestError("release metadata member is invalid")
        raw_path = raw_member.get("path")
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or raw_path != raw_path.strip()
            or "\\" in raw_path
            or any(ord(character) < 32 for character in raw_path)
        ):
            raise ManifestError("release metadata member path is unsafe")
        path = PurePosixPath(raw_path)
        if (
            path.is_absolute()
            or path.as_posix() != raw_path
            or any(part in {"", ".", ".."} for part in path.parts)
            or raw_path
            in {
                "production/release-metadata.json",
                "production/release-metadata.sigstore.json",
            }
        ):
            raise ManifestError("release metadata member path is unsafe")
        if raw_path in member_paths:
            raise ManifestError("release metadata member path is duplicated")
        member_paths.add(raw_path)
        digest = raw_member.get("sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ManifestError("release metadata member sha256 is invalid")
        if raw_member.get("type") != "regular-file":
            raise ManifestError("release metadata member type is invalid")
        mode = raw_member.get("mode")
        if not isinstance(mode, str) or mode not in RELEASE_MEMBER_MODES:
            raise ManifestError("release metadata member mode is invalid")
        members.append(
            {
                "path": raw_path,
                "sha256": digest,
                "type": "regular-file",
                "mode": mode,
            }
        )
    if [member["path"] for member in members] != sorted(member_paths):
        raise ManifestError("release metadata members must be sorted")
    return {
        "schema_version": RELEASE_METADATA_SCHEMA,
        "release_tag": release_tag,
        "source_commit": source_commit,
        "compose": bindings["compose"],
        "image_lock": bindings["image_lock"],
        "restore_policy": bindings["restore_policy"],
        "images": images,
        "members": members,
    }


def _validate_running_images(
    value: Any, release_metadata: Mapping[str, Any]
) -> dict[str, Any]:
    required = {
        "schema_version",
        "release_tag",
        "source_commit",
        "verification_scope",
        "images",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ManifestError("running image evidence has missing or unexpected fields")
    if value.get("schema_version") != "auris.release-running-images.v1":
        raise ManifestError("running image evidence schema is not supported")
    if value.get("release_tag") != release_metadata["release_tag"]:
        raise ManifestError(
            "running image evidence release tag does not match metadata"
        )
    if value.get("source_commit") != release_metadata["source_commit"]:
        raise ManifestError("running image evidence commit does not match metadata")
    if value.get("verification_scope") != "all-running-release-services":
        raise ManifestError(
            "running image evidence must cover all running release services"
        )
    raw_images = value.get("images")
    required_services = {"mysql", "minio", "qdrant", "redis"}
    if not isinstance(raw_images, dict) or not required_services.issubset(raw_images):
        raise ManifestError(
            "running image evidence must contain mysql, minio, qdrant and redis"
        )
    images: dict[str, str] = {}
    metadata_images = release_metadata["images"]
    unknown_services = sorted(set(raw_images) - set(metadata_images))
    if unknown_services:
        raise ManifestError(
            "running image evidence contains an unknown release service"
        )
    for service in sorted(raw_images):
        reference = raw_images.get(service)
        if reference != metadata_images.get(service):
            raise ManifestError(
                f"running image evidence does not match release metadata: {service}"
            )
        images[service] = str(reference)
    return {
        "schema_version": "auris.release-running-images.v1",
        "release_tag": release_metadata["release_tag"],
        "source_commit": release_metadata["source_commit"],
        "verification_scope": "all-running-release-services",
        "images": images,
    }


def _validate_document(document: Any) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != SCHEMA_VERSION
    ):
        raise ManifestError(f"manifest schema_version must be {SCHEMA_VERSION}")
    if set(document) != {
        "schema_version",
        "backup_id",
        "created_at_utc",
        "source",
        "restore_attestation",
        "storage_boundary",
        "data_authority",
        "tenant_independent_counts",
        "tool_versions",
        "artifacts",
    }:
        raise ManifestError("manifest shape is invalid")
    backup_id = document.get("backup_id")
    if not isinstance(backup_id, str) or not BACKUP_ID_RE.fullmatch(backup_id):
        raise ManifestError("invalid backup_id")
    _validate_timestamp(document.get("created_at_utc"))
    source = document.get("source")
    if not isinstance(source, dict) or set(source) != {
        "git_commit",
        "release_version",
        "release_metadata",
        "release_metadata_sha256",
        "running_images",
        "running_images_sha256",
    }:
        raise ManifestError("manifest source is required")
    commit = source.get("git_commit")
    version = source.get("release_version")
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise ManifestError("source.git_commit must be a hexadecimal commit id")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ManifestError("source.release_version is invalid")
    restore_attestation = document.get("restore_attestation")
    if not isinstance(restore_attestation, dict) or set(restore_attestation) != {
        "schema_version",
        "algorithm",
        "purpose",
        "key_id",
    }:
        raise ManifestError("restore attestation delegation is invalid")
    if (
        restore_attestation.get("schema_version")
        != RESTORE_ATTESTATION_DELEGATION_SCHEMA
        or restore_attestation.get("algorithm") != SIGNATURE_ALGORITHM
        or restore_attestation.get("purpose") != RESTORE_ATTESTATION_PURPOSE
        or not KEY_ID_RE.fullmatch(str(restore_attestation.get("key_id") or ""))
    ):
        raise ManifestError("restore attestation delegation is invalid")
    release_metadata = _validate_release_metadata(source.get("release_metadata"))
    release_metadata_sha256 = source.get("release_metadata_sha256")
    if not isinstance(release_metadata_sha256, str) or not SHA256_RE.fullmatch(
        release_metadata_sha256
    ):
        raise ManifestError("source.release_metadata_sha256 is invalid")
    if release_metadata["source_commit"] != commit:
        raise ManifestError("backup commit does not match release metadata")
    if release_metadata["release_tag"] != version:
        raise ManifestError("backup release version does not match release metadata")
    _validate_running_images(source.get("running_images"), release_metadata)
    running_images_sha256 = source.get("running_images_sha256")
    if not isinstance(running_images_sha256, str) or not SHA256_RE.fullmatch(
        running_images_sha256
    ):
        raise ManifestError("source.running_images_sha256 is invalid")
    boundary = document.get("storage_boundary")
    if not isinstance(boundary, dict) or boundary.get("operator_assertion") != (
        "encrypted-at-rest-and-copied-off-host"
    ):
        raise ManifestError("backup requires the encrypted external storage assertion")
    if boundary.get("contains_sensitive_data") is not True:
        raise ManifestError("backup must be classified as containing sensitive data")
    authority = document.get("data_authority")
    if not isinstance(authority, dict) or tuple(
        authority.get("authoritative") or ()
    ) != (REQUIRED_AUTHORITIES):
        raise ManifestError("MySQL and MinIO must be the ordered authoritative sources")
    if set(authority.get("derived_or_optional") or ()) != {"qdrant", "redis"}:
        raise ManifestError("Qdrant and Redis must be classified as derived/optional")
    _validate_counts(document.get("tenant_independent_counts"))
    if not isinstance(document.get("tool_versions"), dict):
        raise ManifestError("tool_versions must be a JSON object")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ManifestError("manifest must contain at least one artifact")
    seen: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ManifestError("artifact entry must be an object")
        artifact_path = artifact.get("path")
        if not isinstance(artifact_path, str):
            raise ManifestError("artifact path must be a string")
        relative = _safe_relative(artifact_path)
        path_text = relative.as_posix()
        if path_text in seen:
            raise ManifestError(f"duplicate artifact: {path_text}")
        seen.add(path_text)
        if type(artifact.get("size_bytes")) is not int or artifact["size_bytes"] < 0:
            raise ManifestError(f"invalid artifact size: {path_text}")
        if not isinstance(artifact.get("sha256"), str) or not SHA256_RE.fullmatch(
            artifact["sha256"]
        ):
            raise ManifestError(f"invalid artifact checksum: {path_text}")
    return document


def _validate_resource_budget(
    document: Mapping[str, Any], budgets: VerificationBudgets
) -> None:
    artifacts = document["artifacts"]
    if len(artifacts) > budgets.artifact_count:
        raise ManifestError(
            "backup artifact count exceeds the configured resource budget"
        )
    total_size = sum(int(artifact["size_bytes"]) for artifact in artifacts)
    if total_size > budgets.signed_bytes:
        raise ManifestError(
            "backup signed artifact bytes exceed the configured resource budget"
        )


def create_manifest(args: argparse.Namespace) -> int:
    budgets = _verification_budgets()
    root = _safe_root(args.root)
    manifest_path = root / MANIFEST_NAME
    checksum_path = root / CHECKSUM_NAME
    if manifest_path.exists() or checksum_path.exists():
        raise ManifestError("refusing to overwrite an existing manifest")
    restore_attestation_public_key = _key_file(
        args.restore_attestation_public_key, private=False
    )
    _require_external_key(root, restore_attestation_public_key)
    restore_attestation_key_id = _key_id(
        _ed25519_public_der(restore_attestation_public_key, private=False)
    )
    counts = _validate_counts(_load_json(Path(args.counts), label="counts"))
    tool_versions = _load_json(Path(args.tool_versions), label="tool versions")
    if not isinstance(tool_versions, dict):
        raise ManifestError("tool versions must be a JSON object")
    release_metadata_path = Path(args.release_metadata)
    release_metadata = _validate_release_metadata(
        _load_json(release_metadata_path, label="release metadata")
    )
    running_images_path = Path(args.running_images)
    running_images = _validate_running_images(
        _load_json(running_images_path, label="running image evidence"),
        release_metadata,
    )
    if release_metadata["source_commit"] != args.git_commit:
        raise ManifestError("--git-commit does not match release metadata")
    if release_metadata["release_tag"] != args.release_version:
        raise ManifestError("--release-version does not match release metadata")
    artifacts = list(_walk_artifacts(root, budgets=budgets).values())
    required_paths = {
        "metadata/release-metadata.json",
        "metadata/release-metadata.sigstore.json",
        "metadata/running-images.json",
        "mysql/all-databases.sql.gz",
        "mysql/table-counts.tsv",
        "minio/versions.json",
    }
    missing = sorted(required_paths.difference(item["path"] for item in artifacts))
    if missing:
        raise ManifestError(
            f"required backup artifact(s) missing: {', '.join(missing)}"
        )
    document = {
        "schema_version": SCHEMA_VERSION,
        "backup_id": args.backup_id,
        "created_at_utc": _validate_timestamp(args.created_at_utc),
        "source": {
            "git_commit": args.git_commit,
            "release_version": args.release_version,
            "release_metadata": release_metadata,
            "release_metadata_sha256": _sha256_file(release_metadata_path),
            "running_images": running_images,
            "running_images_sha256": _sha256_file(running_images_path),
        },
        "restore_attestation": {
            "schema_version": RESTORE_ATTESTATION_DELEGATION_SCHEMA,
            "algorithm": SIGNATURE_ALGORITHM,
            "purpose": RESTORE_ATTESTATION_PURPOSE,
            "key_id": restore_attestation_key_id,
        },
        "storage_boundary": {
            "contains_sensitive_data": True,
            "operator_assertion": "encrypted-at-rest-and-copied-off-host",
            "repository_never_contains_backup_payloads": True,
        },
        "data_authority": {
            "authoritative": list(REQUIRED_AUTHORITIES),
            "derived_or_optional": ["qdrant", "redis"],
            "restore_order": [
                "mysql",
                "minio",
                "qdrant-derived",
                "redis-cache-optional",
            ],
        },
        "tenant_independent_counts": counts,
        "tool_versions": tool_versions,
        "artifacts": artifacts,
    }
    _validate_document(document)
    _validate_resource_budget(document, budgets)
    manifest_bytes = _canonical_json(document)
    if len(manifest_bytes) > budgets.manifest_bytes:
        raise ManifestError(
            f"{MANIFEST_NAME} exceeds the configured manifest byte budget"
        )
    temporary = root / f".{MANIFEST_NAME}.tmp"
    temporary.write_bytes(manifest_bytes)
    os.chmod(temporary, 0o600)
    os.replace(temporary, manifest_path)
    checksum = hashlib.sha256(manifest_bytes).hexdigest()
    checksum_path.write_text(f"{checksum}  {MANIFEST_NAME}\n", encoding="ascii")
    os.chmod(checksum_path, 0o600)
    print(json.dumps({"backup_id": args.backup_id, "artifact_count": len(artifacts)}))
    return 0


def _read_control_payloads(
    root_fd: int, budgets: VerificationBudgets, *, signed: bool
) -> dict[str, bytes]:
    payloads = {
        MANIFEST_NAME: _read_regular_at_bounded(
            root_fd,
            MANIFEST_NAME,
            max_bytes=budgets.manifest_bytes,
            label="manifest control",
        ),
        CHECKSUM_NAME: _read_regular_at_bounded(
            root_fd,
            CHECKSUM_NAME,
            max_bytes=budgets.checksum_bytes,
            label="manifest checksum control",
        ),
    }
    if signed:
        payloads[SIGNATURE_NAME] = _read_regular_at_bounded(
            root_fd,
            SIGNATURE_NAME,
            max_bytes=budgets.signature_bytes,
            label="manifest signature control",
        )
    return payloads


def _validated_manifest_controls(
    payloads: Mapping[str, bytes], budgets: VerificationBudgets
) -> tuple[dict[str, Any], str]:
    manifest_payload = payloads[MANIFEST_NAME]
    try:
        checksum_line = payloads[CHECKSUM_NAME].decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ManifestError("invalid manifest.sha256 format") from exc
    match = re.fullmatch(r"([0-9a-f]{64})  manifest\.json", checksum_line)
    if not match:
        raise ManifestError("invalid manifest.sha256 format")
    manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
    if not hmac.compare_digest(match.group(1), manifest_sha256):
        raise ManifestError("manifest checksum mismatch")
    document = _validate_document(_json_from_bytes(manifest_payload, label="manifest"))
    _validate_resource_budget(document, budgets)
    if manifest_payload != _canonical_json(document):
        raise ManifestError("manifest must use canonical JSON encoding")
    return document, manifest_sha256


def _scan_artifact_set(
    root_fd: int,
    expected: set[str],
    budgets: VerificationBudgets,
    *,
    signed: bool,
) -> None:
    allowed = expected | {MANIFEST_NAME, CHECKSUM_NAME}
    if signed:
        allowed.add(SIGNATURE_NAME)
    scanned_entries = 0
    scan_budget = budgets.artifact_count * 4 + 1024
    for directory, directory_names, file_names, directory_fd in os.fwalk(
        ".", topdown=True, follow_symlinks=False, dir_fd=root_fd
    ):
        base = PurePosixPath() if directory == "." else PurePosixPath(directory)
        for name in (*directory_names, *file_names):
            scanned_entries += 1
            if scanned_entries > scan_budget:
                raise ManifestError(
                    "backup tree entries exceed the configured traversal budget"
                )
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                relative = (base / name).as_posix()
                raise ManifestError(f"symlink is forbidden in backup: {relative}")
        for name in file_names:
            relative = (base / name).as_posix()
            if relative not in allowed:
                raise ManifestError(
                    "backup artifact set mismatch; missing=[]; "
                    f"unexpected={[relative]!r}"
                )


def _read_signed_structured_artifact(
    root_fd: int,
    artifact: Mapping[str, Any],
    budgets: VerificationBudgets,
) -> Any:
    relative = _safe_relative(str(artifact["path"]))
    expected_size = int(artifact["size_bytes"])
    if expected_size > budgets.manifest_bytes:
        raise ManifestError(
            f"structured artifact exceeds the metadata byte budget: {relative}"
        )
    descriptor = _open_relative_regular(
        root_fd, relative, label=f"artifact {relative.as_posix()}"
    )
    try:
        before = os.fstat(descriptor)
        if before.st_size != expected_size:
            raise ManifestError(f"artifact size mismatch: {relative.as_posix()}")
        chunks: list[bytes] = []
        remaining = expected_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ManifestError(f"artifact size mismatch: {relative.as_posix()}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ManifestError(f"artifact size mismatch: {relative.as_posix()}")
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if _stable_file_identity(before) != _stable_file_identity(after):
            raise ManifestError(
                f"artifact changed while being read: {relative.as_posix()}"
            )
    finally:
        os.close(descriptor)
    if not hmac.compare_digest(
        hashlib.sha256(payload).hexdigest(), str(artifact["sha256"])
    ):
        raise ManifestError(f"artifact checksum mismatch: {relative.as_posix()}")
    return _json_from_bytes(payload, label=f"artifact {relative.as_posix()}")


def _verify_artifacts_and_metadata(
    root_fd: int,
    document: Mapping[str, Any],
    budgets: VerificationBudgets,
    *,
    signed: bool,
) -> dict[str, Any]:
    expected = {artifact["path"]: artifact for artifact in document["artifacts"]}
    required_artifacts = {
        "metadata/release-metadata.json",
        "metadata/running-images.json",
    }
    missing_required = sorted(required_artifacts - set(expected))
    if missing_required:
        raise ManifestError(
            "required signed backup artifact(s) missing: " + ", ".join(missing_required)
        )
    for relative, expected_artifact in expected.items():
        actual_checksum = _hash_signed_artifact(
            root_fd,
            _safe_relative(relative),
            expected_size=expected_artifact["size_bytes"],
        )
        if not hmac.compare_digest(actual_checksum, expected_artifact["sha256"]):
            raise ManifestError(f"artifact checksum mismatch: {relative}")
    _scan_artifact_set(root_fd, set(expected), budgets, signed=signed)
    release_metadata_artifact = expected["metadata/release-metadata.json"]
    release_metadata = _validate_release_metadata(
        _read_signed_structured_artifact(root_fd, release_metadata_artifact, budgets)
    )
    source = document["source"]
    if release_metadata != source["release_metadata"]:
        raise ManifestError("release metadata artifact does not match backup source")
    if not hmac.compare_digest(
        release_metadata_artifact["sha256"], source["release_metadata_sha256"]
    ):
        raise ManifestError("release metadata artifact checksum does not match source")
    running_images_artifact = expected["metadata/running-images.json"]
    running_images = _validate_running_images(
        _read_signed_structured_artifact(root_fd, running_images_artifact, budgets),
        release_metadata,
    )
    if running_images != source["running_images"]:
        raise ManifestError(
            "running image evidence artifact does not match backup source"
        )
    if not hmac.compare_digest(
        running_images_artifact["sha256"], source["running_images_sha256"]
    ):
        raise ManifestError("running image evidence checksum does not match source")
    return {
        "status": "verified",
        "backup_id": document["backup_id"],
        "artifact_count": len(expected),
        "git_commit": document["source"]["git_commit"],
        "release_version": document["source"]["release_version"],
        "release_metadata_sha256": document["source"]["release_metadata_sha256"],
        "running_images_sha256": document["source"]["running_images_sha256"],
    }


def _verify_unsigned_manifest_integrity(
    root_raw: str | Path, budgets: VerificationBudgets
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = _safe_root(root_raw)
    root_fd = _open_directory_fd(root)
    try:
        payloads = _read_control_payloads(root_fd, budgets, signed=False)
        document, _manifest_sha256 = _validated_manifest_controls(payloads, budgets)
        summary = _verify_artifacts_and_metadata(
            root_fd, document, budgets, signed=False
        )
        if _read_control_payloads(root_fd, budgets, signed=False) != payloads:
            raise ManifestError("manifest control files changed during verification")
        return document, summary
    finally:
        os.close(root_fd)


def _signature_statement(
    document: Mapping[str, Any], manifest_sha256: str, key_id: str
) -> dict[str, str]:
    if not SHA256_RE.fullmatch(manifest_sha256) or not KEY_ID_RE.fullmatch(key_id):
        raise ManifestError("backup manifest signature identity is invalid")
    return {
        "schema_version": SIGNATURE_SCHEMA,
        "algorithm": SIGNATURE_ALGORITHM,
        "purpose": SIGNATURE_PURPOSE,
        "key_id": key_id,
        "manifest_sha256": manifest_sha256,
        "backup_id": str(document["backup_id"]),
        "created_at_utc": str(document["created_at_utc"]),
        "source_commit": str(document["source"]["git_commit"]),
    }


def _openssl_sign(payload: bytes, private_key: Path) -> bytes:
    with tempfile.TemporaryDirectory(prefix="auris-backup-signature.") as temporary:
        temporary_root = Path(temporary)
        payload_path = temporary_root / "statement.json"
        signature_path = temporary_root / "signature.bin"
        payload_path.write_bytes(payload)
        payload_path.chmod(0o600)
        _run_openssl(
            [
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                str(private_key),
                "-in",
                str(payload_path),
                "-out",
                str(signature_path),
            ],
            label="backup manifest signature",
        )
        _regular_file(signature_path, label="backup manifest signature output")
        signature = signature_path.read_bytes()
    if len(signature) != 64:
        raise ManifestError("Ed25519 backup manifest signature length is invalid")
    return signature


def _openssl_verify(payload: bytes, signature: bytes, public_key: Path) -> None:
    if len(signature) != 64:
        raise ManifestError("Ed25519 backup manifest signature length is invalid")
    with tempfile.TemporaryDirectory(prefix="auris-backup-verification.") as temporary:
        temporary_root = Path(temporary)
        payload_path = temporary_root / "statement.json"
        signature_path = temporary_root / "signature.bin"
        payload_path.write_bytes(payload)
        signature_path.write_bytes(signature)
        payload_path.chmod(0o600)
        signature_path.chmod(0o600)
        _run_openssl(
            [
                "pkeyutl",
                "-verify",
                "-rawin",
                "-pubin",
                "-inkey",
                str(public_key),
                "-in",
                str(payload_path),
                "-sigfile",
                str(signature_path),
            ],
            label="backup manifest signature",
        )


def sign_manifest(args: argparse.Namespace) -> int:
    budgets = _verification_budgets()
    root = _safe_root(args.root)
    document, summary = _verify_unsigned_manifest_integrity(root, budgets)
    signature_path = root / SIGNATURE_NAME
    if signature_path.exists() or signature_path.is_symlink():
        raise ManifestError("refusing to overwrite an existing manifest signature")
    private_key, public_key, key_id = _validated_key_pair(
        args.private_key, args.public_key
    )
    _require_external_key(root, private_key)
    _require_external_key(root, public_key)
    if hmac.compare_digest(key_id, str(document["restore_attestation"]["key_id"])):
        raise ManifestError(
            "backup manifest and restore attestation must use distinct keys"
        )
    statement = _signature_statement(
        document, _sha256_file(root / MANIFEST_NAME), key_id
    )
    signature = _openssl_sign(_canonical_json(statement), private_key)
    envelope = {
        **statement,
        "signature_base64": base64.b64encode(signature).decode("ascii"),
    }
    envelope_payload = _canonical_json(envelope)
    if len(envelope_payload) > budgets.signature_bytes:
        raise ManifestError(
            f"{SIGNATURE_NAME} exceeds the configured signature byte budget"
        )
    temporary = root / f".{SIGNATURE_NAME}.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise ManifestError("manifest signature temporary path already exists")
    temporary.write_bytes(envelope_payload)
    temporary.chmod(0o600)
    os.replace(temporary, signature_path)
    print(
        json.dumps(
            {
                "status": "signed",
                "backup_id": summary["backup_id"],
                "key_id": key_id,
                "manifest_sha256": statement["manifest_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


def _verify_manifest_signature(
    root: Path,
    document: Mapping[str, Any],
    public_key_raw: str | Path,
    envelope: Any,
    manifest_sha256: str,
) -> dict[str, str]:
    public_key = _key_file(public_key_raw, private=False)
    _require_external_key(root, public_key)
    trusted_key_id = _key_id(_ed25519_public_der(public_key, private=False))
    required = {
        "schema_version",
        "algorithm",
        "purpose",
        "key_id",
        "manifest_sha256",
        "backup_id",
        "created_at_utc",
        "source_commit",
        "signature_base64",
    }
    if not isinstance(envelope, dict) or set(envelope) != required:
        raise ManifestError("backup manifest signature envelope is invalid")
    statement = {key: envelope[key] for key in required - {"signature_base64"}}
    expected_statement = _signature_statement(document, manifest_sha256, trusted_key_id)
    if envelope.get("key_id") != trusted_key_id:
        raise ManifestError("backup manifest signature key identity is untrusted")
    if hmac.compare_digest(
        trusted_key_id, str(document["restore_attestation"]["key_id"])
    ):
        raise ManifestError(
            "backup manifest and restore attestation must use distinct keys"
        )
    if statement != expected_statement:
        raise ManifestError(
            "backup manifest signature identity does not match manifest"
        )
    raw_signature = envelope.get("signature_base64")
    if not isinstance(raw_signature, str):
        raise ManifestError("backup manifest signature encoding is invalid")
    try:
        signature = base64.b64decode(raw_signature, validate=True)
    except (ValueError, TypeError) as exc:
        raise ManifestError("backup manifest signature encoding is invalid") from exc
    _openssl_verify(_canonical_json(statement), signature, public_key)
    return expected_statement


def _verified_controls_from_fd(
    root: Path,
    root_fd: int,
    public_key_raw: str | Path,
    budgets: VerificationBudgets,
) -> tuple[dict[str, Any], dict[str, str], dict[str, bytes]]:
    payloads = _read_control_payloads(root_fd, budgets, signed=True)
    document, manifest_sha256 = _validated_manifest_controls(payloads, budgets)
    envelope = _json_from_bytes(payloads[SIGNATURE_NAME], label="manifest signature")
    statement = _verify_manifest_signature(
        root,
        document,
        public_key_raw,
        envelope,
        manifest_sha256,
    )
    return document, statement, payloads


def _verify_trusted_backup(
    root_raw: str | Path,
    public_key_raw: str | Path,
    budgets: VerificationBudgets,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    root = _safe_root(root_raw)
    root_fd = _open_directory_fd(root)
    try:
        document, statement, control_payloads = _verified_controls_from_fd(
            root, root_fd, public_key_raw, budgets
        )
        summary = _verify_artifacts_and_metadata(
            root_fd, document, budgets, signed=True
        )
        final_payloads = _read_control_payloads(root_fd, budgets, signed=True)
        if final_payloads != control_payloads:
            raise ManifestError("manifest control files changed during verification")
        summary.update(
            {
                "created_at_utc": statement["created_at_utc"],
                "manifest_sha256": statement["manifest_sha256"],
                "signing_key_id": statement["key_id"],
                "restore_attestation_key_id": document["restore_attestation"]["key_id"],
            }
        )
        return document, summary, statement
    finally:
        os.close(root_fd)


def verify_manifest(args: argparse.Namespace) -> int:
    _document, summary, _statement = _verify_trusted_backup(
        args.root,
        args.public_key,
        _verification_budgets(),
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


def inspect_manifest(args: argparse.Namespace) -> int:
    document, _summary, signature = _verify_trusted_backup(
        args.root,
        args.public_key,
        _verification_budgets(),
    )
    print(
        json.dumps(
            {
                "backup_id": document["backup_id"],
                "created_at_utc": document["created_at_utc"],
                "git_commit": document["source"]["git_commit"],
                "release_version": document["source"]["release_version"],
                "release_metadata": document["source"]["release_metadata"],
                "release_metadata_sha256": document["source"][
                    "release_metadata_sha256"
                ],
                "running_images": document["source"]["running_images"],
                "running_images_sha256": document["source"]["running_images_sha256"],
                "manifest_sha256": signature["manifest_sha256"],
                "signing_key_id": signature["key_id"],
                "restore_attestation_key_id": document["restore_attestation"]["key_id"],
            },
            sort_keys=True,
        )
    )
    return 0


def verify_key_pair(args: argparse.Namespace) -> int:
    _private_key, _public_key, key_id = _validated_key_pair(
        args.private_key, args.public_key
    )
    print(json.dumps({"algorithm": SIGNATURE_ALGORITHM, "key_id": key_id}))
    return 0


def public_key_identity(args: argparse.Namespace) -> int:
    public_key = _key_file(args.public_key, private=False)
    key_id = _key_id(_ed25519_public_der(public_key, private=False))
    print(json.dumps({"algorithm": SIGNATURE_ALGORITHM, "key_id": key_id}))
    return 0


def build_counts(args: argparse.Namespace) -> int:
    mysql_counts: dict[str, int] = {}
    with Path(args.mysql_tsv).open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            line = raw_line.rstrip("\n")
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) != 3 or not fields[2].isdigit():
                raise ManifestError(f"invalid MySQL count at line {line_number}")
            schema, table, count = fields
            if not re.fullmatch(r"[A-Za-z0-9_]+", schema) or not re.fullmatch(
                r"[A-Za-z0-9_]+", table
            ):
                raise ManifestError("unexpected MySQL schema/table identifier")
            mysql_counts[f"{schema}.{table}"] = int(count)
    minio = _load_json(Path(args.minio_plan), label="MinIO version plan")
    qdrant: dict[str, Any] = {"included": False, "collections": {}, "points_total": 0}
    if args.qdrant_metadata:
        raw_qdrant = _load_json(Path(args.qdrant_metadata), label="Qdrant metadata")
        collections = (
            raw_qdrant.get("collections") if isinstance(raw_qdrant, dict) else None
        )
        if not isinstance(collections, list):
            raise ManifestError("invalid Qdrant metadata")
        qdrant_counts: dict[str, int] = {}
        for collection in collections:
            name = collection.get("name")
            points = collection.get("points_count")
            if not isinstance(name, str) or not isinstance(points, int) or points < 0:
                raise ManifestError("invalid Qdrant collection count")
            qdrant_counts[name] = points
        qdrant = {
            "included": True,
            "collections": dict(sorted(qdrant_counts.items())),
            "points_total": sum(qdrant_counts.values()),
        }
    summary = minio.get("summary") if isinstance(minio, dict) else None
    if not isinstance(summary, dict):
        raise ManifestError("MinIO plan summary is missing")
    document = {
        "mysql": {
            "tables": dict(sorted(mysql_counts.items())),
            "rows_total": sum(mysql_counts.values()),
        },
        "minio": {
            "object_keys": int(summary.get("object_keys", 0)),
            "versions": int(summary.get("versions", 0)),
            "delete_markers": int(summary.get("delete_markers", 0)),
            "content_bytes": int(summary.get("content_bytes", 0)),
        },
        "qdrant": qdrant,
        "redis": {"included": bool(args.redis_included), "authoritative": False},
    }
    Path(args.output).write_bytes(_canonical_json(document))
    return 0


def build_tool_versions(args: argparse.Namespace) -> int:
    versions: dict[str, str] = {}
    with Path(args.versions_tsv).open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            line = raw_line.rstrip("\n")
            if not line:
                continue
            fields = line.split("\t", 1)
            if len(fields) != 2 or not re.fullmatch(r"[a-z0-9_.-]+", fields[0]):
                raise ManifestError(f"invalid tool version at line {line_number}")
            value = " ".join(fields[1].split())
            if not value or len(value) > 1024 or "\x00" in value:
                raise ManifestError(f"invalid tool version value at line {line_number}")
            versions[fields[0]] = value
    images: list[Any] = []
    with Path(args.images_jsonl).open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            try:
                image = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ManifestError(
                    f"invalid Compose image JSON at line {line_number}"
                ) from exc
            if not isinstance(image, dict):
                raise ManifestError("Compose image entry must be an object")
            images.append(image)
    Path(args.output).write_bytes(
        _canonical_json(
            {"commands": dict(sorted(versions.items())), "compose_images": images}
        )
    )
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subparsers = root.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--root", required=True)
    create.add_argument("--backup-id", required=True)
    create.add_argument("--created-at-utc", required=True)
    create.add_argument("--git-commit", required=True)
    create.add_argument("--release-version", required=True)
    create.add_argument("--counts", required=True)
    create.add_argument("--tool-versions", required=True)
    create.add_argument("--release-metadata", required=True)
    create.add_argument("--running-images", required=True)
    create.add_argument("--restore-attestation-public-key", required=True)
    create.set_defaults(handler=create_manifest)

    sign = subparsers.add_parser("sign")
    sign.add_argument("--root", required=True)
    sign.add_argument("--private-key", required=True)
    sign.add_argument("--public-key", required=True)
    sign.set_defaults(handler=sign_manifest)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--root", required=True)
    verify.add_argument("--public-key", required=True)
    verify.set_defaults(handler=verify_manifest)

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--root", required=True)
    inspect.add_argument("--public-key", required=True)
    inspect.set_defaults(handler=inspect_manifest)

    key_pair = subparsers.add_parser("verify-key-pair")
    key_pair.add_argument("--private-key", required=True)
    key_pair.add_argument("--public-key", required=True)
    key_pair.set_defaults(handler=verify_key_pair)

    key_id = subparsers.add_parser("key-id")
    key_id.add_argument("--public-key", required=True)
    key_id.set_defaults(handler=public_key_identity)

    counts = subparsers.add_parser("build-counts")
    counts.add_argument("--mysql-tsv", required=True)
    counts.add_argument("--minio-plan", required=True)
    counts.add_argument("--qdrant-metadata")
    counts.add_argument("--redis-included", action="store_true")
    counts.add_argument("--output", required=True)
    counts.set_defaults(handler=build_counts)

    versions = subparsers.add_parser("build-tool-versions")
    versions.add_argument("--versions-tsv", required=True)
    versions.add_argument("--images-jsonl", required=True)
    versions.add_argument("--output", required=True)
    versions.set_defaults(handler=build_tool_versions)

    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--source", required=True)
    snapshot.add_argument("--snapshot-root", required=True)
    snapshot.add_argument("--public-key", required=True)
    snapshot.set_defaults(handler=snapshot_backup)

    destroy = subparsers.add_parser("destroy-snapshot")
    destroy.add_argument("--snapshot-root", required=True)
    destroy.set_defaults(handler=destroy_snapshot)
    return root


def main() -> int:
    try:
        args = parser().parse_args()
        return int(args.handler(args))
    except (ManifestError, OSError) as exc:
        print(f"backup manifest error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
