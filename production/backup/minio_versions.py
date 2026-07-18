#!/usr/bin/env python3
"""Plan and safely replay every generation of a versioned MinIO bucket."""

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


PLAN_SCHEMA = "auris-flow.minio-version-plan/v1"
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
    validate_plan(document)
    Path(args.output).write_text(canonical_json(document), encoding="utf-8")
    return 0


def load_plan(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PlanError("MinIO plan must be a regular file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError("invalid MinIO plan JSON") from exc
    validate_plan(document)
    return document


def validate_plan(document: Any) -> None:
    if not isinstance(document, dict) or document.get("schema_version") != PLAN_SCHEMA:
        raise PlanError("unsupported MinIO plan schema")
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
            if artifact is not None or size != 0:
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


def _shell_header(alias: str, bucket: str) -> list[str]:
    mc = "mc --config-dir /tmp/auris-flow-mc"
    return [
        "#!/bin/sh",
        "set -eu",
        'access_key="$(cat /run/secrets/object_storage_access_key)"',
        'secret_key="$(cat /run/secrets/object_storage_secret_key)"',
        f"{mc} alias set {shlex.quote(alias)} http://minio:9000 \"$access_key\" \"$secret_key\" >/dev/null",
        "unset access_key secret_key",
        f"{mc} version enable {shlex.quote(alias + '/' + bucket)} >/dev/null",
    ]


def emit_backup_shell(args: argparse.Namespace) -> int:
    document = load_plan(Path(args.plan))
    lines = _shell_header("source", document["bucket"])
    for record in document["versions"]:
        if record["delete_marker"]:
            continue
        artifact = "/backup/" + record["artifact"]
        source = f"source/{document['bucket']}/{record['key']}"
        lines.append(f"mkdir -p {shlex.quote(str(PurePosixPath(artifact).parent))}")
        version_flag = (
            f" --version-id {shlex.quote(record['version_id'])}" if record["version_id"] else ""
        )
        lines.append(
            f"mc --config-dir /tmp/auris-flow-mc cp --quiet{version_flag} "
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


def emit_restore_shell(args: argparse.Namespace) -> int:
    document = load_plan(Path(args.plan))
    lines = _shell_header("target", document["bucket"])
    for record in ordered_versions(document):
        target = f"target/{document['bucket']}/{record['key']}"
        if record["delete_marker"]:
            lines.append(
                "mc --config-dir /tmp/auris-flow-mc rm --quiet --force "
                f"{shlex.quote(target)}"
            )
        else:
            artifact = "/backup/" + record["artifact"]
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
                "mc --config-dir /tmp/auris-flow-mc cp --quiet --preserve"
                f"{rendered_options} "
                f"{shlex.quote(artifact)} {shlex.quote(target)}"
            )
    Path(args.output).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


def compare_listing(args: argparse.Namespace) -> int:
    expected = load_plan(Path(args.plan))
    with Path(args.listing).open(encoding="utf-8") as handle:
        actual_records = _records(handle, expected["bucket"])
    actual = dict(expected)
    actual["versions"] = actual_records
    actual["summary"] = {
        "object_keys": len({record["key"] for record in actual_records}),
        "versions": len(actual_records),
        "delete_markers": sum(record["delete_marker"] for record in actual_records),
        "content_bytes": sum(
            record["size_bytes"] for record in actual_records if not record["delete_marker"]
        ),
    }
    validate_plan(actual)

    def semantic_sequence(
        document: dict[str, Any],
    ) -> dict[str, list[tuple[bool, int, str, str, str, str]]]:
        result: dict[str, list[tuple[bool, int, str, str, str, str]]] = defaultdict(list)
        for record in ordered_versions(document):
            result[record["key"]].append(
                (
                    record["delete_marker"],
                    record["size_bytes"],
                    record["etag"],
                    json.dumps(record["metadata"], sort_keys=True),
                    json.dumps(record["tags"], sort_keys=True),
                    record["storage_class"],
                )
            )
        return dict(result)

    if semantic_sequence(expected) != semantic_sequence(actual):
        raise PlanError("restored MinIO version history differs from the backup plan")
    print(json.dumps({"status": "verified", **actual["summary"]}, sort_keys=True))
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
