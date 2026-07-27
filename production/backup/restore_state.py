#!/usr/bin/env python3
"""Manage the fail-closed Qdrant rebuild restore state transition."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "auris-flow.restore-state/v2"
PENDING_STATUS = "pending-qdrant-rebuild"
COMPLETE_STATUS = "complete"
PENDING_EXIT_CODE = 3
BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
KEY_ID_RE = re.compile(r"^ed25519-sha256:[0-9a-f]{64}$")
CHALLENGE_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_STATE_BYTES = 128 * 1024
MAX_EVIDENCE_BYTES = 64 * 1024
MAX_KEY_BYTES = 16 * 1024
MAX_PRIVATE_SNAPSHOT_BYTES = 256 * 1024
ATTESTATION_SCHEMA = "auris-flow.restore-complete-attestation/v2"
ATTESTATION_ALGORITHM = "ed25519"
ATTESTATION_PURPOSE = "auris-flow-restore-completion"
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")
SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
COLLECTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
REQUIRED_READYZ_CHECKS = frozenset(
    {
        "auth",
        "dagster",
        "database",
        "object_storage",
        "observability",
        "qdrant",
        "redis",
    }
)


class RestoreStateError(ValueError):
    """Raised when restore state cannot be trusted or transitioned."""


def _timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RestoreStateError(f"{label} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RestoreStateError(f"{label} must be an RFC3339 UTC timestamp") from exc
    if parsed.utcoffset() is None:
        raise RestoreStateError(f"{label} must be an RFC3339 UTC timestamp")
    return parsed.astimezone(UTC)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    ).encode("ascii")


def _validate_identity(value: Mapping[str, Any]) -> None:
    if not BACKUP_ID_RE.fullmatch(str(value.get("backup_id") or "")):
        raise RestoreStateError("restore state backup_id is invalid")
    _timestamp(value.get("backup_created_at_utc"), label="backup_created_at_utc")
    if not COMMIT_RE.fullmatch(str(value.get("source_commit") or "")):
        raise RestoreStateError("restore state source_commit is invalid")
    if not SHA256_RE.fullmatch(str(value.get("manifest_sha256") or "")):
        raise RestoreStateError("restore state manifest_sha256 is invalid")
    manifest_signing_key_id = str(value.get("manifest_signing_key_id") or "")
    attestation_key_id = str(value.get("attestation_key_id") or "")
    if not KEY_ID_RE.fullmatch(manifest_signing_key_id):
        raise RestoreStateError("restore state manifest_signing_key_id is invalid")
    if not KEY_ID_RE.fullmatch(attestation_key_id):
        raise RestoreStateError("restore state attestation_key_id is invalid")
    if hmac.compare_digest(manifest_signing_key_id, attestation_key_id):
        raise RestoreStateError(
            "manifest signing and restore attestation keys must be distinct"
        )


def _evidence_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _validate_qdrant_evidence(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "status",
        "collections",
        "verification",
    }:
        raise RestoreStateError("Qdrant finalize evidence is invalid")
    collections = value.get("collections")
    if (
        value.get("status") != "verified"
        or value.get("verification") != "full-fingerprint-and-scoped-probe"
        or not isinstance(collections, dict)
        or len(collections) > 256
    ):
        raise RestoreStateError("Qdrant finalize evidence is invalid")
    for name, count in collections.items():
        if (
            not isinstance(name, str)
            or not COLLECTION_RE.fullmatch(name)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= 1_000_000_000_000
        ):
            raise RestoreStateError("Qdrant finalize evidence is invalid")
    return value


def _validate_running_images_evidence(
    value: object, *, source_commit: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "release_tag",
        "source_commit",
        "verification_scope",
        "images",
    }:
        raise RestoreStateError("running-image finalize evidence is invalid")
    images = value.get("images")
    if (
        value.get("schema_version") != "auris.release-running-images.v1"
        or value.get("source_commit") != source_commit
        or value.get("verification_scope") != "all-running-release-services"
        or not isinstance(value.get("release_tag"), str)
        or not str(value["release_tag"]).strip()
        or not isinstance(images, dict)
        or not images
        or len(images) > 64
    ):
        raise RestoreStateError("running-image finalize evidence is invalid")
    for service, image in images.items():
        if (
            not isinstance(service, str)
            or not SERVICE_RE.fullmatch(service)
            or not isinstance(image, str)
            or not IMAGE_RE.fullmatch(image)
        ):
            raise RestoreStateError("running-image finalize evidence is invalid")
    return value


def _validate_readyz_evidence(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"status", "data"}:
        raise RestoreStateError("readyz finalize evidence is invalid")
    data = value.get("data")
    if not isinstance(data, dict) or set(data) != {
        "status",
        "checks",
        "required_checks",
        "missing_required",
    }:
        raise RestoreStateError("readyz finalize evidence is invalid")
    checks = data.get("checks")
    required = data.get("required_checks")
    if (
        value.get("status") != "ok"
        or data.get("status") != "success"
        or data.get("missing_required") != {}
        or not isinstance(checks, dict)
        or not isinstance(required, list)
        or len(required) != len(set(required))
        or not REQUIRED_READYZ_CHECKS.issubset(set(required))
        or any(
            not isinstance(name, str) or checks.get(name) != "ok" for name in required
        )
    ):
        raise RestoreStateError("readyz finalize evidence is invalid")
    return value


def _validate(value: object) -> dict[str, Any]:
    common = {
        "schema_version",
        "status",
        "backup_id",
        "backup_created_at_utc",
        "source_commit",
        "manifest_sha256",
        "manifest_signing_key_id",
        "attestation_key_id",
        "restore_challenge",
        "pending_at_utc",
    }
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise RestoreStateError("restore state schema is invalid")
    status = value.get("status")
    expected = (
        common
        if status == PENDING_STATUS
        else common | {"governed_finalize", "attestation"}
    )
    if status not in {PENDING_STATUS, COMPLETE_STATUS} or set(value) != expected:
        raise RestoreStateError("restore state shape is invalid")
    _validate_identity(value)
    if not CHALLENGE_RE.fullmatch(str(value.get("restore_challenge") or "")):
        raise RestoreStateError("restore challenge is invalid")
    backup_created = _timestamp(
        value.get("backup_created_at_utc"), label="backup_created_at_utc"
    )
    pending_at = _timestamp(value.get("pending_at_utc"), label="pending_at_utc")
    if pending_at < backup_created:
        raise RestoreStateError("restore pending timestamp predates the backup")
    if status == COMPLETE_STATUS:
        governed = value.get("governed_finalize")
        if not isinstance(governed, dict) or set(governed) != {
            "completed_at_utc",
            "observed_at_utc",
            "qdrant_evidence",
            "qdrant_evidence_sha256",
            "running_images_evidence",
            "running_images_evidence_sha256",
            "readyz_evidence",
            "readyz_evidence_sha256",
            "readyz_http_status",
        }:
            raise RestoreStateError("governed finalize evidence is invalid")
        completed_at = _timestamp(
            governed.get("completed_at_utc"), label="completed_at_utc"
        )
        observed_at = _timestamp(
            governed.get("observed_at_utc"), label="observed_at_utc"
        )
        if observed_at < pending_at:
            raise RestoreStateError("restore observation predates pending state")
        if observed_at > completed_at:
            raise RestoreStateError("restore observation postdates completion")
        if completed_at < pending_at:
            raise RestoreStateError("restore completion predates pending state")
        evidence = (
            (
                "qdrant_evidence",
                _validate_qdrant_evidence(governed.get("qdrant_evidence")),
                "Qdrant",
            ),
            (
                "running_images_evidence",
                _validate_running_images_evidence(
                    governed.get("running_images_evidence"),
                    source_commit=str(value["source_commit"]),
                ),
                "running-image",
            ),
            (
                "readyz_evidence",
                _validate_readyz_evidence(governed.get("readyz_evidence")),
                "readyz",
            ),
        )
        for field, document, label in evidence:
            digest = str(governed.get(f"{field}_sha256") or "")
            if not SHA256_RE.fullmatch(digest) or not hmac.compare_digest(
                digest, _evidence_sha256(document)
            ):
                raise RestoreStateError(f"{label} finalize evidence digest is invalid")
        if governed.get("readyz_http_status") != 200:
            raise RestoreStateError("governed finalize requires readyz HTTP 200")
        attestation = value.get("attestation")
        if not isinstance(attestation, dict) or set(attestation) != {
            "schema_version",
            "algorithm",
            "purpose",
            "key_id",
            "state_sha256",
            "manifest_sha256",
            "manifest_signing_key_id",
            "restore_challenge",
            "observed_at_utc",
            "signature_base64",
        }:
            raise RestoreStateError("restore completion attestation is invalid")
        if (
            attestation.get("schema_version") != ATTESTATION_SCHEMA
            or attestation.get("algorithm") != ATTESTATION_ALGORITHM
            or attestation.get("purpose") != ATTESTATION_PURPOSE
            or attestation.get("key_id") != value.get("attestation_key_id")
            or attestation.get("manifest_sha256") != value.get("manifest_sha256")
            or attestation.get("manifest_signing_key_id")
            != value.get("manifest_signing_key_id")
            or attestation.get("restore_challenge") != value.get("restore_challenge")
            or attestation.get("observed_at_utc") != governed.get("observed_at_utc")
            or not SHA256_RE.fullmatch(str(attestation.get("state_sha256") or ""))
        ):
            raise RestoreStateError(
                "restore completion attestation identity is invalid"
            )
        try:
            signature = base64.b64decode(
                str(attestation.get("signature_base64") or ""), validate=True
            )
        except (ValueError, TypeError) as exc:
            raise RestoreStateError(
                "restore completion attestation signature is invalid"
            ) from exc
        if len(signature) != 64:
            raise RestoreStateError(
                "restore completion attestation signature is invalid"
            )
    if len(_canonical(value)) > MAX_STATE_BYTES:
        raise RestoreStateError("restore state exceeds the size limit")
    return value


def _secure_parent_path(path: Path, *, label: str) -> Path:
    try:
        parent = path.parent.resolve(strict=True)
        metadata = parent.stat()
    except FileNotFoundError as exc:
        raise RestoreStateError(f"{label} parent does not exist") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise RestoreStateError(f"{label} parent must be owner-controlled")
    return parent / path.name


def _read_stable_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    owner_only: bool,
    owner_writable_only: bool = False,
) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except FileNotFoundError as exc:
        raise RestoreStateError(f"{label} does not exist") from exc
    except OSError as exc:
        raise RestoreStateError(f"{label} must be a regular non-symlink file") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RestoreStateError(f"{label} must be a regular non-symlink file")
        mode = stat.S_IMODE(before.st_mode)
        if before.st_uid != os.getuid() or (owner_only and mode & 0o077):
            raise RestoreStateError(f"{label} must be owner-only")
        if owner_writable_only and mode & 0o022:
            raise RestoreStateError(f"{label} must not be group/world writable")
        if before.st_size <= 0 or before.st_size > max_bytes:
            raise RestoreStateError(f"{label} size is invalid")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(payload) > max_bytes or identity_before != identity_after:
            raise RestoreStateError(f"{label} changed while it was read")
        return payload
    finally:
        os.close(descriptor)


def _regular_private_state(path: Path) -> bytes:
    stable_path = _secure_parent_path(path, label="restore state")
    return _read_stable_file(
        stable_path,
        label="restore state",
        max_bytes=MAX_STATE_BYTES,
        owner_only=True,
    )


def _key_bytes(path_raw: str | Path, *, private: bool) -> bytes:
    path = _secure_parent_path(Path(path_raw), label="restore attestation key")
    return _read_stable_file(
        path,
        label="restore attestation key",
        max_bytes=MAX_KEY_BYTES,
        owner_only=private,
        owner_writable_only=True,
    )


def _run_openssl(arguments: list[str]) -> bytes:
    try:
        completed = subprocess.run(
            ["openssl", *arguments],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise RestoreStateError("OpenSSL is required for restore attestation") from exc
    if completed.returncode != 0:
        raise RestoreStateError("restore completion signature verification failed")
    return completed.stdout


def _public_der(path: Path, *, private: bool) -> bytes:
    arguments = ["pkey"]
    if not private:
        arguments.append("-pubin")
    arguments.extend(["-in", str(path), "-pubout", "-outform", "DER"])
    public_der = _run_openssl(arguments)
    if len(public_der) != len(ED25519_SPKI_PREFIX) + 32 or not public_der.startswith(
        ED25519_SPKI_PREFIX
    ):
        raise RestoreStateError("restore attestation key must be Ed25519")
    return public_der


def _key_id(public_der: bytes) -> str:
    return f"ed25519-sha256:{hashlib.sha256(public_der).hexdigest()}"


def _write_private_bytes(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


@contextmanager
def _validated_key_pair(
    private_raw: str | Path,
    public_raw: str | Path,
    *,
    expected_key_id: str,
) -> Any:
    private_payload = _key_bytes(private_raw, private=True)
    public_payload = _key_bytes(public_raw, private=False)
    with tempfile.TemporaryDirectory(prefix="auris-restore-keys.") as temporary:
        root = Path(temporary)
        private_key = root / "private.pem"
        public_key = root / "public.pem"
        _write_private_bytes(private_key, private_payload)
        _write_private_bytes(public_key, public_payload)
        private_public = _public_der(private_key, private=True)
        trusted_public = _public_der(public_key, private=False)
        if not hmac.compare_digest(private_public, trusted_public):
            raise RestoreStateError(
                "restore attestation signing key pair does not match"
            )
        if not hmac.compare_digest(_key_id(trusted_public), expected_key_id):
            raise RestoreStateError(
                "restore attestation key does not match signed backup"
            )
        yield private_key, public_key


def _attestation_statement(
    unsigned_state: Mapping[str, Any], *, key_id: str
) -> dict[str, str]:
    governed = unsigned_state.get("governed_finalize")
    if not isinstance(governed, Mapping):
        raise RestoreStateError("governed finalize evidence is invalid")
    return {
        "schema_version": ATTESTATION_SCHEMA,
        "algorithm": ATTESTATION_ALGORITHM,
        "purpose": ATTESTATION_PURPOSE,
        "key_id": key_id,
        "state_sha256": hashlib.sha256(_canonical(unsigned_state)).hexdigest(),
        "manifest_sha256": str(unsigned_state["manifest_sha256"]),
        "manifest_signing_key_id": str(unsigned_state["manifest_signing_key_id"]),
        "restore_challenge": str(unsigned_state["restore_challenge"]),
        "observed_at_utc": str(governed["observed_at_utc"]),
    }


def _openssl_sign(payload: bytes, private_key: Path) -> bytes:
    with tempfile.TemporaryDirectory(prefix="auris-restore-attestation.") as temporary:
        root = Path(temporary)
        payload_path = root / "statement.json"
        signature_path = root / "signature.bin"
        _write_private_bytes(payload_path, payload)
        _write_private_bytes(signature_path, b"")
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
            ]
        )
        signature = signature_path.read_bytes()
    if len(signature) != 64:
        raise RestoreStateError("restore completion signature is invalid")
    return signature


def _openssl_verify(payload: bytes, signature: bytes, public_key: Path) -> None:
    if len(signature) != 64:
        raise RestoreStateError("restore completion signature is invalid")
    with tempfile.TemporaryDirectory(prefix="auris-restore-verification.") as temporary:
        root = Path(temporary)
        payload_path = root / "statement.json"
        signature_path = root / "signature.bin"
        _write_private_bytes(payload_path, payload)
        _write_private_bytes(signature_path, signature)
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
            ]
        )


def _verify_complete_attestation_with_key(
    document: Mapping[str, Any], public_key: Path
) -> None:
    trusted_key_id = _key_id(_public_der(public_key, private=False))
    if not hmac.compare_digest(
        trusted_key_id, str(document.get("attestation_key_id") or "")
    ):
        raise RestoreStateError("restore attestation key does not match signed backup")
    attestation = document["attestation"]
    assert isinstance(attestation, dict)
    unsigned_state = dict(document)
    unsigned_state.pop("attestation", None)
    expected = _attestation_statement(unsigned_state, key_id=trusted_key_id)
    actual = {key: attestation.get(key) for key in expected}
    if actual != expected:
        raise RestoreStateError("restore completion signature identity is invalid")
    try:
        signature = base64.b64decode(
            str(attestation["signature_base64"]), validate=True
        )
    except (ValueError, TypeError) as exc:
        raise RestoreStateError("restore completion signature is invalid") from exc
    _openssl_verify(_canonical(expected), signature, public_key)


def _verify_complete_attestation(
    document: Mapping[str, Any], public_raw: str | Path
) -> None:
    public_payload = _key_bytes(public_raw, private=False)
    with tempfile.TemporaryDirectory(prefix="auris-restore-public-key.") as temporary:
        public_key = Path(temporary) / "public.pem"
        _write_private_bytes(public_key, public_payload)
        _verify_complete_attestation_with_key(document, public_key)


def _load(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = _regular_private_state(path)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestoreStateError("restore state JSON is invalid") from exc
    document = _validate(value)
    if raw != _canonical(document):
        raise RestoreStateError("restore state must use canonical JSON")
    return document, raw


def _load_evidence(path_raw: str | Path, *, label: str) -> dict[str, Any]:
    raw = _read_stable_file(
        Path(path_raw),
        label=f"{label} evidence",
        max_bytes=MAX_EVIDENCE_BYTES,
        owner_only=True,
    )
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RestoreStateError(f"{label} evidence JSON is invalid") from exc
    if not isinstance(value, dict) or len(_canonical(value)) > MAX_EVIDENCE_BYTES:
        raise RestoreStateError(f"{label} evidence is invalid")
    return value


@contextmanager
def _exclusive_state_lock(path_raw: Path) -> Any:
    path = _secure_parent_path(path_raw, label="restore state")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RestoreStateError("restore state lock could not be acquired") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise RestoreStateError("restore state must be owner-only")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield path
    finally:
        os.close(descriptor)


def _replace_locked_state(path: Path, *, prior: bytes, payload: bytes) -> None:
    parent = _secure_parent_path(path, label="restore state").parent
    directory_fd = os.open(
        parent,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    temporary_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if _regular_private_state(path) != prior:
            raise RestoreStateError("restore state changed concurrently")
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _write_new_private_blob(
    path: Path,
    payload: bytes,
    *,
    label: str,
) -> None:
    path = _secure_parent_path(path, label=label)
    if path.exists() or path.is_symlink():
        raise RestoreStateError(f"refusing to overwrite {label}")
    directory_fd = os.open(
        path.parent,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    temporary_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.fsync(directory_fd)
    except FileExistsError as exc:
        raise RestoreStateError(f"refusing to overwrite {label}") from exc
    finally:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _write_new(path: Path, document: Mapping[str, Any]) -> None:
    payload = _canonical(_validate(dict(document)))
    _write_new_private_blob(path, payload, label="restore state")


def _require_identity(document: Mapping[str, Any], args: argparse.Namespace) -> None:
    expected = {
        "backup_id": args.backup_id,
        "source_commit": args.source_commit,
        "manifest_sha256": args.manifest_sha256,
        "manifest_signing_key_id": args.manifest_signing_key_id,
        "attestation_key_id": args.attestation_key_id,
    }
    if any(document.get(key) != value for key, value in expected.items()):
        raise RestoreStateError("restore state identity does not match signed backup")


def create_pending(args: argparse.Namespace) -> int:
    document = {
        "schema_version": SCHEMA_VERSION,
        "status": PENDING_STATUS,
        "backup_id": args.backup_id,
        "backup_created_at_utc": args.backup_created_at_utc,
        "source_commit": args.source_commit,
        "manifest_sha256": args.manifest_sha256,
        "manifest_signing_key_id": args.manifest_signing_key_id,
        "attestation_key_id": args.attestation_key_id,
        "restore_challenge": secrets.token_hex(32),
        "pending_at_utc": args.pending_at_utc,
    }
    _write_new(Path(args.output), document)
    print(
        json.dumps(
            {
                "restore_challenge": document["restore_challenge"],
                "state": str(args.output),
                "status": PENDING_STATUS,
            },
            sort_keys=True,
        )
    )
    return PENDING_EXIT_CODE


def snapshot_private_file(args: argparse.Namespace) -> int:
    payload = _read_stable_file(
        Path(args.source),
        label="private input",
        max_bytes=MAX_PRIVATE_SNAPSHOT_BYTES,
        owner_only=True,
    )
    output = Path(args.output)
    _write_new_private_blob(output, payload, label="private snapshot")
    print(
        json.dumps(
            {
                "output": str(output),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


def require_pending(args: argparse.Namespace) -> int:
    document, _raw = _load(Path(args.state))
    _require_identity(document, args)
    if document["status"] != PENDING_STATUS:
        raise RestoreStateError("restore state is not pending Qdrant rebuild")
    print(json.dumps(document, sort_keys=True))
    return 0


def finalize(args: argparse.Namespace) -> int:
    with _exclusive_state_lock(Path(args.state)) as path:
        document, prior = _load(path)
        _require_identity(document, args)
        if document["status"] != PENDING_STATUS:
            raise RestoreStateError(
                "only pending Qdrant rebuild state can be finalized"
            )
        qdrant_evidence = _validate_qdrant_evidence(
            _load_evidence(args.qdrant_evidence, label="Qdrant")
        )
        running_images_evidence = _validate_running_images_evidence(
            _load_evidence(args.running_images_evidence, label="running-image"),
            source_commit=str(document["source_commit"]),
        )
        readyz_evidence = _validate_readyz_evidence(
            _load_evidence(args.readyz_evidence, label="readyz")
        )
        unsigned_state = {
            **document,
            "status": COMPLETE_STATUS,
            "governed_finalize": {
                "completed_at_utc": args.completed_at_utc,
                "observed_at_utc": args.observed_at_utc,
                "qdrant_evidence": qdrant_evidence,
                "qdrant_evidence_sha256": _evidence_sha256(qdrant_evidence),
                "running_images_evidence": running_images_evidence,
                "running_images_evidence_sha256": _evidence_sha256(
                    running_images_evidence
                ),
                "readyz_evidence": readyz_evidence,
                "readyz_evidence_sha256": _evidence_sha256(readyz_evidence),
                "readyz_http_status": 200,
            },
        }
        with _validated_key_pair(
            args.private_key,
            args.public_key,
            expected_key_id=str(document["attestation_key_id"]),
        ) as (private_key, public_key):
            statement = _attestation_statement(
                unsigned_state,
                key_id=str(document["attestation_key_id"]),
            )
            signature = _openssl_sign(_canonical(statement), private_key)
            updated = {
                **unsigned_state,
                "attestation": {
                    **statement,
                    "signature_base64": base64.b64encode(signature).decode("ascii"),
                },
            }
            payload = _canonical(_validate(updated))
            _verify_complete_attestation_with_key(updated, public_key)
        _replace_locked_state(path, prior=prior, payload=payload)
    print(json.dumps({"state": str(path), "status": COMPLETE_STATUS}))
    return 0


def verify_complete(args: argparse.Namespace) -> int:
    document, _raw = _load(Path(args.state))
    _require_identity(document, args)
    if document["status"] != COMPLETE_STATUS:
        raise RestoreStateError("restore state is not complete")
    _verify_complete_attestation(document, args.public_key)
    print(json.dumps(document, sort_keys=True))
    return 0


def _identity_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--backup-id", required=True)
    command.add_argument("--source-commit", required=True)
    command.add_argument("--manifest-sha256", required=True)
    command.add_argument("--manifest-signing-key-id", required=True)
    command.add_argument("--attestation-key-id", required=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    pending = commands.add_parser("create-pending")
    pending.add_argument("--output", required=True)
    pending.add_argument("--backup-created-at-utc", required=True)
    pending.add_argument("--pending-at-utc", required=True)
    _identity_arguments(pending)
    pending.set_defaults(handler=create_pending)

    snapshot = commands.add_parser("snapshot-private-file")
    snapshot.add_argument("--source", required=True)
    snapshot.add_argument("--output", required=True)
    snapshot.set_defaults(handler=snapshot_private_file)

    require = commands.add_parser("require-pending")
    require.add_argument("--state", required=True)
    _identity_arguments(require)
    require.set_defaults(handler=require_pending)

    complete = commands.add_parser("finalize")
    complete.add_argument("--state", required=True)
    complete.add_argument("--qdrant-evidence", required=True)
    complete.add_argument("--running-images-evidence", required=True)
    complete.add_argument("--readyz-evidence", required=True)
    complete.add_argument("--observed-at-utc", required=True)
    complete.add_argument("--completed-at-utc", required=True)
    complete.add_argument("--private-key", required=True)
    complete.add_argument("--public-key", required=True)
    _identity_arguments(complete)
    complete.set_defaults(handler=finalize)

    verify = commands.add_parser("verify-complete")
    verify.add_argument("--state", required=True)
    verify.add_argument("--public-key", required=True)
    _identity_arguments(verify)
    verify.set_defaults(handler=verify_complete)
    return root


def main() -> int:
    try:
        args = parser().parse_args()
        return int(args.handler(args))
    except (OSError, RestoreStateError) as exc:
        print(f"restore state error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
