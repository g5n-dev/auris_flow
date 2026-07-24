#!/usr/bin/env python3
"""Build a deterministic, non-sensitive cross-store recovery proof.

The release recovery drill uses one synthetic fixture whose MySQL authority
record binds an immutable MinIO object and one Qdrant point.  This module keeps
the proof calculation pure: callers must independently read the three stores
and pass the observed values to :func:`build_proof`.

Only fixed identifiers and SHA-256 digests leave this module in the public
proof.  Object keys, object bytes, database contents, endpoints, credentials,
and host paths are deliberately excluded.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Sequence


SCHEMA_VERSION = "auris-flow.recovery-linkage-proof/v1"
OBJECT_SCHEMA_VERSION = "auris-flow.recovery-linkage-object/v1"
FIXTURE_ID = "release-recovery-linkage-v1"
TENANT_ID = "auris_release"
PROJECT_ID = "release_recovery_gate"
TRACE_ID = "trace_release_recovery_gate_0001"
AUTHORITY_COLLECTION = "release_recovery_fixtures"
OBJECT_BUCKET = "auris-flow"
OBJECT_KEY = "release-gate/recovery-linkage-v1.json"
QDRANT_COLLECTION = "auris_restore_gate"
QDRANT_POINT_ID = "4dd5b6c0-5342-5f1d-8ef9-752a5d695cc8"
MAX_OBJECT_BYTES = 64 * 1024
MAX_JSON_INPUT_BYTES = 256 * 1024
MAX_QDRANT_RESPONSE_BYTES = 1024 * 1024
QDRANT_BASE_URL = "http://qdrant:6333"
QDRANT_API_KEY_FILE = Path("/run/secrets/qdrant_api_key")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
BACKUP_ID_RE = re.compile(r"^auris-flow-\d{8}T\d{6}Z-[0-9a-f]{12}$")
RELEASE_TAG_RE = re.compile(
    r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z](?:[0-9A-Za-z.-]*[0-9A-Za-z])?)?$"
)
DRILL_PROJECT_RE = re.compile(r"^auris-flow-restore-drill-[0-9a-f]{12}$")
RECEIPT_SCHEMA_VERSION = "auris-flow.recovery-linkage-receipt/v1"

PROOF_FIELDS = {
    "schema_version",
    "fixture_id",
    "authority_record_sha256",
    "object_identity_sha256",
    "object_content_sha256",
    "qdrant_point_identity_sha256",
    "qdrant_payload_sha256",
    "qdrant_vector_sha256",
    "linkage_sha256",
}
RECEIPT_FIELDS = {
    "schema_version",
    "phase",
    "fixture_id",
    "challenge_sha256",
    "backup_id",
    "backup_manifest_sha256",
    "source_commit",
    "release_tag",
    "release_metadata_sha256",
    "drill_project",
    "source_proof_sha256",
    "observed_proof_sha256",
    "receipt_sha256",
}


class LinkageError(RuntimeError):
    """Raised when a cross-store fixture or proof fails closed."""


class QdrantNotFound(LinkageError):
    """Raised only for an exact Qdrant 404 response."""


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not replay the Qdrant API key to any redirect destination."""

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


def _canonical_bytes(value: Any, *, label: str) -> bytes:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise LinkageError(f"{label} is not canonical JSON") from exc
    return encoded


def _canonical_document(value: Any, *, label: str) -> bytes:
    return _canonical_bytes(value, label=label) + b"\n"


def _digest(value: Any, *, label: str) -> str:
    return hashlib.sha256(_canonical_bytes(value, label=label)).hexdigest()


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise LinkageError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise LinkageError(f"{label} fields are invalid")
    return value


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LinkageError("JSON input contains a duplicate field")
        result[key] = value
    return result


def _read_regular_input(
    path_value: str | os.PathLike[str],
    *,
    maximum: int,
    label: str,
) -> bytes:
    path = os.fspath(path_value)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LinkageError(f"{label} must be a readable regular file") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise LinkageError(f"{label} must be a readable regular file")
        if before.st_size < 0 or before.st_size > maximum:
            raise LinkageError(f"{label} size is invalid")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum or len(payload) != before.st_size:
            raise LinkageError(f"{label} size is invalid")
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            raise LinkageError(f"{label} changed while it was read")
        return payload
    finally:
        os.close(descriptor)


