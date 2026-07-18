#!/usr/bin/env python3
"""Create and verify self-contained Auris Flow backup manifests.

Only Python's standard library is used so operators can verify a backup on a
clean recovery host before Docker or the application is started.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = "auris-flow.backup-manifest/v1"
REQUIRED_AUTHORITIES = ("mysql", "minio")
MANIFEST_NAME = "manifest.json"
CHECKSUM_NAME = "manifest.sha256"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")


class ManifestError(ValueError):
    """Raised when an untrusted backup does not satisfy the manifest schema."""


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


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
    if raw in {MANIFEST_NAME, CHECKSUM_NAME}:
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


def _walk_artifacts(root: Path) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {}
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in directory_names:
            candidate = base / name
            if candidate.is_symlink():
                raise ManifestError(f"symlink is forbidden in backup: {candidate.relative_to(root)}")
        for name in file_names:
            candidate = base / name
            relative = candidate.relative_to(root).as_posix()
            if relative in {MANIFEST_NAME, CHECKSUM_NAME}:
                continue
            _safe_relative(relative)
            _regular_file(candidate, label=f"artifact {relative}")
            artifacts[relative] = {
                "path": relative,
                "sha256": _sha256_file(candidate),
                "size_bytes": candidate.stat().st_size,
            }
    return dict(sorted(artifacts.items()))


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
        raise ManifestError("counts must be deployment-wide and must not be grouped by tenant/project")
    return value


def _validate_document(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(f"manifest schema_version must be {SCHEMA_VERSION}")
    backup_id = document.get("backup_id")
    if not isinstance(backup_id, str) or not BACKUP_ID_RE.fullmatch(backup_id):
        raise ManifestError("invalid backup_id")
    _validate_timestamp(document.get("created_at_utc"))
    source = document.get("source")
    if not isinstance(source, dict):
        raise ManifestError("manifest source is required")
    commit = source.get("git_commit")
    version = source.get("release_version")
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise ManifestError("source.git_commit must be a hexadecimal commit id")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ManifestError("source.release_version is invalid")
    boundary = document.get("storage_boundary")
    if not isinstance(boundary, dict) or boundary.get("operator_assertion") != (
        "encrypted-at-rest-and-copied-off-host"
    ):
        raise ManifestError("backup requires the encrypted external storage assertion")
    if boundary.get("contains_sensitive_data") is not True:
        raise ManifestError("backup must be classified as containing sensitive data")
    authority = document.get("data_authority")
    if not isinstance(authority, dict) or tuple(authority.get("authoritative") or ()) != (
        REQUIRED_AUTHORITIES
    ):
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
        relative = _safe_relative(artifact.get("path"))
        path_text = relative.as_posix()
        if path_text in seen:
            raise ManifestError(f"duplicate artifact: {path_text}")
        seen.add(path_text)
        if not isinstance(artifact.get("size_bytes"), int) or artifact["size_bytes"] < 0:
            raise ManifestError(f"invalid artifact size: {path_text}")
        if not isinstance(artifact.get("sha256"), str) or not SHA256_RE.fullmatch(
            artifact["sha256"]
        ):
            raise ManifestError(f"invalid artifact checksum: {path_text}")
    return document


def create_manifest(args: argparse.Namespace) -> int:
    root = _safe_root(args.root)
    manifest_path = root / MANIFEST_NAME
    checksum_path = root / CHECKSUM_NAME
    if manifest_path.exists() or checksum_path.exists():
        raise ManifestError("refusing to overwrite an existing manifest")
    counts = _validate_counts(_load_json(Path(args.counts), label="counts"))
    tool_versions = _load_json(Path(args.tool_versions), label="tool versions")
    if not isinstance(tool_versions, dict):
        raise ManifestError("tool versions must be a JSON object")
    artifacts = list(_walk_artifacts(root).values())
    required_paths = {"mysql/all-databases.sql.gz", "mysql/table-counts.tsv", "minio/versions.json"}
    missing = sorted(required_paths.difference(item["path"] for item in artifacts))
    if missing:
        raise ManifestError(f"required backup artifact(s) missing: {', '.join(missing)}")
    document = {
        "schema_version": SCHEMA_VERSION,
        "backup_id": args.backup_id,
        "created_at_utc": _validate_timestamp(args.created_at_utc),
        "source": {
            "git_commit": args.git_commit,
            "release_version": args.release_version,
        },
        "storage_boundary": {
            "contains_sensitive_data": True,
            "operator_assertion": "encrypted-at-rest-and-copied-off-host",
            "repository_never_contains_backup_payloads": True,
        },
        "data_authority": {
            "authoritative": list(REQUIRED_AUTHORITIES),
            "derived_or_optional": ["qdrant", "redis"],
            "restore_order": ["mysql", "minio", "qdrant-derived", "redis-cache-optional"],
        },
        "tenant_independent_counts": counts,
        "tool_versions": tool_versions,
        "artifacts": artifacts,
    }
    _validate_document(document)
    manifest_bytes = _canonical_json(document)
    temporary = root / f".{MANIFEST_NAME}.tmp"
    temporary.write_bytes(manifest_bytes)
    os.chmod(temporary, 0o600)
    os.replace(temporary, manifest_path)
    checksum = hashlib.sha256(manifest_bytes).hexdigest()
    checksum_path.write_text(f"{checksum}  {MANIFEST_NAME}\n", encoding="ascii")
    os.chmod(checksum_path, 0o600)
    print(json.dumps({"backup_id": args.backup_id, "artifact_count": len(artifacts)}))
    return 0


def verify_manifest(args: argparse.Namespace) -> int:
    root = _safe_root(args.root)
    manifest_path = root / MANIFEST_NAME
    checksum_path = root / CHECKSUM_NAME
    _regular_file(manifest_path, label="manifest")
    _regular_file(checksum_path, label="manifest checksum")
    checksum_line = checksum_path.read_text(encoding="ascii").strip()
    match = re.fullmatch(r"([0-9a-f]{64})  manifest\.json", checksum_line)
    if not match:
        raise ManifestError("invalid manifest.sha256 format")
    actual_manifest_checksum = _sha256_file(manifest_path)
    if not hmac.compare_digest(match.group(1), actual_manifest_checksum):
        raise ManifestError("manifest checksum mismatch")
    document = _validate_document(_load_json(manifest_path, label="manifest"))
    expected = {artifact["path"]: artifact for artifact in document["artifacts"]}
    actual = _walk_artifacts(root)
    if set(expected) != set(actual):
        missing = sorted(set(expected).difference(actual))
        unexpected = sorted(set(actual).difference(expected))
        raise ManifestError(
            f"backup artifact set mismatch; missing={missing!r}, unexpected={unexpected!r}"
        )
    for relative, expected_artifact in expected.items():
        actual_artifact = actual[relative]
        if actual_artifact["size_bytes"] != expected_artifact["size_bytes"]:
            raise ManifestError(f"artifact size mismatch: {relative}")
        if not hmac.compare_digest(actual_artifact["sha256"], expected_artifact["sha256"]):
            raise ManifestError(f"artifact checksum mismatch: {relative}")
    summary = {
        "status": "verified",
        "backup_id": document["backup_id"],
        "artifact_count": len(expected),
        "git_commit": document["source"]["git_commit"],
        "release_version": document["source"]["release_version"],
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


def inspect_manifest(args: argparse.Namespace) -> int:
    root = _safe_root(args.root)
    document = _validate_document(_load_json(root / MANIFEST_NAME, label="manifest"))
    print(
        json.dumps(
            {
                "backup_id": document["backup_id"],
                "created_at_utc": document["created_at_utc"],
                "git_commit": document["source"]["git_commit"],
                "release_version": document["source"]["release_version"],
            },
            sort_keys=True,
        )
    )
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
        collections = raw_qdrant.get("collections") if isinstance(raw_qdrant, dict) else None
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
                raise ManifestError(f"invalid Compose image JSON at line {line_number}") from exc
            if not isinstance(image, dict):
                raise ManifestError("Compose image entry must be an object")
            images.append(image)
    Path(args.output).write_bytes(
        _canonical_json({"commands": dict(sorted(versions.items())), "compose_images": images})
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
    create.set_defaults(handler=create_manifest)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--root", required=True)
    verify.set_defaults(handler=verify_manifest)

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--root", required=True)
    inspect.set_defaults(handler=inspect_manifest)

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
