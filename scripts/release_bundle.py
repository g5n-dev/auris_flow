#!/usr/bin/env python3
"""Assemble and verify the self-contained Auris Flow deployment bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any


METADATA_SCHEMA = "auris.release-deployment-metadata.v3"
IMAGE_LOCK_SCHEMA = "auris.release-image-lock.v1"
RESTORE_POLICY_SCHEMA = "auris.release-restore-compatibility.v1"
METADATA_RELATIVE_PATH = PurePosixPath("production/release-metadata.json")
METADATA_SIGNATURE_RELATIVE_PATH = PurePosixPath(
    "production/release-metadata.sigstore.json"
)
COMPOSE_RELATIVE_PATH = PurePosixPath("production/compose.yaml")
IMAGE_LOCK_RELATIVE_PATH = PurePosixPath("production/images.lock.json")
RESTORE_POLICY_RELATIVE_PATH = PurePosixPath("production/restore-compatibility.json")
SIGSTORE_ISSUER = "https://token.actions.githubusercontent.com"
REGULAR_MEMBER_TYPE = "regular-file"
ALLOWED_MEMBER_MODES = frozenset({"0600", "0644", "0755"})
RELEASE_METADATA_MODE = 0o644
INSTALLED_SIGNATURE_MODE = 0o444
BUNDLE_DIRECTORY_MODE = 0o755
OFFICIAL_RELEASE_WORKFLOW_PREFIX = (
    "https://github.com/g5n-dev/auris_flow/.github/workflows/"
    "release-images.yml@refs/tags/"
)
RELEASE_TAG_PATTERN = re.compile(
    r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-rc\.[1-9]\d*)?"
)
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
REQUIRED_BUNDLE_FILES = (
    "README.md",
    "VERSION",
    "LICENSE",
    "NOTICE",
    "CHANGELOG.md",
    "THIRD_PARTY_NOTICES.md",
    "SECURITY.md",
    "RELEASE_CHECKLIST.md",
    "production/.env.example",
    "production/README.md",
    "production/compose.yaml",
    "production/compose.oidc-confidential.yaml",
    "production/images.lock.json",
    "production/release-metadata.json",
    "production/restore-compatibility.json",
    "production/scripts/init-secrets.sh",
    "production/scripts/backup.sh",
    "production/scripts/restore.sh",
    "production/scripts/finalize-restore.sh",
    "production/scripts/verify-backup.sh",
    "production/backup/restore_state.py",
    "scripts/release_bundle.py",
    "scripts/run_with_deadline.py",
    "scripts/verify_production_compose.py",
    "doc/backend-spec/migration-plan.md",
    "doc/release/versioning-and-compatibility.md",
    "doc/runbooks/backup-restore.md",
    "doc/runbooks/key-rotation.md",
    "doc/runbooks/operations.md",
    "doc/runbooks/release-supply-chain.md",
    "doc/runbooks/security-incident-response.md",
    "doc/runbooks/upgrade-rollback.md",
)
PRODUCTION_DIRECTORIES = (
    "backup",
    "keycloak",
    "minio",
    "mysql",
    "observability",
    "qdrant",
    "scripts",
)
ROOT_GOVERNANCE_FILES = (
    "VERSION",
    "LICENSE",
    "NOTICE",
    "CHANGELOG.md",
    "THIRD_PARTY_NOTICES.md",
    "SECURITY.md",
    "RELEASE_CHECKLIST.md",
)
DOCUMENTATION_FILES = (
    "doc/backend-spec/migration-plan.md",
    "doc/release/versioning-and-compatibility.md",
    "doc/runbooks/backup-restore.md",
    "doc/runbooks/key-rotation.md",
    "doc/runbooks/operations.md",
    "doc/runbooks/release-supply-chain.md",
    "doc/runbooks/security-incident-response.md",
    "doc/runbooks/upgrade-rollback.md",
)


class ReleaseBundleError(RuntimeError):
    """Raised when a deployment bundle is incomplete or not commit-bound."""


def _canonical_json(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_regular_file(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ReleaseBundleError(f"missing {label}: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ReleaseBundleError(f"{label} must be a regular file, not a symlink")
    return path


def _require_real_directory(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ReleaseBundleError(f"missing {label}: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseBundleError(f"{label} must be a real directory, not a symlink")
    return path.resolve()


def _load_json(path: Path, label: str) -> Any:
    _require_regular_file(path, label)

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError(f"duplicate JSON key: {key}")
            document[key] = value
        return document

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReleaseBundleError(f"invalid JSON in {label}") from exc


def _validate_member_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ReleaseBundleError("unsafe bundle member path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative in {METADATA_RELATIVE_PATH, METADATA_SIGNATURE_RELATIVE_PATH}
    ):
        raise ReleaseBundleError("unsafe bundle member path")
    return value


def _member_mode(path: Path) -> str:
    return f"0{stat.S_IMODE(path.lstat().st_mode):03o}"


def _bundle_member_record(bundle_root: Path, path: Path) -> dict[str, str]:
    relative_path = path.relative_to(bundle_root).as_posix()
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ReleaseBundleError(
            f"bundle member must be a regular file: {relative_path}"
        )
    if metadata.st_nlink != 1:
        raise ReleaseBundleError(f"hard links are forbidden in bundle: {relative_path}")
    mode = _member_mode(path)
    return {
        "path": _validate_member_path(relative_path),
        "sha256": _sha256_file(path),
        "type": REGULAR_MEMBER_TYPE,
        "mode": mode,
    }


def _collect_bundle_members(bundle_root: Path) -> list[dict[str, str]]:
    members: list[dict[str, str]] = []
    for path in sorted(bundle_root.rglob("*")):
        relative = PurePosixPath(path.relative_to(bundle_root).as_posix())
        if relative in {METADATA_RELATIVE_PATH, METADATA_SIGNATURE_RELATIVE_PATH}:
            continue
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        members.append(_bundle_member_record(bundle_root, path))
    return members


def _validate_bundle_members(document: Any) -> list[dict[str, str]]:
    if not isinstance(document, list) or not document:
        raise ReleaseBundleError("release metadata members must be a non-empty list")
    members: list[dict[str, str]] = []
    paths: set[str] = set()
    for raw_member in document:
        if not isinstance(raw_member, dict) or set(raw_member) != {
            "path",
            "sha256",
            "type",
            "mode",
        }:
            raise ReleaseBundleError("release metadata bundle member is invalid")
        path = _validate_member_path(raw_member.get("path"))
        if path in paths:
            raise ReleaseBundleError(f"duplicate bundle member path: {path}")
        paths.add(path)
        digest = raw_member.get("sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ReleaseBundleError(f"bundle member sha256 is invalid: {path}")
        if raw_member.get("type") != REGULAR_MEMBER_TYPE:
            raise ReleaseBundleError(f"bundle member type is invalid: {path}")
        mode = raw_member.get("mode")
        if not isinstance(mode, str) or mode not in ALLOWED_MEMBER_MODES:
            raise ReleaseBundleError(f"bundle member mode is invalid: {path}")
        members.append(
            {
                "path": path,
                "sha256": digest,
                "type": REGULAR_MEMBER_TYPE,
                "mode": mode,
            }
        )
    if [member["path"] for member in members] != sorted(paths):
        raise ReleaseBundleError("release metadata bundle members are not sorted")
    return members


def _validate_release_tag(value: Any) -> str:
    if not isinstance(value, str) or RELEASE_TAG_PATTERN.fullmatch(value) is None:
        raise ReleaseBundleError("release tag must be SemVer or an rc.N prerelease")
    return value


def _validate_source_commit(value: Any) -> str:
    if not isinstance(value, str) or COMMIT_PATTERN.fullmatch(value) is None:
        raise ReleaseBundleError("source commit must be a complete lowercase Git id")
    return value


def _validate_image_reference(value: Any) -> str:
    if not isinstance(value, str) or value.count("@sha256:") != 1 or "${" in value:
        raise ReleaseBundleError("release images must use TAG@sha256:DIGEST")
    tagged, digest = value.rsplit("@", 1)
    final_segment = tagged.rsplit("/", 1)[-1]
    if (
        ":" not in final_segment
        or final_segment.rsplit(":", 1)[1].casefold() == "latest"
    ):
        raise ReleaseBundleError("release images require an explicit non-latest tag")
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise ReleaseBundleError("release image digest must be lowercase sha256")
    return value


def _validate_image_lock(document: Any) -> dict[str, Any]:
    required = {"schema_version", "release_tag", "source_commit", "images"}
    if not isinstance(document, dict) or set(document) != required:
        raise ReleaseBundleError("image lock has missing or unexpected fields")
    if document.get("schema_version") != IMAGE_LOCK_SCHEMA:
        raise ReleaseBundleError("unsupported image lock schema")
    release_tag = _validate_release_tag(document.get("release_tag"))
    source_commit = _validate_source_commit(document.get("source_commit"))
    raw_images = document.get("images")
    if not isinstance(raw_images, dict) or not raw_images:
        raise ReleaseBundleError("image lock must contain at least one service image")
    images: dict[str, str] = {}
    for service, reference in sorted(raw_images.items()):
        if (
            not isinstance(service, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]*", service) is None
        ):
            raise ReleaseBundleError(f"invalid image-lock service: {service!r}")
        images[service] = _validate_image_reference(reference)
    return {
        "schema_version": IMAGE_LOCK_SCHEMA,
        "release_tag": release_tag,
        "source_commit": source_commit,
        "images": images,
    }


def _validate_restore_policy(document: Any) -> dict[str, Any]:
    required = {"schema_version", "compatible_from"}
    if not isinstance(document, dict) or set(document) != required:
        raise ReleaseBundleError(
            "restore compatibility policy has missing or unexpected fields"
        )
    if document.get("schema_version") != RESTORE_POLICY_SCHEMA:
        raise ReleaseBundleError("unsupported restore compatibility policy schema")
    raw_entries = document.get("compatible_from")
    if not isinstance(raw_entries, list):
        raise ReleaseBundleError("restore compatibility entries must be a list")
    entries: list[dict[str, str]] = []
    identities: set[tuple[str, str, str]] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "release_tag",
            "source_commit",
            "release_metadata_sha256",
        }:
            raise ReleaseBundleError("restore compatibility entry is invalid")
        entry = {
            "release_tag": _validate_release_tag(raw_entry.get("release_tag")),
            "source_commit": _validate_source_commit(raw_entry.get("source_commit")),
            "release_metadata_sha256": str(raw_entry.get("release_metadata_sha256")),
        }
        if re.fullmatch(r"[0-9a-f]{64}", entry["release_metadata_sha256"]) is None:
            raise ReleaseBundleError("restore compatibility metadata sha256 is invalid")
        identity = (
            entry["release_tag"],
            entry["source_commit"],
            entry["release_metadata_sha256"],
        )
        if identity in identities:
            raise ReleaseBundleError("duplicate restore compatibility entry")
        identities.add(identity)
        entries.append(entry)
    entries.sort(
        key=lambda item: (
            item["release_tag"],
            item["source_commit"],
            item["release_metadata_sha256"],
        )
    )
    return {
        "schema_version": RESTORE_POLICY_SCHEMA,
        "compatible_from": entries,
    }


def _validate_compose_images(compose: Any, images: Mapping[str, str]) -> None:
    if not isinstance(compose, dict) or not isinstance(compose.get("services"), dict):
        raise ReleaseBundleError("release Compose document must contain services")
    services = compose["services"]
    if set(services) != set(images):
        raise ReleaseBundleError("release Compose services do not match the image lock")
    for service_name, expected_image in images.items():
        service = services.get(service_name)
        if not isinstance(service, dict):
            raise ReleaseBundleError(f"invalid Compose service: {service_name}")
        if service.get("image") != expected_image:
            raise ReleaseBundleError(
                f"Compose image does not match image lock: {service_name}"
            )
        if "build" in service:
            raise ReleaseBundleError(
                f"release Compose service must not contain build: {service_name}"
            )


def create_release_metadata(
    *,
    release_tag: str,
    source_commit: str,
    compose_file: Path,
    image_lock_file: Path,
    restore_policy_file: Path,
    members: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    release_tag = _validate_release_tag(release_tag)
    source_commit = _validate_source_commit(source_commit)
    compose = _load_json(compose_file, "release Compose document")
    image_lock = _validate_image_lock(_load_json(image_lock_file, "image lock"))
    _validate_restore_policy(
        _load_json(restore_policy_file, "restore compatibility policy")
    )
    if image_lock["release_tag"] != release_tag:
        raise ReleaseBundleError("image lock release tag does not match bundle tag")
    if image_lock["source_commit"] != source_commit:
        raise ReleaseBundleError(
            "image lock source commit does not match bundle commit"
        )
    _validate_compose_images(compose, image_lock["images"])
    validated_members = _validate_bundle_members(list(members))
    return {
        "schema_version": METADATA_SCHEMA,
        "release_tag": release_tag,
        "source_commit": source_commit,
        "compose": {
            "path": COMPOSE_RELATIVE_PATH.as_posix(),
            "sha256": _sha256_file(compose_file),
        },
        "image_lock": {
            "path": IMAGE_LOCK_RELATIVE_PATH.as_posix(),
            "sha256": _sha256_file(image_lock_file),
        },
        "restore_policy": {
            "path": RESTORE_POLICY_RELATIVE_PATH.as_posix(),
            "sha256": _sha256_file(restore_policy_file),
        },
        "images": image_lock["images"],
        "members": validated_members,
    }


def _validate_release_metadata(document: Any) -> dict[str, Any]:
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
    if not isinstance(document, dict) or set(document) != required:
        raise ReleaseBundleError("release metadata has missing or unexpected fields")
    if document.get("schema_version") != METADATA_SCHEMA:
        raise ReleaseBundleError("unsupported release metadata schema")
    release_tag = _validate_release_tag(document.get("release_tag"))
    source_commit = _validate_source_commit(document.get("source_commit"))
    validated_files: dict[str, dict[str, str]] = {}
    for key, expected_path in (
        ("compose", COMPOSE_RELATIVE_PATH.as_posix()),
        ("image_lock", IMAGE_LOCK_RELATIVE_PATH.as_posix()),
        ("restore_policy", RESTORE_POLICY_RELATIVE_PATH.as_posix()),
    ):
        value = document.get(key)
        if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
            raise ReleaseBundleError(f"release metadata {key} binding is invalid")
        if value.get("path") != expected_path:
            raise ReleaseBundleError(f"release metadata {key} path is not canonical")
        digest = value.get("sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ReleaseBundleError(f"release metadata {key} sha256 is invalid")
        validated_files[key] = {"path": expected_path, "sha256": digest}
    raw_images = document.get("images")
    if not isinstance(raw_images, dict) or not raw_images:
        raise ReleaseBundleError("release metadata images must be a non-empty map")
    images: dict[str, str] = {}
    for service, reference in sorted(raw_images.items()):
        if (
            not isinstance(service, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]*", service) is None
        ):
            raise ReleaseBundleError("release metadata service name is invalid")
        images[service] = _validate_image_reference(reference)
    members = _validate_bundle_members(document.get("members"))
    return {
        "schema_version": METADATA_SCHEMA,
        "release_tag": release_tag,
        "source_commit": source_commit,
        "compose": validated_files["compose"],
        "image_lock": validated_files["image_lock"],
        "restore_policy": validated_files["restore_policy"],
        "images": images,
        "members": members,
    }


def _resolve_bound_file(bundle_root: Path, raw: str, label: str) -> Path:
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ReleaseBundleError(f"unsafe {label} path")
    cursor = bundle_root
    for part in relative.parts:
        cursor /= part
        try:
            if stat.S_ISLNK(cursor.lstat().st_mode):
                raise ReleaseBundleError(f"symlink is forbidden in {label} path")
        except FileNotFoundError as exc:
            raise ReleaseBundleError(f"missing {label}: {cursor}") from exc
    target = cursor.resolve()
    if not target.is_relative_to(bundle_root):
        raise ReleaseBundleError(f"{label} path escapes the bundle")
    return _require_regular_file(target, label)


def _manifest_parent_directories(paths: Sequence[str]) -> set[str]:
    directories: set[str] = set()
    for raw in paths:
        parent = PurePosixPath(raw).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _verify_exact_bundle_members(
    bundle_root: Path,
    members: Sequence[Mapping[str, str]],
) -> None:
    if stat.S_IMODE(bundle_root.lstat().st_mode) != BUNDLE_DIRECTORY_MODE:
        raise ReleaseBundleError("bundle directory mode must be 0755: .")
    metadata_path = _resolve_bound_file(
        bundle_root, METADATA_RELATIVE_PATH.as_posix(), "release metadata"
    )
    if stat.S_IMODE(metadata_path.lstat().st_mode) != RELEASE_METADATA_MODE:
        raise ReleaseBundleError("release metadata mode must be 0644")

    signature_path = bundle_root / METADATA_SIGNATURE_RELATIVE_PATH
    if signature_path.exists() or signature_path.is_symlink():
        _require_regular_file(signature_path, "release metadata Sigstore bundle")
        if stat.S_IMODE(signature_path.lstat().st_mode) != INSTALLED_SIGNATURE_MODE:
            raise ReleaseBundleError(
                "installed release metadata Sigstore bundle mode must be 0444"
            )

    declared = {member["path"]: dict(member) for member in members}
    actual_records = _collect_bundle_members(bundle_root)
    actual = {member["path"]: member for member in actual_records}
    missing = sorted(set(declared) - set(actual))
    if missing:
        raise ReleaseBundleError("missing bundle member(s): " + ", ".join(missing))
    unexpected = sorted(set(actual) - set(declared))
    if unexpected:
        raise ReleaseBundleError(
            "unexpected bundle member(s): " + ", ".join(unexpected)
        )

    for path, expected in declared.items():
        observed = actual[path]
        if observed["type"] != expected["type"]:
            raise ReleaseBundleError(
                f"bundle member type does not match metadata: {path}"
            )
        if observed["mode"] != expected["mode"]:
            raise ReleaseBundleError(
                f"bundle member mode does not match metadata: {path}"
            )
        if observed["sha256"] != expected["sha256"]:
            raise ReleaseBundleError(
                f"bundle member checksum does not match metadata: {path}"
            )

    actual_directories: set[str] = set()
    for directory_candidate in bundle_root.rglob("*"):
        metadata = directory_candidate.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            relative = directory_candidate.relative_to(bundle_root).as_posix()
            if stat.S_IMODE(metadata.st_mode) != BUNDLE_DIRECTORY_MODE:
                raise ReleaseBundleError(
                    f"bundle directory mode must be 0755: {relative}"
                )
            actual_directories.add(relative)
    expected_directories = _manifest_parent_directories(
        [*declared, METADATA_RELATIVE_PATH.as_posix()]
    )
    unexpected_directories = sorted(actual_directories - expected_directories)
    if unexpected_directories:
        raise ReleaseBundleError(
            "unexpected bundle directory(s): " + ", ".join(unexpected_directories)
        )


def verify_bundle(bundle_root: Path) -> dict[str, Any]:
    bundle_root = _require_real_directory(bundle_root, "bundle root")
    for path in bundle_root.rglob("*"):
        relative = path.relative_to(bundle_root)
        if path.is_symlink():
            raise ReleaseBundleError(f"symlink is forbidden in bundle: {relative}")
        if any(
            part
            in {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
            for part in relative.parts
        ):
            raise ReleaseBundleError(
                f"development-only path is forbidden in bundle: {relative}"
            )
    metadata_path = _resolve_bound_file(
        bundle_root, METADATA_RELATIVE_PATH.as_posix(), "release metadata"
    )
    metadata = _validate_release_metadata(_load_json(metadata_path, "release metadata"))
    _verify_exact_bundle_members(bundle_root, metadata["members"])
    for relative_path in REQUIRED_BUNDLE_FILES:
        _resolve_bound_file(
            bundle_root, relative_path, f"bundle member {relative_path}"
        )
    compose_path = _resolve_bound_file(
        bundle_root, metadata["compose"]["path"], "release Compose document"
    )
    image_lock_path = _resolve_bound_file(
        bundle_root, metadata["image_lock"]["path"], "image lock"
    )
    restore_policy_path = _resolve_bound_file(
        bundle_root,
        metadata["restore_policy"]["path"],
        "restore compatibility policy",
    )
    if _sha256_file(compose_path) != metadata["compose"]["sha256"]:
        raise ReleaseBundleError("release Compose checksum does not match metadata")
    if _sha256_file(image_lock_path) != metadata["image_lock"]["sha256"]:
        raise ReleaseBundleError("image lock checksum does not match metadata")
    if _sha256_file(restore_policy_path) != metadata["restore_policy"]["sha256"]:
        raise ReleaseBundleError(
            "restore compatibility policy checksum does not match metadata"
        )
    image_lock = _validate_image_lock(_load_json(image_lock_path, "image lock"))
    if image_lock["release_tag"] != metadata["release_tag"]:
        raise ReleaseBundleError("image lock and release metadata tags differ")
    if image_lock["source_commit"] != metadata["source_commit"]:
        raise ReleaseBundleError("image lock and release metadata commits differ")
    if image_lock["images"] != metadata["images"]:
        raise ReleaseBundleError("image lock and release metadata images differ")
    _validate_compose_images(
        _load_json(compose_path, "release Compose document"), metadata["images"]
    )
    restore_policy = _validate_restore_policy(
        _load_json(restore_policy_path, "restore compatibility policy")
    )
    current_identity = (metadata["release_tag"], metadata["source_commit"])
    if any(
        (entry["release_tag"], entry["source_commit"]) == current_identity
        for entry in restore_policy["compatible_from"]
    ):
        raise ReleaseBundleError(
            "restore compatibility policy must not list the current release"
        )
    readme = (bundle_root / "README.md").read_text(encoding="utf-8")
    for forbidden in (
        "build/release",
        "compose.release.json",
        "images.lock.env",
        "github.workspace",
        "/home/runner/work",
    ):
        if forbidden in readme:
            raise ReleaseBundleError(
                f"deployment README contains forbidden path: {forbidden}"
            )
    for required_command in (
        "python3 scripts/release_bundle.py verify --bundle-root . --verify-signature",
        "install -m 0444",
        "auris-flow-${RELEASE_TAG}-release-metadata.sigstore.json",
        "production/release-metadata.sigstore.json",
        "--file production/compose.yaml",
        "bash production/scripts/init-secrets.sh",
        "docker --context default compose --project-name auris-flow",
    ):
        if required_command not in readme:
            raise ReleaseBundleError(
                f"deployment README is missing command contract: {required_command}"
            )
    return metadata


def _official_release_workflow_identity(release_tag: str) -> str:
    return f"{OFFICIAL_RELEASE_WORKFLOW_PREFIX}{_validate_release_tag(release_tag)}"


def verify_bundle_signature(
    bundle_root: Path,
    *,
    cosign_binary: str = "cosign",
    signature_bundle: Path | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    metadata = verify_bundle(bundle_root)
    bundle_root = bundle_root.resolve()
    metadata_path = _resolve_bound_file(
        bundle_root, METADATA_RELATIVE_PATH.as_posix(), "release metadata"
    )
    if signature_bundle is None:
        signature_path = _resolve_bound_file(
            bundle_root,
            METADATA_SIGNATURE_RELATIVE_PATH.as_posix(),
            "release metadata Sigstore bundle",
        )
    else:
        signature_path = _require_regular_file(
            signature_bundle, "release metadata Sigstore bundle"
        ).resolve()
    expected_identity = _official_release_workflow_identity(metadata["release_tag"])
    try:
        completed = run(
            (
                cosign_binary,
                "verify-blob",
                "--bundle",
                str(signature_path),
                "--certificate-identity",
                expected_identity,
                "--certificate-oidc-issuer",
                SIGSTORE_ISSUER,
                str(metadata_path),
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseBundleError(
            "Cosign is required to verify signed release metadata"
        ) from exc
    if completed.returncode != 0:
        raise ReleaseBundleError(
            "release metadata signature is missing or has an untrusted workflow identity"
        )
    return metadata


def verify_restore_source(
    *,
    bundle_root: Path,
    backup_release_tag: str,
    backup_source_commit: str,
    backup_metadata_sha256: str,
    verify_signature: bool = False,
    cosign_binary: str = "cosign",
    signature_bundle: Path | None = None,
) -> dict[str, str]:
    metadata = (
        verify_bundle_signature(
            bundle_root,
            cosign_binary=cosign_binary,
            signature_bundle=signature_bundle,
        )
        if verify_signature
        else verify_bundle(bundle_root)
    )
    backup_identity = {
        "release_tag": _validate_release_tag(backup_release_tag),
        "source_commit": _validate_source_commit(backup_source_commit),
        "release_metadata_sha256": backup_metadata_sha256,
    }
    if re.fullmatch(r"[0-9a-f]{64}", backup_metadata_sha256) is None:
        raise ReleaseBundleError("backup release metadata sha256 is invalid")
    if (
        backup_identity["release_tag"] == metadata["release_tag"]
        and backup_identity["source_commit"] == metadata["source_commit"]
        and backup_identity["release_metadata_sha256"]
        == _sha256_file(bundle_root / METADATA_RELATIVE_PATH)
    ):
        return {
            "status": "same-release",
            "target_release_tag": metadata["release_tag"],
            **backup_identity,
        }
    policy_path = _resolve_bound_file(
        bundle_root.resolve(),
        metadata["restore_policy"]["path"],
        "restore compatibility policy",
    )
    policy = _validate_restore_policy(
        _load_json(policy_path, "restore compatibility policy")
    )
    if backup_identity not in policy["compatible_from"]:
        raise ReleaseBundleError(
            "backup release is outside the signed restore compatibility policy"
        )
    return {
        "status": "compatible-predecessor",
        "target_release_tag": metadata["release_tag"],
        **backup_identity,
    }


def _copy_regular_file(
    source: Path, destination: Path, *, mode: int | None = None
) -> None:
    _require_regular_file(source, f"assembly source {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(mode if mode is not None else stat.S_IMODE(source.stat().st_mode))


def _copy_real_tree(source: Path, destination: Path) -> None:
    source = _require_real_directory(source, f"assembly source directory {source}")
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if "__pycache__" in relative.parts or path.name in {
            ".DS_Store",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
        }:
            continue
        if path.is_symlink():
            raise ReleaseBundleError(f"symlink is forbidden in bundle source: {path}")
        if path.is_dir():
            (destination / relative).mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            _copy_regular_file(path, destination / relative)
        else:
            raise ReleaseBundleError(f"unsupported bundle source entry: {path}")


def assemble_bundle(
    *,
    repository_root: Path,
    output_root: Path,
    rendered_compose: Path,
    image_lock_file: Path,
    restore_policy_file: Path | None = None,
    release_tag: str,
    source_commit: str,
) -> dict[str, Any]:
    repository_root = _require_real_directory(repository_root, "repository root")
    rendered_compose = _require_regular_file(
        rendered_compose.resolve(), "rendered release Compose document"
    )
    image_lock_file = _require_regular_file(image_lock_file.resolve(), "image lock")
    restore_policy_file = _require_regular_file(
        (
            restore_policy_file or repository_root / RESTORE_POLICY_RELATIVE_PATH
        ).resolve(),
        "restore compatibility policy",
    )
    if output_root.exists():
        raise ReleaseBundleError("deployment bundle output must not already exist")
    output_root.mkdir(parents=True, mode=0o755)

    _copy_regular_file(
        repository_root / "production/deployment-bundle.README.md",
        output_root / "README.md",
        mode=0o644,
    )
    for relative in ROOT_GOVERNANCE_FILES:
        _copy_regular_file(
            repository_root / relative, output_root / relative, mode=0o644
        )
    _copy_regular_file(
        repository_root / "production/.env.example",
        output_root / "production/.env.example",
        mode=0o600,
    )
    _copy_regular_file(
        repository_root / "production/README.md",
        output_root / "production/README.md",
        mode=0o644,
    )
    _copy_regular_file(
        repository_root / "production/compose.oidc-confidential.yaml",
        output_root / "production/compose.oidc-confidential.yaml",
        mode=0o644,
    )
    for directory in PRODUCTION_DIRECTORIES:
        _copy_real_tree(
            repository_root / "production" / directory,
            output_root / "production" / directory,
        )
    for relative in DOCUMENTATION_FILES:
        _copy_regular_file(
            repository_root / relative, output_root / relative, mode=0o644
        )
    for relative in (
        "scripts/release_bundle.py",
        "scripts/run_with_deadline.py",
        "scripts/verify_production_compose.py",
    ):
        _copy_regular_file(
            repository_root / relative, output_root / relative, mode=0o755
        )

    compose_destination = output_root / COMPOSE_RELATIVE_PATH
    lock_destination = output_root / IMAGE_LOCK_RELATIVE_PATH
    restore_policy_destination = output_root / RESTORE_POLICY_RELATIVE_PATH
    _copy_regular_file(rendered_compose, compose_destination, mode=0o644)
    _copy_regular_file(image_lock_file, lock_destination, mode=0o644)
    _copy_regular_file(restore_policy_file, restore_policy_destination, mode=0o644)
    output_root.chmod(BUNDLE_DIRECTORY_MODE)
    for path in output_root.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            path.chmod(BUNDLE_DIRECTORY_MODE)
    members = _collect_bundle_members(output_root)
    metadata = create_release_metadata(
        release_tag=release_tag,
        source_commit=source_commit,
        compose_file=compose_destination,
        image_lock_file=lock_destination,
        restore_policy_file=restore_policy_destination,
        members=members,
    )
    metadata_destination = output_root / METADATA_RELATIVE_PATH
    metadata_destination.write_bytes(_canonical_json(metadata))
    metadata_destination.chmod(0o644)
    return verify_bundle(output_root)


def _repository_without_tag(reference: str) -> str:
    tagged = reference.split("@", 1)[0]
    prefix, separator, final = tagged.rpartition("/")
    final_repository = final.rsplit(":", 1)[0]
    return f"{prefix}{separator}{final_repository}"


def validate_running_image(
    *, expected: str, configured: Any, image_id: Any, repo_digests: Any
) -> None:
    expected = _validate_image_reference(expected)
    if configured != expected:
        raise ReleaseBundleError(
            "running container was created from an unexpected image"
        )
    if (
        not isinstance(image_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
    ):
        raise ReleaseBundleError("running container image id is invalid")
    if not isinstance(repo_digests, list) or not all(
        isinstance(item, str) for item in repo_digests
    ):
        raise ReleaseBundleError("running image RepoDigests are unavailable")
    expected_digest = expected.rsplit("@", 1)[1]
    expected_repo_digest = f"{_repository_without_tag(expected)}@{expected_digest}"
    if expected_repo_digest not in repo_digests:
        raise ReleaseBundleError(
            "running image content does not match the release digest"
        )


def _parse_compose_ps(payload: str) -> list[dict[str, Any]]:
    payload = payload.strip()
    if not payload:
        return []
    try:
        document = json.loads(payload)
    except json.JSONDecodeError:
        records: list[dict[str, Any]] = []
        for line in payload.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReleaseBundleError(
                    "docker compose ps returned invalid JSON"
                ) from exc
            if not isinstance(item, dict):
                raise ReleaseBundleError("docker compose ps record must be an object")
            records.append(item)
        return records
    if isinstance(document, dict):
        return [document]
    if isinstance(document, list) and all(isinstance(item, dict) for item in document):
        return list(document)
    raise ReleaseBundleError("docker compose ps returned an invalid record set")


def verify_running_images(
    *,
    bundle_root: Path,
    project_directory: Path,
    env_file: Path,
    project_name: str | None,
    docker_context: str | None = None,
    services: Sequence[str],
    include_all_running: bool = False,
    verify_signature: bool = False,
    cosign_binary: str = "cosign",
    signature_bundle: Path | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    metadata = (
        verify_bundle_signature(
            bundle_root,
            cosign_binary=cosign_binary,
            signature_bundle=signature_bundle,
            run=run,
        )
        if verify_signature
        else verify_bundle(bundle_root)
    )
    requested = tuple(dict.fromkeys(services))
    if not requested:
        raise ReleaseBundleError("at least one running service must be verified")
    missing = sorted(set(requested) - set(metadata["images"]))
    if missing:
        raise ReleaseBundleError("unknown release service(s): " + ", ".join(missing))
    docker_command = ["docker"]
    if docker_context:
        if re.fullmatch(r"[A-Za-z0-9_.-]+", docker_context) is None:
            raise ReleaseBundleError("Docker context name is invalid")
        docker_command.extend(("--context", docker_context))
    command = [
        *docker_command,
        "compose",
        "--project-directory",
        str(project_directory),
        "--env-file",
        str(env_file),
        "--file",
        str(bundle_root / COMPOSE_RELATIVE_PATH),
    ]
    if project_name:
        command.extend(("--project-name", project_name))
    completed = run(
        [*command, "ps", "--all", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    records = _parse_compose_ps(completed.stdout)
    if include_all_running:
        running_services = {
            str(record.get("Service"))
            for record in records
            if str(record.get("State", "")).casefold() == "running"
        }
        unknown_running = sorted(running_services - set(metadata["images"]))
        if unknown_running:
            raise ReleaseBundleError(
                "running Compose service is not declared by release metadata: "
                + ", ".join(unknown_running)
            )
        requested = tuple(dict.fromkeys((*requested, *sorted(running_services))))
    verified: dict[str, str] = {}
    for service in requested:
        matches = [record for record in records if record.get("Service") == service]
        if len(matches) != 1:
            raise ReleaseBundleError(
                f"expected exactly one Compose container for service {service}"
            )
        container_id = matches[0].get("ID")
        if str(matches[0].get("State", "")).casefold() != "running":
            raise ReleaseBundleError(f"Compose service is not running: {service}")
        if not isinstance(container_id, str) or not container_id:
            raise ReleaseBundleError(f"Compose container id is missing for {service}")
        container = run(
            [*docker_command, "inspect", container_id],
            check=True,
            capture_output=True,
            text=True,
        )
        container_payload = json.loads(container.stdout)
        if not isinstance(container_payload, list) or len(container_payload) != 1:
            raise ReleaseBundleError(
                f"docker inspect returned invalid data for {service}"
            )
        container_record = container_payload[0]
        configured = (container_record.get("Config") or {}).get("Image")
        image_id = container_record.get("Image")
        image = run(
            [*docker_command, "image", "inspect", str(image_id)],
            check=True,
            capture_output=True,
            text=True,
        )
        image_payload = json.loads(image.stdout)
        if not isinstance(image_payload, list) or len(image_payload) != 1:
            raise ReleaseBundleError(
                f"docker image inspect returned invalid data for {service}"
            )
        validate_running_image(
            expected=metadata["images"][service],
            configured=configured,
            image_id=image_id,
            repo_digests=image_payload[0].get("RepoDigests"),
        )
        verified[service] = metadata["images"][service]
    return {
        "schema_version": "auris.release-running-images.v1",
        "release_tag": metadata["release_tag"],
        "source_commit": metadata["source_commit"],
        "verification_scope": (
            "all-running-release-services"
            if include_all_running
            else "requested-services"
        ),
        "images": verified,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    assemble = commands.add_parser("assemble")
    assemble.add_argument("--repository-root", type=Path, default=Path.cwd())
    assemble.add_argument("--output", type=Path, required=True)
    assemble.add_argument("--rendered-compose", type=Path, required=True)
    assemble.add_argument("--image-lock", type=Path, required=True)
    assemble.add_argument("--restore-policy", type=Path)
    assemble.add_argument("--release-tag", required=True)
    assemble.add_argument("--source-commit", required=True)

    def add_signature_arguments(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--verify-signature", action="store_true")
        subparser.add_argument("--cosign-binary", default="cosign")
        subparser.add_argument("--signature-bundle", type=Path)

    for command in ("verify", "inspect", "identity"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--bundle-root", type=Path, default=Path.cwd())
        add_signature_arguments(subparser)

    running = commands.add_parser("verify-running-images")
    running.add_argument("--bundle-root", type=Path, default=Path.cwd())
    running.add_argument("--project-directory", type=Path, required=True)
    running.add_argument("--env-file", type=Path, required=True)
    running.add_argument("--project-name")
    running.add_argument("--docker-context")
    running.add_argument("--service", action="append", default=[], required=True)
    running.add_argument("--all-running-release-services", action="store_true")
    add_signature_arguments(running)

    restore_source = commands.add_parser("verify-restore-source")
    restore_source.add_argument("--bundle-root", type=Path, default=Path.cwd())
    restore_source.add_argument("--backup-release-tag", required=True)
    restore_source.add_argument("--backup-source-commit", required=True)
    restore_source.add_argument("--backup-metadata-sha256", required=True)
    add_signature_arguments(restore_source)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "assemble":
            metadata = assemble_bundle(
                repository_root=args.repository_root,
                output_root=args.output,
                rendered_compose=args.rendered_compose,
                image_lock_file=args.image_lock,
                restore_policy_file=args.restore_policy,
                release_tag=args.release_tag,
                source_commit=args.source_commit,
            )
        elif args.command == "verify-running-images":
            metadata = verify_running_images(
                bundle_root=args.bundle_root,
                project_directory=args.project_directory,
                env_file=args.env_file,
                project_name=args.project_name,
                docker_context=args.docker_context,
                services=args.service,
                include_all_running=args.all_running_release_services,
                verify_signature=args.verify_signature,
                cosign_binary=args.cosign_binary,
                signature_bundle=args.signature_bundle,
            )
        elif args.command == "verify-restore-source":
            metadata = verify_restore_source(
                bundle_root=args.bundle_root,
                backup_release_tag=args.backup_release_tag,
                backup_source_commit=args.backup_source_commit,
                backup_metadata_sha256=args.backup_metadata_sha256,
                verify_signature=args.verify_signature,
                cosign_binary=args.cosign_binary,
                signature_bundle=args.signature_bundle,
            )
        else:
            metadata = (
                verify_bundle_signature(
                    args.bundle_root,
                    cosign_binary=args.cosign_binary,
                    signature_bundle=args.signature_bundle,
                )
                if args.verify_signature
                else verify_bundle(args.bundle_root)
            )
            if args.command == "identity":
                metadata_path = args.bundle_root / METADATA_RELATIVE_PATH
                print(
                    "\t".join(
                        (
                            metadata["source_commit"],
                            metadata["release_tag"],
                            _sha256_file(metadata_path),
                            metadata["compose"]["sha256"],
                            metadata["image_lock"]["sha256"],
                        )
                    )
                )
                return 0
            if args.command == "inspect":
                metadata = dict(metadata)
                metadata["metadata_sha256"] = _sha256_file(
                    args.bundle_root / METADATA_RELATIVE_PATH
                )
        print(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
    except (
        OSError,
        ReleaseBundleError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as exc:
        print(f"release bundle verification failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