def read_json_input(path_value: str | os.PathLike[str]) -> Any:
    """Read one bounded, no-follow JSON file and reject duplicate fields."""

    payload = _read_regular_input(
        path_value,
        maximum=MAX_JSON_INPUT_BYTES,
        label="JSON input",
    )
    return _decode_json(payload, label="JSON input")


def _decode_json(payload: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            payload,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                LinkageError(f"{label} contains a non-finite number")
            ),
        )
    except LinkageError:
        raise
    except (UnicodeDecodeError, RecursionError, ValueError) as exc:
        raise LinkageError(f"{label} is invalid") from exc


def _read_bounded_stdin(*, maximum: int, label: str) -> bytes:
    payload = sys.stdin.buffer.read(maximum + 1)
    if len(payload) > maximum:
        raise LinkageError(f"{label} exceeds the byte budget")
    if not payload:
        raise LinkageError(f"{label} is empty")
    return payload


def _capture_json_stdin(output: str | os.PathLike[str]) -> None:
    payload = _read_bounded_stdin(
        maximum=MAX_JSON_INPUT_BYTES,
        label="live JSON capture",
    )
    document = _decode_json(payload, label="live JSON capture")
    _mapping(document, label="live JSON capture")
    _write_json_exclusive(output, document)


def _capture_object_stdin(output: str | os.PathLike[str]) -> None:
    payload = _read_bounded_stdin(
        maximum=MAX_OBJECT_BYTES,
        label="live object capture",
    )
    _validated_object(payload)
    _write_bytes_exclusive(output, payload)


def _publish_proof_stdin(output: str | os.PathLike[str]) -> None:
    payload = _read_bounded_stdin(
        maximum=MAX_JSON_INPUT_BYTES,
        label="proof publication input",
    )
    proof = validate_proof(_decode_json(payload, label="proof publication input"))
    _write_json_exclusive(output, proof)


def _validate_object_stat_stdin() -> None:
    payload = _read_bounded_stdin(
        maximum=MAX_JSON_INPUT_BYTES,
        label="MinIO stat response",
    )
    document = _mapping(
        _decode_json(payload, label="MinIO stat response"),
        label="MinIO stat response",
    )
    size = document.get("size")
    expected_size = len(_canonical_document(_object_document(), label="fixture object"))
    if (
        document.get("status") != "success"
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size != expected_size
    ):
        raise LinkageError("MinIO fixture stat does not match the object contract")


