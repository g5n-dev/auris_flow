#!/usr/bin/env python3
"""Plan, bind, replay, and verify every generation of a versioned MinIO bucket."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
import urllib.parse
from collections import defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


PLAN_SCHEMA = "auris-flow.minio-version-plan/v2"
CONTENT_HASH_ALGORITHM = "sha256"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class PlanError(ValueError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def parse_timestamp(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.endswith("Z"):
        raise PlanError("MinIO version is missing lastModified")
    normalized = raw.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PlanError("MinIO version has an invalid UTC lastModified timestamp") from exc
    return raw


def safe_key(raw: Any, bucket: str) -> str:
    del bucket  # mc ls already emits keys relative to the requested bucket/prefix.
    if not isinstance(raw, str) or not raw or CONTROL_RE.search(raw):
        raise PlanError("object key is empty or contains a forbidden control character")
    key = raw
    if not key or key.startswith("/"):
        raise PlanError("object key must be relative to the bucket")
    # Object keys are not filesystem paths, but dot segments are rejected so
    # operator output and generated commands remain unambiguous.
    if any(part in {"", ".", ".."} for part in PurePosixPath(key).parts):
        raise PlanError("object key contains an unsafe path segment")
    return key


def safe_string_map(raw: Any, *, label: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PlanError(f"{label} must be a JSON object")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise PlanError(f"{label} keys and values must be strings")
        if not key or CONTROL_RE.search(key) or CONTROL_RE.search(value):
            raise PlanError(f"{label} contains an unsafe key or value")
        result[key] = value
    return dict(sorted(result.items()))


def _delete_marker(item: dict[str, Any]) -> bool:
    return bool(
        item.get("isDeleteMarker")
        or item.get("deleteMarker")
        or item.get("is_delete_marker")
        or item.get("type") == "delete-marker"
    )


def _records(lines: Iterable[str], bucket: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source_order, raw_line in enumerate(lines):
        if not raw_line.strip():
            continue
        try:
            item = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise PlanError(f"invalid mc JSON at line {source_order + 1}") from exc
        if not isinstance(item, dict):
            raise PlanError("mc listing entry must be an object")
        if item.get("status") == "error":
            raise PlanError("mc reported an error while listing object versions")
        if item.get("type") in {"folder", "directory"}:
            continue
        key = safe_key(item.get("key") or item.get("name"), bucket)
        version_value = item.get("versionId", item.get("version_id", ""))
        version_id = "" if version_value is None else str(version_value)
        if CONTROL_RE.search(version_id):
            raise PlanError("version id contains a forbidden control character")
        identity = (key, version_id)
        if identity in seen:
            raise PlanError("duplicate object key/version id in mc listing")
        seen.add(identity)
        delete_marker = _delete_marker(item)
        size = int(item.get("size") or 0)
        if size < 0 or (delete_marker and size != 0):
            raise PlanError("invalid object version size")
        etag = str(item.get("etag") or item.get("ETag") or "").strip('"')
        metadata = safe_string_map(item.get("metadata"), label="object metadata")
        tags = safe_string_map(item.get("tags"), label="object tags")
        storage_class = str(item.get("storageClass") or item.get("storage_class") or "")
        if CONTROL_RE.search(storage_class):
            raise PlanError("storage class contains a control character")
        artifact: str | None = None
        if not delete_marker:
            if not etag:
                raise PlanError("content version is missing its ETag")
            identity_hash = hashlib.sha256(f"{key}\0{version_id}".encode("utf-8")).hexdigest()
            artifact = f"minio/objects/{identity_hash[:2]}/{identity_hash}.bin"
        records.append(
            {
                "key": key,
                "version_id": version_id,
                "last_modified": parse_timestamp(item.get("lastModified") or item.get("last_modified")),
                "size_bytes": size,
                "etag": etag,
                "metadata": metadata,
                "tags": tags,
                "storage_class": storage_class,
                "delete_marker": delete_marker,
                "artifact": artifact,
                "content_sha256": None,
                "source_order": source_order,
            }
        )
    return records


def build_plan(args: argparse.Namespace) -> int:
    with Path(args.listing).open(encoding="utf-8") as handle:
        records = _records(handle, args.bucket)
    document = {
        "schema_version": PLAN_SCHEMA,
        "bucket": args.bucket,
        "content_hash_algorithm": CONTENT_HASH_ALGORITHM,
        "ordering": "last_modified_ascending_then_reverse_mc_source_order",
        "summary": {
            "object_keys": len({record["key"] for record in records}),
            "versions": len(records),
            "delete_markers": sum(record["delete_marker"] for record in records),
            "content_bytes": sum(
                record["size_bytes"] for record in records if not record["delete_marker"]
            ),
        },
        "versions": records,
    }
    validate_plan(document, require_content_hashes=False)
    Path(args.output).write_text(canonical_json(document), encoding="utf-8")
    return 0


def load_plan(
    path: Path, *, require_content_hashes: bool = True
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PlanError("MinIO plan must be a regular file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError("invalid MinIO plan JSON") from exc
    validate_plan(document, require_content_hashes=require_content_hashes)
    return document


def validate_plan(document: Any, *, require_content_hashes: bool = True) -> None:
    if not isinstance(document, dict) or document.get("schema_version") != PLAN_SCHEMA:
        raise PlanError("unsupported MinIO plan schema")
    if document.get("content_hash_algorithm") != CONTENT_HASH_ALGORITHM:
        raise PlanError("MinIO plan must bind content with SHA-256")
    bucket = document.get("bucket")
    if not isinstance(bucket, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{1,62}", bucket):
        raise PlanError("invalid bucket name")
    versions = document.get("versions")
    if not isinstance(versions, list):
        raise PlanError("MinIO plan versions must be a list")
    seen: set[tuple[str, str]] = set()
    for record in versions:
        if not isinstance(record, dict):
            raise PlanError("MinIO version entry must be an object")
        key = safe_key(record.get("key"), bucket)
        version_id = record.get("version_id")
        if not isinstance(version_id, str) or CONTROL_RE.search(version_id):
            raise PlanError("invalid MinIO version id")
        identity = (key, version_id)
        if identity in seen:
            raise PlanError("duplicate MinIO object generation")
        seen.add(identity)
        parse_timestamp(record.get("last_modified"))
        size = record.get("size_bytes")
        if not isinstance(size, int) or size < 0:
            raise PlanError("invalid MinIO object size")
        delete_marker = record.get("delete_marker")
        if not isinstance(delete_marker, bool):
            raise PlanError("delete_marker must be boolean")
        artifact = record.get("artifact")
        content_sha256 = record.get("content_sha256")
        safe_string_map(record.get("metadata"), label="object metadata")
        safe_string_map(record.get("tags"), label="object tags")
        if any(
            ";" in key or "=" in key or ";" in value
            for key, value in record["metadata"].items()
        ):
            raise PlanError("object metadata cannot be represented safely by mc cp --attr")
        storage_class = record.get("storage_class")
        if not isinstance(storage_class, str) or not re.fullmatch(r"[A-Za-z0-9._-]{0,64}", storage_class):
            raise PlanError("invalid object storage class")
        if delete_marker:
            if artifact is not None or content_sha256 is not None or size != 0:
                raise PlanError("delete marker must not reference object content")
        else:
            if not isinstance(artifact, str):
                raise PlanError("content version is missing its backup artifact")
            etag = record.get("etag")
            if not isinstance(etag, str) or not etag:
                raise PlanError("content version is missing its ETag")
            path = PurePosixPath(artifact)
            if (
                path.is_absolute()
                or path.parts[:2] != ("minio", "objects")
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise PlanError("unsafe MinIO artifact path")
            if content_sha256 is None and not require_content_hashes:
                pass
            elif not isinstance(content_sha256, str) or not SHA256_RE.fullmatch(
                content_sha256
            ):
                raise PlanError("content version is missing its artifact SHA-256")
        source_order = record.get("source_order")
        if not isinstance(source_order, int) or source_order < 0:
            raise PlanError("invalid mc source order")
    summary = document.get("summary")
    expected_summary = {
        "object_keys": len({record["key"] for record in versions}),
        "versions": len(versions),
        "delete_markers": sum(record["delete_marker"] for record in versions),
        "content_bytes": sum(
            record["size_bytes"] for record in versions if not record["delete_marker"]
        ),
    }
    if summary != expected_summary:
        raise PlanError("MinIO plan summary does not match its versions")


def _artifact_file(backup_root: Path, relative: str) -> Path:
    if backup_root.is_symlink() or not backup_root.is_dir():
        raise PlanError("backup root must be a real directory")
    candidate = backup_root
    for part in PurePosixPath(relative).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise PlanError("MinIO artifact path must not contain symlinks")
    if not candidate.is_file():
        raise PlanError(f"MinIO artifact is missing or not a regular file: {relative}")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_observation(backup_root: Path, record: dict[str, Any]) -> tuple[int, str]:
    artifact = _artifact_file(backup_root, record["artifact"])
    size = artifact.stat().st_size
    return size, _sha256_file(artifact)


def bind_artifacts(args: argparse.Namespace) -> int:
    document = load_plan(Path(args.plan), require_content_hashes=False)
    backup_root = Path(args.backup_root)
    for record in document["versions"]:
        if record["delete_marker"]:
            continue
        if record["content_sha256"] is not None:
            raise PlanError("MinIO plan is already content-bound")
        size, digest = _artifact_observation(backup_root, record)
        if size != record["size_bytes"]:
            raise PlanError(
                f"MinIO artifact size mismatch for generation: {record['key']}"
            )
        record["content_sha256"] = digest
    validate_plan(document, require_content_hashes=True)
    output = Path(args.output)
    if output.is_symlink() or not output.parent.is_dir():
        raise PlanError("MinIO bound plan output path is unsafe")
    output.write_text(canonical_json(document), encoding="utf-8")
    return 0


def verify_artifacts(args: argparse.Namespace) -> int:
    document = load_plan(Path(args.plan))
    backup_root = Path(args.backup_root)
    verified = 0
    for record in document["versions"]:
        if record["delete_marker"]:
            continue
        size, digest = _artifact_observation(backup_root, record)
        if size != record["size_bytes"]:
            raise PlanError(
                f"MinIO artifact size mismatch for generation: {record['key']}"
            )
        if digest != record["content_sha256"]:
            raise PlanError(
                f"MinIO artifact checksum mismatch for generation: {record['key']}"
            )
        verified += 1
    print(json.dumps({"status": "verified", "content_generations": verified}))
    return 0


def _shell_header(alias: str, bucket: str) -> list[str]:
    mc = "/opt/auris/minio-client.sh"
    return [
        "#!/bin/sh",
        "set -eu",
        f"{mc} version enable {shlex.quote(alias + '/' + bucket)} >/dev/null",
    ]


def emit_backup_shell(args: argparse.Namespace) -> int:
    document = load_plan(Path(args.plan), require_content_hashes=False)
    lines = _shell_header("auris", document["bucket"])
    for record in document["versions"]:
        if record["delete_marker"]:
            continue
        artifact = "/backup/" + record["artifact"]
        source = f"auris/{document['bucket']}/{record['key']}"
        lines.append(f"mkdir -p {shlex.quote(str(PurePosixPath(artifact).parent))}")
        version_flag = (
            f" --version-id {shlex.quote(record['version_id'])}" if record["version_id"] else ""
        )
        lines.append(
            f"/opt/auris/minio-client.sh cp --quiet{version_flag} "
            f"{shlex.quote(source)} {shlex.quote(artifact)}"
        )
    Path(args.output).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


def ordered_versions(document: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in document["versions"]:
        grouped[record["key"]].append(record)
    ordered: list[dict[str, Any]] = []
    for key in sorted(grouped):
        ordered.extend(
            sorted(
                grouped[key],
                key=lambda item: (item["last_modified"], -item["source_order"]),
            )
        )
    return ordered


def _shell_file_verifier() -> list[str]:
    return [
        "verify_file() {",
        '  expected_size="$1"',
        '  expected_sha256="$2"',
        '  artifact_path="$3"',
        '  generation_label="$4"',
        '  actual_size="$(wc -c <"${artifact_path}")"',
        '  [ "${actual_size}" -eq "${expected_size}" ] || {',
        "    printf 'MinIO generation size mismatch: %s\\n' "
        '"${generation_label}" >&2',
        "    exit 42",
        "  }",
        '  hash_line="$(sha256sum "${artifact_path}")"',
        '  actual_sha256="${hash_line%% *}"',
        '  [ "${actual_sha256}" = "${expected_sha256}" ] || {',
        "    printf 'MinIO generation SHA-256 mismatch: %s\\n' "
        '"${generation_label}" >&2',
        "    exit 43",
        "  }",
        "}",
    ]


def emit_restore_shell(args: argparse.Namespace) -> int:
    document = load_plan(Path(args.plan))
    lines = _shell_header("auris", document["bucket"])
    lines.extend(_shell_file_verifier())
    for record in ordered_versions(document):
        target = f"auris/{document['bucket']}/{record['key']}"
        if record["delete_marker"]:
            lines.append(
                "/opt/auris/minio-client.sh rm --quiet --force "
                f"{shlex.quote(target)}"
            )
        else:
            artifact = "/backup/" + record["artifact"]
            generation_label = f"{record['key']}@{record['version_id']}"
            lines.append(
                "verify_file "
                f"{record['size_bytes']} {record['content_sha256']} "
                f"{shlex.quote(artifact)} {shlex.quote(generation_label)}"
            )
            option_parts: list[str] = []
            if record["metadata"]:
                attributes = ";".join(
                    f"{key}={value}" for key, value in sorted(record["metadata"].items())
                )
                option_parts.extend(["--attr", attributes])
            if record["tags"]:
                option_parts.extend(
                    ["--tags", urllib.parse.urlencode(sorted(record["tags"].items()))]
                )
            if record["storage_class"]:
                option_parts.extend(["--storage-class", record["storage_class"]])
            rendered_options = "".join(f" {shlex.quote(value)}" for value in option_parts)
            lines.append(
                "/opt/auris/minio-client.sh cp --quiet --preserve"
                f"{rendered_options} "
                f"{shlex.quote(artifact)} {shlex.quote(target)}"
            )
    Path(args.output).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


def _listing_document(path: Path, bucket: str) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        records = _records(handle, bucket)
    document = {
        "schema_version": PLAN_SCHEMA,
        "bucket": bucket,
        "content_hash_algorithm": CONTENT_HASH_ALGORITHM,
        "ordering": "last_modified_ascending_then_reverse_mc_source_order",
        "versions": records,
        "summary": {
            "object_keys": len({record["key"] for record in records}),
            "versions": len(records),
            "delete_markers": sum(record["delete_marker"] for record in records),
            "content_bytes": sum(
                record["size_bytes"] for record in records if not record["delete_marker"]
            ),
        },
    }
    validate_plan(document, require_content_hashes=False)
    return document


def _semantic_signature(record: dict[str, Any]) -> tuple[bool, int, str, str, str]:
    return (
        record["delete_marker"],
        record["size_bytes"],
        json.dumps(record["metadata"], sort_keys=True),
        json.dumps(record["tags"], sort_keys=True),
        record["storage_class"],
    )


def _aligned_generations(
    expected: dict[str, Any], actual: dict[str, Any]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    expected_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    actual_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in ordered_versions(expected):
        expected_by_key[record["key"]].append(record)
    for record in ordered_versions(actual):
        actual_by_key[record["key"]].append(record)
    if set(expected_by_key) != set(actual_by_key):
        raise PlanError("restored MinIO object keys differ from the backup plan")
    aligned: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for key in sorted(expected_by_key):
        expected_records = expected_by_key[key]
        actual_records = actual_by_key[key]
        if len(expected_records) != len(actual_records):
            raise PlanError(
                f"restored MinIO generation count differs from the backup plan: {key}"
            )
        for expected_record, actual_record in zip(
            expected_records, actual_records, strict=True
        ):
            if _semantic_signature(expected_record) != _semantic_signature(
                actual_record
            ):
                raise PlanError(
                    f"restored MinIO version order or attributes differ: {key}"
                )
            aligned.append((expected_record, actual_record))
    return aligned


def compare_listing(args: argparse.Namespace) -> int:
    expected = load_plan(Path(args.plan))
    actual = _listing_document(Path(args.listing), expected["bucket"])
    _aligned_generations(expected, actual)
    print(json.dumps({"status": "verified", **actual["summary"]}, sort_keys=True))
    return 0


def emit_verify_shell(args: argparse.Namespace) -> int:
    expected = load_plan(Path(args.plan))
    actual = _listing_document(Path(args.listing), expected["bucket"])
    aligned = _aligned_generations(expected, actual)
    lines = [
        "#!/bin/sh",
        "set -eu",
        'verification_root="$(mktemp -d /tmp/auris-flow-minio-verify.XXXXXX)"',
        "cleanup_verification_root() {",
        '  case "${verification_root}" in',
        "    /tmp/auris-flow-minio-verify.*) "
        'rm -rf -- "${verification_root}" ;;',
        "    *) printf 'unsafe MinIO verification directory\\n' >&2; exit 44 ;;",
        "  esac",
        "}",
        "trap cleanup_verification_root EXIT",
        "trap 'exit 129' HUP",
        "trap 'exit 130' INT",
        "trap 'exit 143' TERM",
    ]
    lines.extend(_shell_file_verifier())
    content_index = 0
    for expected_record, actual_record in aligned:
        if expected_record["delete_marker"]:
            continue
        target_version_id = actual_record["version_id"]
        if not target_version_id:
            raise PlanError(
                "restored content generation is missing its target version id"
            )
        content_index += 1
        verification_file = (
            f'"${{verification_root}}/generation-{content_index:08d}.bin"'
        )
        source = f"auris/{expected['bucket']}/{actual_record['key']}"
        lines.append(
            f"/opt/auris/minio-client.sh cp --quiet --version-id "
            f"{shlex.quote(target_version_id)} {shlex.quote(source)} "
            f"{verification_file}"
        )
        generation_label = f"{actual_record['key']}@{target_version_id}"
        lines.append(
            "verify_file "
            f"{expected_record['size_bytes']} "
            f"{expected_record['content_sha256']} {verification_file} "
            f"{shlex.quote(generation_label)}"
        )
        lines.append(f"rm -f -- {verification_file}")
    lines.append(
        f"printf 'verified {content_index} MinIO content generations by SHA-256\\n'"
    )
    Path(args.output).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


def du_size(_: argparse.Namespace) -> int:
    maximum = 0
    for line_number, line in enumerate(sys.stdin, 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PlanError(f"invalid mc du JSON at line {line_number}") from exc
        if isinstance(item, dict) and item.get("status") == "error":
            raise PlanError("mc du failed")
        if isinstance(item, dict):
            for key in ("size", "totalSize", "total_size"):
                value = item.get(key)
                if isinstance(value, int):
                    maximum = max(maximum, value)
    print(maximum)
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--listing", required=True)
    plan.add_argument("--bucket", required=True)
    plan.add_argument("--output", required=True)
    plan.set_defaults(handler=build_plan)
    bind = commands.add_parser("bind-artifacts")
    bind.add_argument("--plan", required=True)
    bind.add_argument("--backup-root", required=True)
    bind.add_argument("--output", required=True)
    bind.set_defaults(handler=bind_artifacts)
    verify = commands.add_parser("verify-artifacts")
    verify.add_argument("--plan", required=True)
    verify.add_argument("--backup-root", required=True)
    verify.set_defaults(handler=verify_artifacts)
    backup = commands.add_parser("emit-backup-shell")
    backup.add_argument("--plan", required=True)
    backup.add_argument("--output", required=True)
    backup.set_defaults(handler=emit_backup_shell)
    restore = commands.add_parser("emit-restore-shell")
    restore.add_argument("--plan", required=True)
    restore.add_argument("--output", required=True)
    restore.set_defaults(handler=emit_restore_shell)
    compare = commands.add_parser("compare-listing")
    compare.add_argument("--plan", required=True)
    compare.add_argument("--listing", required=True)
    compare.set_defaults(handler=compare_listing)
    verify_shell = commands.add_parser("emit-verify-shell")
    verify_shell.add_argument("--plan", required=True)
    verify_shell.add_argument("--listing", required=True)
    verify_shell.add_argument("--output", required=True)
    verify_shell.set_defaults(handler=emit_verify_shell)
    du = commands.add_parser("du-size")
    du.set_defaults(handler=du_size)
    return root


def main() -> int:
    try:
        args = parser().parse_args()
        return int(args.handler(args))
    except (OSError, PlanError) as exc:
        print(f"MinIO backup plan error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
