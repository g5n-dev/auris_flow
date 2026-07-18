#!/usr/bin/env python3
"""Back up and restore derived Qdrant collections through its snapshot API."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = "auris-flow.qdrant-snapshots/v1"
BASE_URL = os.environ.get("AURIS_BACKUP_QDRANT_URL", "http://qdrant:6333").rstrip("/")
API_KEY_FILE = Path(os.environ.get("AURIS_BACKUP_QDRANT_API_KEY_FILE", "/run/secrets/qdrant_api_key"))
NAME_RE = re.compile(r"^[^/\x00-\x1f\x7f]{1,255}$")


class SnapshotError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def api_key() -> str:
    try:
        value = API_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SnapshotError("cannot read the Qdrant API key file") from exc
    if not value or "\x00" in value:
        raise SnapshotError("Qdrant API key file is empty or invalid")
    return value


def request_json(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = canonical_json(body) if body is not None else None
    request = urllib.request.Request(BASE_URL + path, data=payload, method=method)
    request.add_header("api-key", api_key())
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            document = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"Qdrant {method} {path} failed") from exc
    if not isinstance(document, dict) or document.get("status") not in {"ok", None}:
        raise SnapshotError(f"Qdrant {method} {path} returned an invalid response")
    return document


def qdrant_version() -> str:
    document = request_json("GET", "/")
    result = document.get("version") or document.get("result")
    if isinstance(result, dict):
        result = result.get("version")
    if not isinstance(result, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+].*)?", result):
        raise SnapshotError("Qdrant did not report a semantic version")
    return result.removeprefix("v")


def collection_names() -> list[str]:
    document = request_json("GET", "/collections")
    result = document.get("result")
    collections = result.get("collections") if isinstance(result, dict) else None
    if not isinstance(collections, list):
        raise SnapshotError("Qdrant collection list is invalid")
    names: list[str] = []
    for item in collections:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise SnapshotError("Qdrant collection name is invalid")
        names.append(name)
    return sorted(names)


def collection_info(name: str) -> dict[str, Any]:
    document = request_json("GET", f"/collections/{urllib.parse.quote(name, safe='')}")
    result = document.get("result")
    if not isinstance(result, dict):
        raise SnapshotError("Qdrant collection metadata is invalid")
    points = result.get("points_count")
    if not isinstance(points, int) or points < 0:
        raise SnapshotError("Qdrant collection points_count is invalid")
    return {"points_count": points, "status": str(result.get("status") or "unknown")}


def alias_map() -> list[dict[str, str]]:
    document = request_json("GET", "/aliases")
    result = document.get("result")
    aliases = result.get("aliases") if isinstance(result, dict) else None
    if aliases is None:
        return []
    if not isinstance(aliases, list):
        raise SnapshotError("Qdrant alias metadata is invalid")
    normalized: list[dict[str, str]] = []
    for item in aliases:
        if not isinstance(item, dict):
            raise SnapshotError("Qdrant alias metadata is invalid")
        alias = item.get("alias_name")
        collection = item.get("collection_name")
        if not isinstance(alias, str) or not NAME_RE.fullmatch(alias):
            raise SnapshotError("Qdrant alias name is invalid")
        if not isinstance(collection, str) or not NAME_RE.fullmatch(collection):
            raise SnapshotError("Qdrant alias collection is invalid")
        normalized.append({"alias_name": alias, "collection_name": collection})
    return sorted(normalized, key=lambda item: item["alias_name"])


def _safe_snapshot_name(value: Any) -> str:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value) or not value.endswith(".snapshot"):
        raise SnapshotError("Qdrant returned an unsafe snapshot name")
    return value


def download(path: str, destination: Path) -> None:
    request = urllib.request.Request(BASE_URL + path, method="GET")
    request.add_header("api-key", api_key())
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(request, timeout=300) as response, temporary.open("wb") as output:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                output.write(chunk)
        os.replace(temporary, destination)
    except (OSError, urllib.error.URLError) as exc:
        raise SnapshotError("Qdrant snapshot download failed") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if output.is_symlink() or not output.is_dir():
        raise SnapshotError("Qdrant backup output must be an existing real directory")
    snapshots_dir = output / "snapshots"
    snapshots_dir.mkdir(mode=0o700)
    records: list[dict[str, Any]] = []
    for name in collection_names():
        quoted_name = urllib.parse.quote(name, safe="")
        info = collection_info(name)
        created = request_json("POST", f"/collections/{quoted_name}/snapshots?wait=true")
        result = created.get("result")
        if not isinstance(result, dict):
            raise SnapshotError("Qdrant snapshot creation response is invalid")
        snapshot_name = _safe_snapshot_name(result.get("name"))
        artifact_name = hashlib.sha256(name.encode("utf-8")).hexdigest() + ".snapshot"
        artifact = snapshots_dir / artifact_name
        download(
            f"/collections/{quoted_name}/snapshots/{urllib.parse.quote(snapshot_name, safe='')}",
            artifact,
        )
        record = {
            "name": name,
            "points_count": info["points_count"],
            "snapshot_name": snapshot_name,
            "artifact": f"qdrant/snapshots/{artifact_name}",
            "sha256": sha256_file(artifact),
            "size_bytes": artifact.stat().st_size,
        }
        records.append(record)
        if not args.keep_server_snapshots:
            request_json(
                "DELETE",
                f"/collections/{quoted_name}/snapshots/{urllib.parse.quote(snapshot_name, safe='')}",
            )
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "qdrant_version": qdrant_version(),
        "authority": "derived-rebuildable-from-mysql-and-minio",
        "collections": records,
        "aliases": alias_map(),
    }
    (output / "snapshots.json").write_bytes(canonical_json(metadata))
    return 0


def load_metadata(root: Path) -> dict[str, Any]:
    path = root / "snapshots.json"
    if path.is_symlink() or not path.is_file():
        raise SnapshotError("Qdrant snapshots.json is missing or unsafe")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError("Qdrant snapshots.json is invalid") from exc
    if not isinstance(metadata, dict) or metadata.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError("unsupported Qdrant snapshot metadata schema")
    if metadata.get("authority") != "derived-rebuildable-from-mysql-and-minio":
        raise SnapshotError("Qdrant must not be classified as an authoritative source")
    collections = metadata.get("collections")
    aliases = metadata.get("aliases")
    version = metadata.get("qdrant_version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+].*)?", version):
        raise SnapshotError("invalid Qdrant snapshot source version")
    if not isinstance(collections, list) or not isinstance(aliases, list):
        raise SnapshotError("invalid Qdrant snapshot metadata")
    seen: set[str] = set()
    for item in collections:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise SnapshotError("invalid Qdrant collection snapshot")
        name = item["name"]
        if not NAME_RE.fullmatch(name) or name in seen:
            raise SnapshotError("invalid or duplicate Qdrant collection")
        seen.add(name)
        if not isinstance(item.get("points_count"), int) or item["points_count"] < 0:
            raise SnapshotError("invalid Qdrant points_count")
        _safe_snapshot_name(item.get("snapshot_name"))
        relative = item.get("artifact")
        if not isinstance(relative, str):
            raise SnapshotError("Qdrant snapshot artifact is missing")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or pure.parts[:2] != ("qdrant", "snapshots") or ".." in pure.parts:
            raise SnapshotError("unsafe Qdrant snapshot artifact path")
        artifact = root.parent / pure
        if artifact.is_symlink() or not artifact.is_file():
            raise SnapshotError("Qdrant snapshot artifact is missing or unsafe")
        if sha256_file(artifact) != item.get("sha256") or artifact.stat().st_size != item.get(
            "size_bytes"
        ):
            raise SnapshotError("Qdrant snapshot artifact checksum mismatch")
    for item in aliases:
        if not isinstance(item, dict):
            raise SnapshotError("invalid Qdrant alias metadata")
        alias = item.get("alias_name")
        collection = item.get("collection_name")
        if not isinstance(alias, str) or not NAME_RE.fullmatch(alias):
            raise SnapshotError("invalid Qdrant alias name")
        if not isinstance(collection, str) or collection not in seen:
            raise SnapshotError("Qdrant alias references an unknown collection")
    return metadata


def assert_empty(_: argparse.Namespace) -> int:
    names = collection_names()
    if names:
        raise SnapshotError("target Qdrant is not empty; refusing derived-index restore")
    print(json.dumps({"status": "empty"}))
    return 0


def validate(args: argparse.Namespace) -> int:
    root = Path(args.input)
    if root.is_symlink() or not root.is_dir():
        raise SnapshotError("Qdrant backup input must be a real directory")
    metadata = load_metadata(root)
    print(
        json.dumps(
            {
                "status": "verified",
                "collections": len(metadata["collections"]),
                "qdrant_version": metadata["qdrant_version"],
            },
            sort_keys=True,
        )
    )
    return 0


def _upload_snapshot(collection: str, path: Path) -> None:
    parsed = urllib.parse.urlparse(BASE_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SnapshotError("invalid Qdrant base URL")
    boundary = "auris-flow-" + uuid.uuid4().hex
    filename = path.name.replace('"', "")
    preamble = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="snapshot"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("ascii")
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    quoted_collection = urllib.parse.quote(collection, safe="")
    request_path = f"/collections/{quoted_collection}/snapshots/upload?priority=snapshot&wait=true"
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=300)
    try:
        connection.putrequest("POST", request_path)
        connection.putheader("api-key", api_key())
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(len(preamble) + path.stat().st_size + len(suffix)))
        connection.endheaders()
        connection.send(preamble)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                connection.send(chunk)
        connection.send(suffix)
        response = connection.getresponse()
        response_body = response.read()
        if response.status < 200 or response.status >= 300:
            raise SnapshotError(f"Qdrant snapshot upload failed with HTTP {response.status}")
        document = json.loads(response_body.decode("utf-8"))
        if not isinstance(document, dict) or document.get("status") not in {"ok", None}:
            raise SnapshotError("Qdrant snapshot upload response is invalid")
    except (OSError, http.client.HTTPException, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError("Qdrant snapshot upload failed") from exc
    finally:
        connection.close()


def restore(args: argparse.Namespace) -> int:
    root = Path(args.input)
    if root.is_symlink() or not root.is_dir():
        raise SnapshotError("Qdrant backup input must be a real directory")
    metadata = load_metadata(root)
    if collection_names():
        raise SnapshotError("target Qdrant is not empty; refusing derived-index restore")
    source_minor = ".".join(str(metadata.get("qdrant_version", "")).split(".")[:2])
    target_minor = ".".join(qdrant_version().split(".")[:2])
    if source_minor != target_minor:
        raise SnapshotError("Qdrant snapshot and target must share the same major/minor version")
    for item in metadata["collections"]:
        artifact = root.parent / PurePosixPath(item["artifact"])
        _upload_snapshot(item["name"], artifact)
    if metadata["aliases"]:
        actions = [
            {
                "create_alias": {
                    "collection_name": item["collection_name"],
                    "alias_name": item["alias_name"],
                }
            }
            for item in metadata["aliases"]
        ]
        request_json("POST", "/collections/aliases", {"actions": actions})
    return verify_counts(argparse.Namespace(input=args.input))


def verify_counts(args: argparse.Namespace) -> int:
    root = Path(args.input)
    metadata = load_metadata(root)
    expected = {item["name"]: item["points_count"] for item in metadata["collections"]}
    actual_names = collection_names()
    if set(actual_names) != set(expected):
        raise SnapshotError("restored Qdrant collection set differs from backup")
    actual = {name: collection_info(name)["points_count"] for name in actual_names}
    if actual != expected:
        raise SnapshotError("restored Qdrant point counts differ from backup")
    print(json.dumps({"status": "verified", "collections": actual}, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    backup_command = commands.add_parser("backup")
    backup_command.add_argument("--output", required=True)
    backup_command.add_argument("--keep-server-snapshots", action="store_true")
    backup_command.set_defaults(handler=backup)
    empty_command = commands.add_parser("assert-empty")
    empty_command.set_defaults(handler=assert_empty)
    validate_command = commands.add_parser("validate")
    validate_command.add_argument("--input", required=True)
    validate_command.set_defaults(handler=validate)
    restore_command = commands.add_parser("restore")
    restore_command.add_argument("--input", required=True)
    restore_command.set_defaults(handler=restore)
    verify_command = commands.add_parser("verify-counts")
    verify_command.add_argument("--input", required=True)
    verify_command.set_defaults(handler=verify_counts)
    return root


def main() -> int:
    try:
        args = parser().parse_args()
        return int(args.handler(args))
    except (OSError, SnapshotError) as exc:
        print(f"Qdrant snapshot error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