def _write_bytes_exclusive(
    path_value: str | os.PathLike[str],
    payload: bytes,
) -> None:
    path = Path(path_value)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise LinkageError("output path must be an absolute regular-file path")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(path.parent, directory_flags)
    except OSError as exc:
        raise LinkageError("output parent must be a real directory") from exc
    temporary_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    temporary_created = False
    try:
        output_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(
            temporary_name,
            output_flags,
            0o600,
            dir_fd=directory_fd,
        )
        temporary_created = True
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise LinkageError("output write did not make progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise LinkageError("output already exists") from exc
        os.unlink(temporary_name, dir_fd=directory_fd)
        temporary_created = False
        os.fsync(directory_fd)
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def _write_json_exclusive(
    path_value: str | os.PathLike[str],
    value: Any,
) -> None:
    _write_bytes_exclusive(
        path_value,
        _canonical_document(value, label="output"),
    )


def _qdrant_api_key() -> str:
    payload = _read_regular_input(
        QDRANT_API_KEY_FILE,
        maximum=64 * 1024,
        label="Qdrant API key",
    )
    try:
        value = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise LinkageError("Qdrant API key is invalid") from exc
    if (
        not value
        or len(value) > 4096
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise LinkageError("Qdrant API key is invalid")
    return value


def _qdrant_request(
    method: str,
    path: str,
    body: object | None = None,
) -> dict[str, Any]:
    if (
        method not in {"GET", "POST", "PUT"}
        or not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in path)
    ):
        raise LinkageError("Qdrant request target is invalid")
    payload: bytes | None = None
    headers = {
        "Accept": "application/json",
        "api-key": _qdrant_api_key(),
    }
    if body is not None:
        payload = _canonical_bytes(body, label="Qdrant request")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        QDRANT_BASE_URL + path,
        data=payload,
        headers=headers,
        method=method,
    )
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=15) as response:
            status = response.status
            declared_raw = response.headers.get("Content-Length")
            if declared_raw is not None:
                try:
                    declared = int(declared_raw)
                except ValueError as exc:
                    raise LinkageError("Qdrant response length is invalid") from exc
                if declared < 0 or declared > MAX_QDRANT_RESPONSE_BYTES:
                    raise LinkageError("Qdrant response size is invalid")
            raw = response.read(MAX_QDRANT_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise QdrantNotFound("Qdrant fixture target was not found") from exc
        raise LinkageError("Qdrant returned a non-success response") from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise LinkageError("Qdrant request failed") from exc
    if not isinstance(status, int) or not 200 <= status < 300:
        raise LinkageError("Qdrant returned a non-success response")
    if len(raw) > MAX_QDRANT_RESPONSE_BYTES:
        raise LinkageError("Qdrant response size is invalid")
    document = _decode_json(raw, label="Qdrant response")
    result = _mapping(document, label="Qdrant response")
    if result.get("status") != "ok":
        raise LinkageError("Qdrant response status is invalid")
    return dict(result)


QdrantRequest = Callable[[str, str, object | None], dict[str, Any]]


def qdrant_read_live_point(
    *,
    request: QdrantRequest = _qdrant_request,
) -> dict[str, Any]:
    """Read the exact fixture and prove its filtered cardinality is one."""

    point_path = (
        f"/collections/{QDRANT_COLLECTION}/points/"
        f"{urllib.parse.quote(QDRANT_POINT_ID, safe='')}"
        "?with_payload=true&with_vector=true"
    )
    point_document = _mapping(
        request("GET", point_path, None), label="Qdrant point response"
    )
    point = dict(_mapping(point_document.get("result"), label="Qdrant point"))
    expected_material = fixture_material()
    build_proof(
        authority_record=expected_material["authority_record"],
        object_bytes=expected_material["object_bytes"],
        qdrant_point=point,
    )

    scroll_document = _mapping(
        request(
            "POST",
            f"/collections/{QDRANT_COLLECTION}/points/scroll",
            {
                "filter": {
                    "must": [
                        {"key": "fixture_id", "match": {"value": FIXTURE_ID}},
                        {"key": "tenant_id", "match": {"value": TENANT_ID}},
                        {"key": "project_id", "match": {"value": PROJECT_ID}},
                    ]
                },
                "limit": 2,
                "with_payload": True,
                "with_vector": True,
            },
        ),
        label="Qdrant filtered response",
    )
    result = _mapping(scroll_document.get("result"), label="Qdrant filtered result")
    points = result.get("points")
    if (
        not isinstance(points, list)
        or len(points) != 1
        or result.get("next_page_offset") is not None
    ):
        raise LinkageError("Qdrant fixture query must return exactly one point")
    filtered_point = _mapping(points[0], label="Qdrant filtered point")
    if not hmac.compare_digest(
        _canonical_bytes(point, label="Qdrant point"),
        _canonical_bytes(filtered_point, label="Qdrant filtered point"),
    ):
        raise LinkageError("Qdrant direct and filtered reads do not match")
    return point


def qdrant_seed_from_authorities(
    *,
    authority_record: object,
    object_bytes: object,
    request: QdrantRequest = _qdrant_request,
) -> dict[str, Any]:
    """Create the fixed fixture only in a previously empty Qdrant target."""

    expected_point = expected_point_from_authorities(
        authority_record=authority_record,
        object_bytes=object_bytes,
    )
    try:
        request("GET", f"/collections/{QDRANT_COLLECTION}", None)
    except QdrantNotFound:
        pass
    else:
        raise LinkageError("Qdrant recovery target must be empty")
    request(
        "PUT",
        f"/collections/{QDRANT_COLLECTION}",
        # Cosine collections normalize vectors on write.  The recovery proof
        # compares exact stored values, so the synthetic gate uses Dot.
        {"vectors": {"distance": "Dot", "size": len(expected_point["vector"])}},
    )
    request(
        "PUT",
        f"/collections/{QDRANT_COLLECTION}/points?wait=true",
        {"points": [expected_point]},
    )
    observed = qdrant_read_live_point(request=request)
    if not hmac.compare_digest(
        _canonical_bytes(expected_point, label="expected Qdrant point"),
        _canonical_bytes(observed, label="observed Qdrant point"),
    ):
        raise LinkageError("Qdrant rebuilt point differs from the authorities")
    return observed


def _object_document() -> dict[str, object]:
    return {
        "fixture_id": FIXTURE_ID,
        "kind": "synthetic-release-recovery-fixture",
        "payload": {
            "contains_customer_data": False,
            "purpose": "cross-store-backup-restore-linkage",
        },
        "schema_version": OBJECT_SCHEMA_VERSION,
    }


def _object_ref(object_content_sha256: str) -> dict[str, Any]:
    document: dict[str, Any] = {
        "bucket": OBJECT_BUCKET,
        "key": OBJECT_KEY,
        "content_sha256": object_content_sha256,
        "locator_sha256": _digest(
            {"bucket": OBJECT_BUCKET, "key": OBJECT_KEY},
            label="object identity",
        ),
        "size_bytes": len(
            _canonical_document(_object_document(), label="fixture object")
        ),
    }
    return document


def _expected_vector(object_content_sha256: str) -> list[float]:
    digest_bytes = bytes.fromhex(object_content_sha256)
    # Binary fractions round-trip exactly through Qdrant's float32 storage.
    return [((value & 0x0F) + 1) / 16.0 for value in digest_bytes[:8]]


def fixture_material() -> dict[str, object]:
    """Return the one canonical synthetic fixture used by the release gate."""

    object_bytes = _canonical_document(_object_document(), label="fixture object")
    object_content_sha256 = hashlib.sha256(object_bytes).hexdigest()
    object_ref = _object_ref(object_content_sha256)
    authority_record = {
        "collection": AUTHORITY_COLLECTION,
        "resource_key": FIXTURE_ID,
        "tenant_id": TENANT_ID,
        "project_id": PROJECT_ID,
        "status": "verified",
        "trace_id": TRACE_ID,
        "data": {
            "authority_schema_version": "auris-flow.recovery-linkage-authority/v1",
            "contains_customer_data": False,
            "evidence_scope": "synthetic-release-gate-only",
            "fixture_id": FIXTURE_ID,
            "object_ref": object_ref,
            "qdrant_ref": {
                "collection": QDRANT_COLLECTION,
                "point_id": QDRANT_POINT_ID,
                "point_identity_version": "fixed-fixture-v1",
            },
        },
    }
    qdrant_point = {
        "id": QDRANT_POINT_ID,
        "payload": {
            "authority_ref": {
                "collection": AUTHORITY_COLLECTION,
                "resource_key": FIXTURE_ID,
            },
            "contains_customer_data": False,
            "evidence_scope": "synthetic-release-gate-only",
            "fixture_id": FIXTURE_ID,
            "object_ref": object_ref,
            "point_identity_version": "fixed-fixture-v1",
            "project_id": PROJECT_ID,
            "tenant_id": TENANT_ID,
            "trace_id": TRACE_ID,
        },
        "vector": _expected_vector(object_content_sha256),
    }
    return {
        "authority_record": authority_record,
        "object_bytes": object_bytes,
        "qdrant_point": qdrant_point,
    }


def expected_point_from_authorities(
    *,
    authority_record: object,
    object_bytes: object,
) -> dict[str, Any]:
    """Derive the exact Qdrant fixture solely from restored authorities."""

    _, object_content_sha256, _ = _validated_object(object_bytes)
    _validated_authority(
        authority_record,
        object_content_sha256=object_content_sha256,
    )
    expected = fixture_material()["qdrant_point"]
    assert isinstance(expected, dict)
    # Canonical JSON round-tripping produces a detached, JSON-only copy.
    detached = json.loads(_canonical_bytes(expected, label="Qdrant point"))
    assert isinstance(detached, dict)
    return detached


def _sql_utf8(value: str) -> str:
    return f"CONVERT(UNHEX('{value.encode('utf-8').hex()}') USING utf8mb4)"


def mysql_seed_sql() -> bytes:
    """Return a fixed, input-free SQL statement for the synthetic authority."""

    material = fixture_material()
    authority = material["authority_record"]
    assert isinstance(authority, dict)
    data = authority["data"]
    encoded_data = _canonical_bytes(data, label="authority data").hex()
    fields = (
        "collection",
        "resource_key",
        "tenant_id",
        "project_id",
        "status",
        "trace_id",
    )
    values = ",\n  ".join(_sql_utf8(str(authority[field])) for field in fields)
    statement = (
        "INSERT INTO auris_flow.json_resources\n"
        "  (collection, resource_key, tenant_id, project_id, status, trace_id, data)\n"
        "VALUES\n"
        f"  ({values},\n"
        f"  CAST(CONVERT(UNHEX('{encoded_data}') USING utf8mb4) AS JSON));\n"
    )
    return statement.encode("ascii")


def mysql_export_sql() -> bytes:
    """Return the exact fixed-scope live authority projection query."""

    fields = (
        ("tenant_id", TENANT_ID),
        ("project_id", PROJECT_ID),
        ("collection", AUTHORITY_COLLECTION),
        ("resource_key", FIXTURE_ID),
        ("status", "verified"),
        ("trace_id", TRACE_ID),
    )
    predicates = "\n  AND ".join(
        f"{field} = {_sql_utf8(value)}" for field, value in fields
    )
    statement = (
        "SELECT JSON_OBJECT(\n"
        "  'collection', collection,\n"
        "  'resource_key', resource_key,\n"
        "  'tenant_id', tenant_id,\n"
        "  'project_id', project_id,\n"
        "  'status', status,\n"
        "  'trace_id', trace_id,\n"
        "  'data', data\n"
        ")\n"
        "FROM auris_flow.json_resources\n"
        f"WHERE {predicates}\n"
        "ORDER BY id\n"
        "LIMIT 2;\n"
    )
    return statement.encode("ascii")


def _validated_object(object_bytes: object) -> tuple[bytes, str, dict[str, Any]]:
    if not isinstance(object_bytes, bytes):
        raise LinkageError("object bytes are invalid")
    if not 0 < len(object_bytes) <= MAX_OBJECT_BYTES:
        raise LinkageError("object size is invalid")
    document = _decode_json(object_bytes, label="object")
    if not isinstance(document, dict):
        raise LinkageError("object must be a JSON object")
    canonical = _canonical_document(document, label="fixture object")
    if not hmac.compare_digest(canonical, object_bytes):
        raise LinkageError("object must use canonical encoding")
    if document != _object_document():
        raise LinkageError("object content does not match the fixture contract")
    return object_bytes, hashlib.sha256(object_bytes).hexdigest(), document


def _validated_authority(
    authority_record: object,
    *,
    object_content_sha256: str,
) -> dict[str, Any]:
    authority = dict(_mapping(authority_record, label="authority record"))
    expected = fixture_material()["authority_record"]
    assert isinstance(expected, dict)

    for field in ("tenant_id", "project_id", "trace_id"):
        if authority.get(field) != expected[field]:
            raise LinkageError("authority scope does not match the fixture contract")
    if {
        key: authority.get(key) for key in ("collection", "resource_key", "status")
    } != {key: expected[key] for key in ("collection", "resource_key", "status")}:
        raise LinkageError("authority identity does not match the fixture contract")
    if set(authority) != set(expected):
        raise LinkageError("authority record fields are invalid")

    data = dict(_mapping(authority.get("data"), label="authority data"))
    expected_data = expected["data"]
    assert isinstance(expected_data, dict)
    if set(data) != set(expected_data):
        raise LinkageError("authority object binding fields are invalid")
    object_ref = dict(_mapping(data.get("object_ref"), label="authority object ref"))
    expected_object_ref = _object_ref(object_content_sha256)
    if object_ref != expected_object_ref:
        raise LinkageError("authority object binding is invalid")
    if data != expected_data:
        raise LinkageError("authority object or point binding is invalid")
    return authority


def _validated_point(
    qdrant_point: object,
    *,
    authority: Mapping[str, Any],
    object_content_sha256: str,
) -> dict[str, Any]:
    point = dict(_mapping(qdrant_point, label="Qdrant point"))
    if set(point) != {"id", "payload", "vector"}:
        raise LinkageError("point fields are invalid")
    if point.get("id") != QDRANT_POINT_ID:
        raise LinkageError("point identity does not match the fixture contract")

    payload = dict(_mapping(point.get("payload"), label="Qdrant payload"))
    expected = fixture_material()["qdrant_point"]
    assert isinstance(expected, dict)
    expected_payload = expected["payload"]
    assert isinstance(expected_payload, dict)
    if payload != expected_payload:
        raise LinkageError("payload does not match the authority and object bindings")
    if payload.get("tenant_id") != authority.get("tenant_id"):
        raise LinkageError("payload scope does not match the authority")
    if payload.get("project_id") != authority.get("project_id"):
        raise LinkageError("payload scope does not match the authority")
    if payload.get("trace_id") != authority.get("trace_id"):
        raise LinkageError("payload trace does not match the authority")
    if payload.get("object_ref") != _object_ref(object_content_sha256):
        raise LinkageError("payload object binding is invalid")

    vector = point.get("vector")
    if (
        not isinstance(vector, list)
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in vector
        )
        or [float(value) for value in vector] != _expected_vector(object_content_sha256)
    ):
        raise LinkageError("vector does not match the immutable object")
    point["vector"] = [float(value) for value in vector]
    return point


