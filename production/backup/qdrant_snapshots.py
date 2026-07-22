#!/usr/bin/env python3
"""Back up and restore derived Qdrant collections through its snapshot API."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn


SCHEMA_VERSION = "auris-flow.qdrant-snapshots/v2"
FINGERPRINT_ALGORITHM = "sha256-canonical-point-digests-v1"
SCROLL_PAGE_SIZE = 256
BASE_URL = os.environ.get("AURIS_BACKUP_QDRANT_URL", "http://qdrant:6333").rstrip("/")
API_KEY_FILE = Path(
    os.environ.get("AURIS_BACKUP_QDRANT_API_KEY_FILE", "/run/secrets/qdrant_api_key")
)
NAME_RE = re.compile(r"^[^/\x00-\x1f\x7f]{1,255}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SCOPE_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,255}$")
MAX_CONTROL_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_UPLOAD_RESPONSE_BYTES = 1024 * 1024
DEFAULT_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024 * 1024


class SnapshotError(RuntimeError):
    pass


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never replay the Qdrant API key to a redirect target."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> NoReturn:
        del newurl
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


_NO_REDIRECT_OPENER = urllib.request.build_opener(_RejectRedirectHandler())


def _open_request(request: urllib.request.Request, *, timeout: float) -> Any:
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def _base_url() -> str:
    try:
        parsed = urllib.parse.urlsplit(BASE_URL)
        port = parsed.port
    except ValueError as exc:
        raise SnapshotError("invalid Qdrant base URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SnapshotError("invalid Qdrant base URL")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    authority = host if port is None else f"{host}:{port}"
    return f"{parsed.scheme}://{authority}"


def _request_url(path: str) -> str:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in path)
    ):
        raise SnapshotError("invalid Qdrant request path")
    return _base_url() + path


def _response_status(response: Any) -> int:
    raw = getattr(response, "status", None)
    if raw is None and hasattr(response, "getcode"):
        raw = response.getcode()
    if isinstance(raw, bool) or not isinstance(raw, int) or not 200 <= raw < 300:
        raise SnapshotError("Qdrant returned a non-success HTTP response")
    return raw


def _declared_content_length(response: Any, *, maximum: int) -> int | None:
    headers = getattr(response, "headers", None)
    raw = headers.get("Content-Length") if headers is not None else None
    if raw is None:
        return None
    try:
        value = int(str(raw))
    except ValueError as exc:
        raise SnapshotError("Qdrant returned an invalid Content-Length") from exc
    if value < 0 or value > maximum:
        raise SnapshotError("Qdrant response exceeds the configured byte budget")
    return value


def _read_bounded(response: Any, *, maximum: int) -> bytes:
    declared = _declared_content_length(response, maximum=maximum)
    payload = response.read(maximum + 1)
    if len(payload) > maximum:
        raise SnapshotError("Qdrant response exceeds the configured byte budget")
    if declared is not None and len(payload) != declared:
        raise SnapshotError("Qdrant response length differs from Content-Length")
    return payload


def _snapshot_byte_budget() -> int:
    raw = os.environ.get(
        "AURIS_BACKUP_QDRANT_MAX_SNAPSHOT_BYTES",
        str(DEFAULT_MAX_SNAPSHOT_BYTES),
    )
    try:
        value = int(raw)
    except ValueError as exc:
        raise SnapshotError("invalid Qdrant snapshot byte budget") from exc
    if value <= 0 or value > 1024**5:
        raise SnapshotError("invalid Qdrant snapshot byte budget")
    return value


def canonical_json(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SnapshotError("value cannot be encoded as canonical JSON") from exc
    return (rendered + "\n").encode("utf-8")


def _canonical_digest(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SnapshotError("Qdrant semantic value is not canonical JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def _normalize_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SnapshotError("Qdrant payload contains a non-finite number")
        return 0.0 if value == 0.0 else value
    if isinstance(value, list):
        return [_normalize_json(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise SnapshotError("Qdrant payload contains a non-string key")
        return {key: _normalize_json(item) for key, item in value.items()}
    raise SnapshotError("Qdrant payload contains a non-JSON value")


def _normalize_point_id(value: Any) -> str | int:
    if isinstance(value, bool):
        raise SnapshotError("Qdrant point id is invalid")
    if isinstance(value, int):
        if value < 0 or value > 2**64 - 1:
            raise SnapshotError("Qdrant point id is invalid")
        return value
    if isinstance(value, str) and NAME_RE.fullmatch(value):
        return value
    raise SnapshotError("Qdrant point id is invalid")


def _point_id_identity(value: Any) -> dict[str, str | int]:
    point_id = _normalize_point_id(value)
    if isinstance(point_id, int):
        return {"type": "integer", "value": point_id}
    return {"type": "string", "value": point_id}


def _point_id_key(value: Any) -> str:
    identity = _point_id_identity(value)
    return json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _normalize_vector(value: Any) -> list[float]:
    if not isinstance(value, list) or not value:
        raise SnapshotError("Qdrant point must use a non-empty unnamed dense vector")
    normalized: list[float] = []
    for coordinate in value:
        if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
            raise SnapshotError("Qdrant point must use an unnamed dense vector")
        try:
            number = float(coordinate)
        except (OverflowError, ValueError) as exc:
            raise SnapshotError(
                "Qdrant point has an invalid unnamed dense vector"
            ) from exc
        if not math.isfinite(number):
            raise SnapshotError("Qdrant point has a non-finite unnamed dense vector")
        normalized.append(0.0 if number == 0.0 else number)
    return normalized


def _scope_value(value: Any) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or value == "*"
        or not SCOPE_RE.fullmatch(value)
    ):
        raise SnapshotError("Qdrant point is missing a valid tenant/project scope")
    return value


def _point_descriptor(point: Any) -> dict[str, Any]:
    if not isinstance(point, dict):
        raise SnapshotError("Qdrant scroll returned an invalid point")
    point_id = _normalize_point_id(point.get("id"))
    payload = point.get("payload")
    if not isinstance(payload, dict):
        raise SnapshotError("Qdrant point is missing a valid tenant/project scope")
    normalized_payload = _normalize_json(payload)
    tenant_id = _scope_value(normalized_payload.get("tenant_id"))
    project_id = _scope_value(normalized_payload.get("project_id"))
    normalized_vector = _normalize_vector(point.get("vector"))
    payload_sha256 = _canonical_digest(normalized_payload)
    vector_sha256 = _canonical_digest(normalized_vector)
    identity = _point_id_identity(point_id)
    return {
        "point_id": point_id,
        "point_id_identity": identity,
        "point_id_key": _point_id_key(point_id),
        "payload_sha256": payload_sha256,
        "vector_sha256": vector_sha256,
        "point_sha256": _canonical_digest(
            {
                "point_id": identity,
                "payload_sha256": payload_sha256,
                "vector_sha256": vector_sha256,
            }
        ),
        "scope": {"tenant_id": tenant_id, "project_id": project_id},
    }


def _scroll_points(name: str) -> Iterator[dict[str, Any]]:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise SnapshotError("Qdrant collection name is invalid")
    quoted_name = urllib.parse.quote(name, safe="")
    seen_point_ids: set[str] = set()
    seen_offsets: set[str] = set()
    offset: str | int | None = None
    while True:
        body: dict[str, Any] = {
            "limit": SCROLL_PAGE_SIZE,
            "with_payload": True,
            "with_vector": True,
        }
        if offset is not None:
            body["offset"] = offset
        document = request_json(
            "POST",
            f"/collections/{quoted_name}/points/scroll",
            body,
        )
        result = document.get("result")
        if not isinstance(result, dict):
            raise SnapshotError("Qdrant scroll response is invalid")
        page = result.get("points")
        if not isinstance(page, list):
            raise SnapshotError("Qdrant scroll response is invalid")
        for point in page:
            if not isinstance(point, dict):
                raise SnapshotError("Qdrant scroll returned an invalid point")
            identity = _point_id_key(point.get("id"))
            if identity in seen_point_ids:
                raise SnapshotError("Qdrant scroll returned a duplicate point id")
            seen_point_ids.add(identity)
            yield point
        next_offset = result.get("next_page_offset")
        if next_offset is None:
            break
        normalized_offset = _normalize_point_id(next_offset)
        offset_key = _point_id_key(normalized_offset)
        if offset_key in seen_offsets:
            raise SnapshotError("Qdrant scroll returned a repeated page offset")
        seen_offsets.add(offset_key)
        offset = normalized_offset


def collection_semantics(name: str, expected_count: int) -> dict[str, Any]:
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 0
    ):
        raise SnapshotError("Qdrant expected point count is invalid")
    point_digests: list[str] = []
    probe: dict[str, Any] | None = None
    point_count = 0
    for point in _scroll_points(name):
        descriptor = _point_descriptor(point)
        point_count += 1
        point_digests.append(descriptor["point_sha256"])
        if probe is None or descriptor["point_id_key"] < probe["point_id_key"]:
            probe = descriptor
    if point_count != expected_count:
        raise SnapshotError(
            "Qdrant full semantic inventory point count differs from metadata"
        )
    point_digests.sort()
    fingerprint = _canonical_digest(
        {
            "algorithm": FINGERPRINT_ALGORITHM,
            "point_digests": point_digests,
        }
    )
    if probe is None:
        return {
            "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
            "points_fingerprint_sha256": fingerprint,
            "scope_policy": "empty-collection",
            "probe": None,
        }
    return {
        "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        "points_fingerprint_sha256": fingerprint,
        "scope_policy": "tenant-project-required",
        "probe": {
            "point_id": probe["point_id"],
            "payload_sha256": probe["payload_sha256"],
            "vector_sha256": probe["vector_sha256"],
            "vector_kind": "unnamed-dense",
            "scope": probe["scope"],
        },
    }


def api_key() -> str:
    try:
        metadata = API_KEY_FILE.lstat()
        if (
            API_KEY_FILE.is_symlink()
            or not API_KEY_FILE.is_file()
            or metadata.st_size > 65_536
        ):
            raise SnapshotError("Qdrant API key file is unsafe")
        value = API_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SnapshotError("cannot read the Qdrant API key file") from exc
    if not value or "\x00" in value:
        raise SnapshotError("Qdrant API key file is empty or invalid")
    return value


def request_json(
    method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload = canonical_json(body) if body is not None else None
    request = urllib.request.Request(_request_url(path), data=payload, method=method)
    request.add_header("api-key", api_key())
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with _open_request(request, timeout=120) as response:
            _response_status(response)
            document = json.loads(
                _read_bounded(response, maximum=MAX_CONTROL_RESPONSE_BYTES).decode(
                    "utf-8"
                )
            )
    except (
        urllib.error.URLError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        SnapshotError,
    ) as exc:
        raise SnapshotError(f"Qdrant {method} {path} failed") from exc
    if not isinstance(document, dict) or document.get("status") not in {"ok", None}:
        raise SnapshotError(f"Qdrant {method} {path} returned an invalid response")
    return document


def qdrant_version() -> str:
    document = request_json("GET", "/")
    result = document.get("version") or document.get("result")
    if isinstance(result, dict):
        result = result.get("version")
    if not isinstance(result, str) or not re.fullmatch(
        r"v?\d+\.\d+\.\d+(?:[-+].*)?", result
    ):
        raise SnapshotError("Qdrant did not report a semantic version")
    return result.removeprefix("v")


def collection_names() -> list[str]:
    document = request_json("GET", "/collections")
    result = document.get("result")
    collections = result.get("collections") if isinstance(result, dict) else None
    if not isinstance(collections, list):
        raise SnapshotError("Qdrant collection list is invalid")
    names: list[str] = []
    seen: set[str] = set()
    for item in collections:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in seen:
            raise SnapshotError("Qdrant collection name is invalid")
        seen.add(name)
        names.append(name)
    return sorted(names)


def collection_info(name: str) -> dict[str, Any]:
    document = request_json("GET", f"/collections/{urllib.parse.quote(name, safe='')}")
    result = document.get("result")
    if not isinstance(result, dict):
        raise SnapshotError("Qdrant collection metadata is invalid")
    points = result.get("points_count")
    if isinstance(points, bool) or not isinstance(points, int) or points < 0:
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
    seen: set[str] = set()
    for item in aliases:
        if not isinstance(item, dict):
            raise SnapshotError("Qdrant alias metadata is invalid")
        alias = item.get("alias_name")
        collection = item.get("collection_name")
        if not isinstance(alias, str) or not NAME_RE.fullmatch(alias) or alias in seen:
            raise SnapshotError("Qdrant alias name is invalid")
        seen.add(alias)
        if not isinstance(collection, str) or not NAME_RE.fullmatch(collection):
            raise SnapshotError("Qdrant alias collection is invalid")
        normalized.append({"alias_name": alias, "collection_name": collection})
    return sorted(normalized, key=lambda item: item["alias_name"])


def _safe_snapshot_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not NAME_RE.fullmatch(value)
        or not value.endswith(".snapshot")
    ):
        raise SnapshotError("Qdrant returned an unsafe snapshot name")
    return value


def download(path: str, destination: Path) -> None:
    request = urllib.request.Request(_request_url(path), method="GET")
    request.add_header("api-key", api_key())
    temporary = destination.with_suffix(destination.suffix + ".part")
    budget = _snapshot_byte_budget()
    try:
        with (
            _open_request(request, timeout=300) as response,
            temporary.open("xb") as output,
        ):
            _response_status(response)
            declared = _declared_content_length(response, maximum=budget)
            written = 0
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                written += len(chunk)
                if written > budget:
                    raise SnapshotError(
                        "Qdrant snapshot exceeds the configured byte budget"
                    )
                output.write(chunk)
            if declared is not None and written != declared:
                raise SnapshotError(
                    "Qdrant snapshot length differs from Content-Length"
                )
        os.replace(temporary, destination)
    except (OSError, urllib.error.URLError, SnapshotError) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
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
        semantics = _validate_semantics_metadata(
            collection_semantics(name, expected_count=info["points_count"]),
            info["points_count"],
        )
        created = request_json(
            "POST", f"/collections/{quoted_name}/snapshots?wait=true"
        )
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
            "semantics": semantics,
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


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise SnapshotError(f"invalid Qdrant {label} SHA-256")
    return value


def _validate_semantics_metadata(value: Any, points_count: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "fingerprint_algorithm",
        "points_fingerprint_sha256",
        "scope_policy",
        "probe",
    }:
        raise SnapshotError("invalid Qdrant collection semantic metadata")
    if value.get("fingerprint_algorithm") != FINGERPRINT_ALGORITHM:
        raise SnapshotError("unsupported Qdrant semantic fingerprint algorithm")
    _require_sha256(value.get("points_fingerprint_sha256"), "collection fingerprint")
    probe = value.get("probe")
    if points_count == 0:
        if value.get("scope_policy") != "empty-collection" or probe is not None:
            raise SnapshotError(
                "empty Qdrant collection must declare empty-collection scope policy"
            )
        return value
    if value.get("scope_policy") != "tenant-project-required":
        raise SnapshotError(
            "non-empty Qdrant collection must require tenant/project scope"
        )
    if not isinstance(probe, dict) or set(probe) != {
        "point_id",
        "payload_sha256",
        "vector_sha256",
        "vector_kind",
        "scope",
    }:
        raise SnapshotError("invalid Qdrant semantic probe metadata")
    _normalize_point_id(probe.get("point_id"))
    _require_sha256(probe.get("payload_sha256"), "probe payload")
    _require_sha256(probe.get("vector_sha256"), "probe vector")
    if probe.get("vector_kind") != "unnamed-dense":
        raise SnapshotError("Qdrant semantic probe must use an unnamed dense vector")
    scope = probe.get("scope")
    if not isinstance(scope, dict) or set(scope) != {"tenant_id", "project_id"}:
        raise SnapshotError("invalid Qdrant semantic probe tenant/project scope")
    _scope_value(scope.get("tenant_id"))
    _scope_value(scope.get("project_id"))
    return value


def load_metadata(root: Path) -> dict[str, Any]:
    path = root / "snapshots.json"
    if path.is_symlink() or not path.is_file():
        raise SnapshotError("Qdrant snapshots.json is missing or unsafe")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError("Qdrant snapshots.json is invalid") from exc
    if (
        not isinstance(metadata, dict)
        or metadata.get("schema_version") != SCHEMA_VERSION
    ):
        raise SnapshotError("unsupported Qdrant snapshot metadata schema")
    if set(metadata) != {
        "schema_version",
        "qdrant_version",
        "authority",
        "collections",
        "aliases",
    }:
        raise SnapshotError("invalid Qdrant snapshot metadata fields")
    if metadata.get("authority") != "derived-rebuildable-from-mysql-and-minio":
        raise SnapshotError("Qdrant must not be classified as an authoritative source")
    collections = metadata.get("collections")
    aliases = metadata.get("aliases")
    version = metadata.get("qdrant_version")
    if not isinstance(version, str) or not re.fullmatch(
        r"\d+\.\d+\.\d+(?:[-+].*)?", version
    ):
        raise SnapshotError("invalid Qdrant snapshot source version")
    if not isinstance(collections, list) or not isinstance(aliases, list):
        raise SnapshotError("invalid Qdrant snapshot metadata")
    seen: set[str] = set()
    for item in collections:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "points_count",
            "snapshot_name",
            "artifact",
            "sha256",
            "size_bytes",
            "semantics",
        }:
            raise SnapshotError("invalid Qdrant collection snapshot")
        if not isinstance(item.get("name"), str):
            raise SnapshotError("invalid Qdrant collection snapshot")
        name = item["name"]
        if not NAME_RE.fullmatch(name) or name in seen:
            raise SnapshotError("invalid or duplicate Qdrant collection")
        seen.add(name)
        if (
            isinstance(item.get("points_count"), bool)
            or not isinstance(item.get("points_count"), int)
            or item["points_count"] < 0
        ):
            raise SnapshotError("invalid Qdrant points_count")
        _validate_semantics_metadata(item.get("semantics"), item["points_count"])
        _safe_snapshot_name(item.get("snapshot_name"))
        relative = item.get("artifact")
        if not isinstance(relative, str):
            raise SnapshotError("Qdrant snapshot artifact is missing")
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or pure.parts[:2] != ("qdrant", "snapshots")
            or ".." in pure.parts
        ):
            raise SnapshotError("unsafe Qdrant snapshot artifact path")
        artifact = root.parent / pure
        if artifact.is_symlink() or not artifact.is_file():
            raise SnapshotError("Qdrant snapshot artifact is missing or unsafe")
        expected_sha256 = _require_sha256(item.get("sha256"), "snapshot artifact")
        size_bytes = item.get("size_bytes")
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
        ):
            raise SnapshotError("invalid Qdrant snapshot artifact size")
        if (
            sha256_file(artifact) != expected_sha256
            or artifact.stat().st_size != size_bytes
        ):
            raise SnapshotError("Qdrant snapshot artifact checksum mismatch")
    seen_aliases: set[str] = set()
    for item in aliases:
        if not isinstance(item, dict) or set(item) != {"alias_name", "collection_name"}:
            raise SnapshotError("invalid Qdrant alias metadata")
        alias = item.get("alias_name")
        collection = item.get("collection_name")
        if (
            not isinstance(alias, str)
            or not NAME_RE.fullmatch(alias)
            or alias in seen_aliases
        ):
            raise SnapshotError("invalid Qdrant alias name")
        seen_aliases.add(alias)
        if not isinstance(collection, str) or collection not in seen:
            raise SnapshotError("Qdrant alias references an unknown collection")
    return metadata


def assert_empty(_: argparse.Namespace) -> int:
    names = collection_names()
    if names:
        raise SnapshotError(
            "target Qdrant is not empty; refusing derived-index restore"
        )
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
    parsed = urllib.parse.urlsplit(_base_url())
    boundary = "auris-flow-" + uuid.uuid4().hex
    filename = path.name.replace('"', "")
    preamble = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="snapshot"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("ascii")
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    quoted_collection = urllib.parse.quote(collection, safe="")
    request_path = (
        f"/collections/{quoted_collection}/snapshots/upload?priority=snapshot&wait=true"
    )
    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(parsed.hostname, parsed.port, timeout=300)
    try:
        connection.putrequest("POST", request_path)
        connection.putheader("api-key", api_key())
        connection.putheader(
            "Content-Type", f"multipart/form-data; boundary={boundary}"
        )
        connection.putheader(
            "Content-Length", str(len(preamble) + path.stat().st_size + len(suffix))
        )
        connection.endheaders()
        connection.send(preamble)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                connection.send(chunk)
        connection.send(suffix)
        response = connection.getresponse()
        response_body = _read_bounded(response, maximum=MAX_UPLOAD_RESPONSE_BYTES)
        if response.status < 200 or response.status >= 300:
            raise SnapshotError(
                f"Qdrant snapshot upload failed with HTTP {response.status}"
            )
        document = json.loads(response_body.decode("utf-8"))
        if not isinstance(document, dict) or document.get("status") not in {"ok", None}:
            raise SnapshotError("Qdrant snapshot upload response is invalid")
    except (
        OSError,
        http.client.HTTPException,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise SnapshotError("Qdrant snapshot upload failed") from exc
    finally:
        connection.close()


def verify_collection_semantics(record: dict[str, Any]) -> None:
    name = record.get("name")
    points_count = record.get("points_count")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise SnapshotError("invalid Qdrant collection name in semantic verification")
    if (
        isinstance(points_count, bool)
        or not isinstance(points_count, int)
        or points_count < 0
    ):
        raise SnapshotError("invalid Qdrant point count in semantic verification")
    expected = _validate_semantics_metadata(record.get("semantics"), points_count)
    actual = collection_semantics(name, expected_count=points_count)
    if actual["points_fingerprint_sha256"] != expected["points_fingerprint_sha256"]:
        raise SnapshotError(
            "restored Qdrant collection semantic fingerprint differs from backup"
        )
    if actual != expected:
        raise SnapshotError(
            "restored Qdrant collection semantic probe differs from backup"
        )
    probe = expected["probe"]
    if probe is None:
        return

    quoted_name = urllib.parse.quote(name, safe="")
    probe_id = _normalize_point_id(probe["point_id"])
    document = request_json(
        "POST",
        f"/collections/{quoted_name}/points",
        {"ids": [probe_id], "with_payload": True, "with_vector": True},
    )
    retrieved = document.get("result")
    if not isinstance(retrieved, list) or len(retrieved) != 1:
        raise SnapshotError(
            "Qdrant semantic probe exact retrieval did not return one point"
        )
    point = retrieved[0]
    descriptor = _point_descriptor(point)
    if descriptor["point_id_identity"] != _point_id_identity(probe_id):
        raise SnapshotError(
            "Qdrant semantic probe exact retrieval returned the wrong point"
        )
    if descriptor["payload_sha256"] != probe["payload_sha256"]:
        raise SnapshotError("restored Qdrant probe payload hash differs from backup")
    if descriptor["vector_sha256"] != probe["vector_sha256"]:
        raise SnapshotError("restored Qdrant probe vector hash differs from backup")
    if descriptor["scope"] != probe["scope"]:
        raise SnapshotError(
            "restored Qdrant probe tenant/project scope differs from backup"
        )
    assert isinstance(point, dict)
    probe_vector = _normalize_vector(point.get("vector"))
    scope = probe["scope"]
    search = request_json(
        "POST",
        f"/collections/{quoted_name}/points/search",
        {
            "vector": probe_vector,
            "limit": 1,
            "with_payload": True,
            "with_vector": False,
            "filter": {
                "must": [
                    {"key": "tenant_id", "match": {"value": scope["tenant_id"]}},
                    {"key": "project_id", "match": {"value": scope["project_id"]}},
                    {"has_id": [probe_id]},
                ]
            },
        },
    )
    results = search.get("result")
    if not isinstance(results, list) or len(results) != 1:
        raise SnapshotError(
            "Qdrant filtered nearest-query did not return the probe exactly once"
        )
    match = results[0]
    if not isinstance(match, dict):
        raise SnapshotError("Qdrant filtered nearest-query returned an invalid point")
    matched_payload = match.get("payload")
    if not isinstance(matched_payload, dict):
        raise SnapshotError("Qdrant filtered nearest-query omitted probe payload")
    normalized_payload = _normalize_json(matched_payload)
    matched_scope = {
        "tenant_id": _scope_value(normalized_payload.get("tenant_id")),
        "project_id": _scope_value(normalized_payload.get("project_id")),
    }
    if matched_scope != scope:
        raise SnapshotError(
            "Qdrant filtered nearest-query returned a cross-scope payload"
        )
    if _point_id_identity(match.get("id")) != _point_id_identity(probe_id):
        raise SnapshotError(
            "Qdrant filtered nearest-query did not return the probe itself"
        )
    if _canonical_digest(normalized_payload) != probe["payload_sha256"]:
        raise SnapshotError(
            "Qdrant filtered nearest-query probe payload differs from exact retrieval"
        )


def restore(args: argparse.Namespace) -> int:
    root = Path(args.input)
    if root.is_symlink() or not root.is_dir():
        raise SnapshotError("Qdrant backup input must be a real directory")
    metadata = load_metadata(root)
    if collection_names():
        raise SnapshotError(
            "target Qdrant is not empty; refusing derived-index restore"
        )
    source_minor = ".".join(str(metadata.get("qdrant_version", "")).split(".")[:2])
    target_minor = ".".join(qdrant_version().split(".")[:2])
    if source_minor != target_minor:
        raise SnapshotError(
            "Qdrant snapshot and target must share the same major/minor version"
        )
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
    return verify_semantics(argparse.Namespace(input=args.input))


def verify_semantics(args: argparse.Namespace) -> int:
    root = Path(args.input)
    metadata = load_metadata(root)
    expected = {item["name"]: item for item in metadata["collections"]}
    actual_names = collection_names()
    if set(actual_names) != set(expected):
        raise SnapshotError("restored Qdrant collection set differs from backup")
    for name in actual_names:
        verify_collection_semantics(expected[name])
    expected_aliases = sorted(metadata["aliases"], key=lambda item: item["alias_name"])
    if alias_map() != expected_aliases:
        raise SnapshotError("restored Qdrant aliases differ from backup")
    verified_counts = {name: expected[name]["points_count"] for name in actual_names}
    print(
        json.dumps(
            {
                "status": "verified",
                "collections": verified_counts,
                "verification": "full-fingerprint-and-scoped-probe",
            },
            sort_keys=True,
        )
    )
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
    verify_command = commands.add_parser("verify-semantics")
    verify_command.add_argument("--input", required=True)
    verify_command.set_defaults(handler=verify_semantics)
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