def build_proof(
    *,
    authority_record: object,
    object_bytes: object,
    qdrant_point: object,
) -> dict[str, str]:
    """Validate independently observed material and return its public proof."""

    _, object_content_sha256, _ = _validated_object(object_bytes)
    authority = _validated_authority(
        authority_record,
        object_content_sha256=object_content_sha256,
    )
    point = _validated_point(
        qdrant_point,
        authority=authority,
        object_content_sha256=object_content_sha256,
    )
    payload = point["payload"]
    vector = point["vector"]
    proof = {
        "schema_version": SCHEMA_VERSION,
        "fixture_id": FIXTURE_ID,
        "authority_record_sha256": _digest(authority, label="authority record"),
        "object_identity_sha256": _digest(
            {"bucket": OBJECT_BUCKET, "key": OBJECT_KEY},
            label="object identity",
        ),
        "object_content_sha256": object_content_sha256,
        "qdrant_point_identity_sha256": _digest(
            {"collection": QDRANT_COLLECTION, "point_id": point["id"]},
            label="Qdrant point identity",
        ),
        "qdrant_payload_sha256": _digest(payload, label="Qdrant payload"),
        "qdrant_vector_sha256": _digest(vector, label="Qdrant vector"),
    }
    proof["linkage_sha256"] = _digest(proof, label="cross-store linkage")
    return proof


def validate_proof(proof: object) -> dict[str, str]:
    """Validate a public proof without requiring access to any backing store."""

    document = dict(_mapping(proof, label="proof"))
    if set(document) != PROOF_FIELDS:
        raise LinkageError("proof fields are invalid")
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or document.get("fixture_id") != FIXTURE_ID
    ):
        raise LinkageError("proof identity is invalid")
    for field in PROOF_FIELDS:
        if not field.endswith("_sha256"):
            continue
        value = document.get(field)
        if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
            raise LinkageError("proof digest is invalid")
    linkage = document.pop("linkage_sha256")
    expected_linkage = _digest(document, label="cross-store linkage")
    if not hmac.compare_digest(linkage, expected_linkage):
        raise LinkageError("proof linkage digest is invalid")
    document["linkage_sha256"] = linkage
    return document


def build_receipt(
    *,
    source_proof: object,
    observed_proof: object,
    phase: str,
    challenge: str,
    backup_id: str,
    backup_manifest_sha256: str,
    source_commit: str,
    release_tag: str,
    release_metadata_sha256: str,
    drill_project: str,
) -> dict[str, str]:
    """Bind one live observation to its exact signed backup and drill run."""

    source = validate_proof(source_proof)
    observed = validate_proof(observed_proof)
    if not hmac.compare_digest(
        _canonical_bytes(source, label="source proof"),
        _canonical_bytes(observed, label="observed proof"),
    ):
        raise LinkageError("observed proof does not match the signed source proof")
    if phase not in {"snapshot", "rebuild"}:
        raise LinkageError("receipt phase is invalid")
    if not isinstance(challenge, str) or SHA256_RE.fullmatch(challenge) is None:
        raise LinkageError("receipt challenge is invalid")
    if BACKUP_ID_RE.fullmatch(backup_id) is None:
        raise LinkageError("receipt backup identity is invalid")
    if SHA256_RE.fullmatch(backup_manifest_sha256) is None:
        raise LinkageError("receipt manifest identity is invalid")
    if COMMIT_RE.fullmatch(source_commit) is None:
        raise LinkageError("receipt source identity is invalid")
    if RELEASE_TAG_RE.fullmatch(release_tag) is None:
        raise LinkageError("receipt release identity is invalid")
    if SHA256_RE.fullmatch(release_metadata_sha256) is None:
        raise LinkageError("receipt release metadata identity is invalid")
    if DRILL_PROJECT_RE.fullmatch(drill_project) is None:
        raise LinkageError("receipt drill project is invalid")
    proof_sha256 = _digest(source, label="proof")
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "phase": phase,
        "fixture_id": FIXTURE_ID,
        "challenge_sha256": hashlib.sha256(bytes.fromhex(challenge)).hexdigest(),
        "backup_id": backup_id,
        "backup_manifest_sha256": backup_manifest_sha256,
        "source_commit": source_commit,
        "release_tag": release_tag,
        "release_metadata_sha256": release_metadata_sha256,
        "drill_project": drill_project,
        "source_proof_sha256": proof_sha256,
        "observed_proof_sha256": proof_sha256,
    }
    receipt["receipt_sha256"] = _digest(receipt, label="receipt")
    return receipt


def validate_receipt(receipt: object) -> dict[str, str]:
    """Validate the strict public receipt shape and its transitive digest."""

    document = dict(_mapping(receipt, label="receipt"))
    if set(document) != RECEIPT_FIELDS:
        raise LinkageError("receipt fields are invalid")
    if (
        document.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or document.get("phase") not in {"snapshot", "rebuild"}
        or document.get("fixture_id") != FIXTURE_ID
        or BACKUP_ID_RE.fullmatch(str(document.get("backup_id") or "")) is None
        or COMMIT_RE.fullmatch(str(document.get("source_commit") or "")) is None
        or RELEASE_TAG_RE.fullmatch(str(document.get("release_tag") or "")) is None
        or DRILL_PROJECT_RE.fullmatch(str(document.get("drill_project") or "")) is None
    ):
        raise LinkageError("receipt identity is invalid")
    for field in RECEIPT_FIELDS:
        if not field.endswith("_sha256"):
            continue
        value = document.get(field)
        if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
            raise LinkageError("receipt digest is invalid")
    if not hmac.compare_digest(
        str(document["source_proof_sha256"]),
        str(document["observed_proof_sha256"]),
    ):
        raise LinkageError("receipt proof digests do not match")
    receipt_sha256 = document.pop("receipt_sha256")
    expected = _digest(document, label="receipt")
    if not hmac.compare_digest(receipt_sha256, expected):
        raise LinkageError("receipt digest is invalid")
    document["receipt_sha256"] = receipt_sha256
    return document


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser(
        "build-proof",
        help="build a proof from three independently captured live inputs",
    )
    build.add_argument("--authority-json", required=True)
    build.add_argument("--object-file", required=True)
    build.add_argument("--qdrant-point-json", required=True)
    build.add_argument("--output", required=True)

    validate = commands.add_parser("validate-proof")
    validate.add_argument("--input", required=True)

    fixture = commands.add_parser(
        "write-fixture",
        help="publish the canonical MySQL authority projection and MinIO object",
    )
    fixture.add_argument("--authority-output", required=True)
    fixture.add_argument("--object-output", required=True)

    mysql_seed = commands.add_parser("write-mysql-seed-sql")
    mysql_seed.add_argument("--output", required=True)

    mysql_export = commands.add_parser("write-mysql-export-sql")
    mysql_export.add_argument("--output", required=True)

    qdrant_seed = commands.add_parser(
        "qdrant-seed",
        help="derive and seed the Qdrant fixture into an empty target",
    )
    qdrant_seed.add_argument("--authority-json", required=True)
    qdrant_seed.add_argument("--object-file", required=True)
    qdrant_seed.add_argument("--point-output", required=True)

    qdrant_export = commands.add_parser(
        "qdrant-export",
        help="independently read the live Qdrant fixture and exact cardinality",
    )
    qdrant_export.add_argument("--point-output", required=True)

    receipt = commands.add_parser(
        "build-receipt",
        help="bind an observed proof to one signed backup and isolated drill",
    )
    receipt.add_argument("--source-proof", required=True)
    receipt.add_argument("--observed-proof", required=True)
    receipt.add_argument("--phase", choices=("snapshot", "rebuild"), required=True)
    receipt.add_argument("--challenge", required=True)
    receipt.add_argument("--backup-id", required=True)
    receipt.add_argument("--backup-manifest-sha256", required=True)
    receipt.add_argument("--source-commit", required=True)
    receipt.add_argument("--release-tag", required=True)
    receipt.add_argument("--release-metadata-sha256", required=True)
    receipt.add_argument("--drill-project", required=True)
    receipt.add_argument("--output", required=True)

    validate_receipt_parser = commands.add_parser("validate-receipt")
    validate_receipt_parser.add_argument("--input", required=True)

    capture_json = commands.add_parser(
        "capture-json-stdin",
        help="capture one bounded live JSON document from standard input",
    )
    capture_json.add_argument("--output", required=True)

    capture_object = commands.add_parser(
        "capture-object-stdin",
        help="capture the exact bounded fixture object from standard input",
    )
    capture_object.add_argument("--output", required=True)

    publish_proof = commands.add_parser(
        "publish-proof-stdin",
        help="validate and exclusively publish one digest-only proof",
    )
    publish_proof.add_argument("--output", required=True)

    commands.add_parser(
        "validate-object-stat-stdin",
        help="validate the bounded MinIO stat response for the fixture object",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "build-proof":
            authority = read_json_input(args.authority_json)
            object_bytes = _read_regular_input(
                args.object_file,
                maximum=MAX_OBJECT_BYTES,
                label="object input",
            )
            point = read_json_input(args.qdrant_point_json)
            _write_json_exclusive(
                args.output,
                build_proof(
                    authority_record=authority,
                    object_bytes=object_bytes,
                    qdrant_point=point,
                ),
            )
            return 0
        if args.command == "validate-proof":
            validate_proof(read_json_input(args.input))
            return 0
        if args.command == "write-fixture":
            material = fixture_material()
            _write_json_exclusive(
                args.authority_output,
                material["authority_record"],
            )
            object_bytes = material["object_bytes"]
            assert isinstance(object_bytes, bytes)
            _write_bytes_exclusive(args.object_output, object_bytes)
            return 0
        if args.command == "write-mysql-seed-sql":
            _write_bytes_exclusive(args.output, mysql_seed_sql())
            return 0
        if args.command == "write-mysql-export-sql":
            _write_bytes_exclusive(args.output, mysql_export_sql())
            return 0
        if args.command == "qdrant-seed":
            authority = read_json_input(args.authority_json)
            object_bytes = _read_regular_input(
                args.object_file,
                maximum=MAX_OBJECT_BYTES,
                label="object input",
            )
            _write_json_exclusive(
                args.point_output,
                qdrant_seed_from_authorities(
                    authority_record=authority,
                    object_bytes=object_bytes,
                ),
            )
            return 0
        if args.command == "qdrant-export":
            _write_json_exclusive(
                args.point_output,
                qdrant_read_live_point(),
            )
            return 0
        if args.command == "build-receipt":
            _write_json_exclusive(
                args.output,
                build_receipt(
                    source_proof=read_json_input(args.source_proof),
                    observed_proof=read_json_input(args.observed_proof),
                    phase=args.phase,
                    challenge=args.challenge,
                    backup_id=args.backup_id,
                    backup_manifest_sha256=args.backup_manifest_sha256,
                    source_commit=args.source_commit,
                    release_tag=args.release_tag,
                    release_metadata_sha256=args.release_metadata_sha256,
                    drill_project=args.drill_project,
                ),
            )
            return 0
        if args.command == "validate-receipt":
            validate_receipt(read_json_input(args.input))
            return 0
        if args.command == "capture-json-stdin":
            _capture_json_stdin(args.output)
            return 0
        if args.command == "capture-object-stdin":
            _capture_object_stdin(args.output)
            return 0
        if args.command == "publish-proof-stdin":
            _publish_proof_stdin(args.output)
            return 0
        if args.command == "validate-object-stat-stdin":
            _validate_object_stat_stdin()
            return 0
    except (LinkageError, OSError) as exc:
        print(f"recovery linkage error: {exc}", file=sys.stderr)
        return 2
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
