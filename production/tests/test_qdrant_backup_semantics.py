from __future__ import annotations

import argparse
import importlib.util
import json
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
QDRANT_TOOL = ROOT / "production" / "backup" / "qdrant_snapshots.py"
COLLECTION = "knowledge_chunks"
POINT_A = "00000000-0000-0000-0000-000000000001"
POINT_B = "00000000-0000-0000-0000-000000000002"


def load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "qdrant_snapshot_semantics", QDRANT_TOOL
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def point(
    point_id: str,
    *,
    tenant_id: str = "tenant-a",
    project_id: str = "project-a",
    marker: str = "top-secret-a",
    vector: list[float] | None = None,
) -> dict[str, Any]:
    return {
        "id": point_id,
        "payload": {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "marker": marker,
            "nested": {"enabled": True, "rank": 1},
        },
        "vector": vector or [0.125, 0.25, 0.5],
    }


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_length: str | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self.offset = 0

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body) - self.offset
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


def test_qdrant_redirect_handler_rejects_before_replaying_api_key() -> None:
    tool = load_tool()
    request = urllib.request.Request("http://qdrant:6333/collections")
    request.add_header("api-key", "must-not-be-forwarded")

    with pytest.raises(urllib.error.HTTPError) as error:
        tool._RejectRedirectHandler().redirect_request(  # noqa: SLF001
            request,
            None,
            307,
            "redirect",
            {},
            "https://attacker.example/steal",
        )

    assert error.value.code == 307


@pytest.mark.parametrize(
    "base_url",
    (
        "http://user:secret@qdrant:6333",
        "http://qdrant:6333/prefix",
        "http://qdrant:6333?redirect=evil",
        "file:///tmp/qdrant",
    ),
)
def test_qdrant_base_url_rejects_credential_or_path_confusion(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    tool = load_tool()
    monkeypatch.setattr(tool, "BASE_URL", base_url)

    with pytest.raises(tool.SnapshotError, match="base URL"):
        tool._request_url("/collections")  # noqa: SLF001


def test_qdrant_control_response_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = load_tool()
    monkeypatch.setattr(tool, "MAX_CONTROL_RESPONSE_BYTES", 8)
    monkeypatch.setattr(tool, "api_key", lambda: "test-key")
    monkeypatch.setattr(
        tool,
        "_open_request",
        lambda request, timeout: _Response(b'{"status":"ok"}', content_length="15"),
    )

    with pytest.raises(tool.SnapshotError, match="Qdrant GET /collections failed"):
        tool.request_json("GET", "/collections")


def test_qdrant_snapshot_download_enforces_declared_byte_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = load_tool()
    destination = tmp_path / "collection.snapshot"
    monkeypatch.setenv("AURIS_BACKUP_QDRANT_MAX_SNAPSHOT_BYTES", "8")
    monkeypatch.setattr(tool, "api_key", lambda: "test-key")
    monkeypatch.setattr(
        tool,
        "_open_request",
        lambda request, timeout: _Response(b"012345678", content_length="9"),
    )

    with pytest.raises(tool.SnapshotError, match="snapshot download failed"):
        tool.download("/collections/c/snapshots/s.snapshot", destination)

    assert not destination.exists()
    assert not destination.with_suffix(".snapshot.part").exists()


def install_scroll(
    monkeypatch: pytest.MonkeyPatch,
    tool: ModuleType,
    points: list[dict[str, Any]],
) -> list[tuple[str, str, dict[str, Any] | None]]:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request_json(
        method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        calls.append((method, path, body))
        assert method == "POST"
        assert path == f"/collections/{COLLECTION}/points/scroll"
        assert body == {
            "limit": 256,
            "with_payload": True,
            "with_vector": True,
        }
        return {
            "status": "ok",
            "result": {"points": points, "next_page_offset": None},
        }

    monkeypatch.setattr(tool, "request_json", request_json)
    return calls


def test_semantic_fingerprint_is_order_independent_and_metadata_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = load_tool()
    source_points = [
        point(POINT_B, marker="top-secret-b", vector=[0.75, 0.5, 0.25]),
        point(POINT_A),
    ]
    install_scroll(monkeypatch, tool, source_points)
    first = tool.collection_semantics(COLLECTION, expected_count=2)
    install_scroll(monkeypatch, tool, list(reversed(source_points)))
    second = tool.collection_semantics(COLLECTION, expected_count=2)

    assert first == second
    assert first["fingerprint_algorithm"] == "sha256-canonical-point-digests-v1"
    assert first["scope_policy"] == "tenant-project-required"
    assert first["probe"]["point_id"] == POINT_A
    assert set(first["probe"]) == {
        "point_id",
        "payload_sha256",
        "vector_sha256",
        "vector_kind",
        "scope",
    }
    serialized = json.dumps(first, sort_keys=True)
    assert "top-secret" not in serialized
    assert "0.125" not in serialized
    assert "0.75" not in serialized


def test_empty_collection_models_scope_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = load_tool()
    install_scroll(monkeypatch, tool, [])

    semantics = tool.collection_semantics(COLLECTION, expected_count=0)

    assert semantics["scope_policy"] == "empty-collection"
    assert semantics["probe"] is None
    assert semantics["points_fingerprint_sha256"]


@pytest.mark.parametrize(
    "bad_point,match",
    [
        (point(POINT_A, tenant_id=""), "tenant/project scope"),
        (point(POINT_A, project_id="*"), "tenant/project scope"),
        (
            {**point(POINT_A), "vector": {"named": [0.125, 0.25, 0.5]}},
            "unnamed dense vector",
        ),
    ],
)
def test_semantic_inventory_fails_closed_for_non_production_shapes(
    monkeypatch: pytest.MonkeyPatch,
    bad_point: dict[str, Any],
    match: str,
) -> None:
    tool = load_tool()
    install_scroll(monkeypatch, tool, [bad_point])

    with pytest.raises(tool.SnapshotError, match=match):
        tool.collection_semantics(COLLECTION, expected_count=1)


@pytest.mark.parametrize(
    "mutated",
    [
        point(POINT_B, marker="top-secret-c", vector=[0.75, 0.5, 0.25]),
        point(POINT_B, marker="top-secret-b", vector=[0.75, 0.5, 0.20]),
    ],
)
def test_same_count_payload_or_vector_tamper_fails_full_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    mutated: dict[str, Any],
) -> None:
    tool = load_tool()
    original = [
        point(POINT_A),
        point(POINT_B, marker="top-secret-b", vector=[0.75, 0.5, 0.25]),
    ]
    install_scroll(monkeypatch, tool, original)
    semantics = tool.collection_semantics(COLLECTION, expected_count=2)
    install_scroll(monkeypatch, tool, [original[0], mutated])

    with pytest.raises(tool.SnapshotError, match="semantic fingerprint"):
        tool.verify_collection_semantics(
            {"name": COLLECTION, "points_count": 2, "semantics": semantics}
        )


def test_probe_is_retrieved_by_id_then_used_for_scope_filtered_nearest_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = load_tool()
    points = [
        point(POINT_A),
        point(
            POINT_B,
            tenant_id="tenant-b",
            project_id="project-b",
            marker="other-scope",
            vector=[0.75, 0.5, 0.25],
        ),
    ]
    install_scroll(monkeypatch, tool, points)
    semantics = tool.collection_semantics(COLLECTION, expected_count=2)
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request_json(
        method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        calls.append((method, path, body))
        if path.endswith("/points/scroll"):
            return {
                "status": "ok",
                "result": {"points": points, "next_page_offset": None},
            }
        if path.endswith("/points"):
            assert body == {
                "ids": [POINT_A],
                "with_payload": True,
                "with_vector": True,
            }
            return {"status": "ok", "result": [points[0]]}
        if path.endswith("/points/search"):
            assert body is not None
            assert body["vector"] == points[0]["vector"]
            assert body["limit"] == 1
            assert body["with_payload"] is True
            assert body["with_vector"] is False
            assert body["filter"] == {
                "must": [
                    {"key": "tenant_id", "match": {"value": "tenant-a"}},
                    {"key": "project_id", "match": {"value": "project-a"}},
                    {"has_id": [POINT_A]},
                ]
            }
            return {
                "status": "ok",
                "result": [
                    {"id": POINT_A, "payload": points[0]["payload"], "score": 1.0}
                ],
            }
        raise AssertionError(path)

    monkeypatch.setattr(tool, "request_json", request_json)
    tool.verify_collection_semantics(
        {"name": COLLECTION, "points_count": 2, "semantics": semantics}
    )

    assert any(path.endswith("/points") for _, path, _ in calls)
    assert any(path.endswith("/points/search") for _, path, _ in calls)


def test_filtered_query_rejects_cross_scope_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = load_tool()
    source = point(POINT_A)
    install_scroll(monkeypatch, tool, [source])
    semantics = tool.collection_semantics(COLLECTION, expected_count=1)

    def request_json(
        method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        del method, body
        if path.endswith("/points/scroll"):
            return {
                "status": "ok",
                "result": {"points": [source], "next_page_offset": None},
            }
        if path.endswith("/points"):
            return {"status": "ok", "result": [source]}
        if path.endswith("/points/search"):
            leaked = point(POINT_A, tenant_id="tenant-b", project_id="project-b")
            return {
                "status": "ok",
                "result": [{"id": POINT_A, "payload": leaked["payload"], "score": 1.0}],
            }
        raise AssertionError(path)

    monkeypatch.setattr(tool, "request_json", request_json)
    with pytest.raises(tool.SnapshotError, match="cross-scope"):
        tool.verify_collection_semantics(
            {"name": COLLECTION, "points_count": 1, "semantics": semantics}
        )


def test_backup_metadata_v2_embeds_hashes_not_raw_points(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = load_tool()
    output = tmp_path / "qdrant"
    output.mkdir()
    semantics = {
        "fingerprint_algorithm": "sha256-canonical-point-digests-v1",
        "points_fingerprint_sha256": "a" * 64,
        "scope_policy": "tenant-project-required",
        "probe": {
            "point_id": POINT_A,
            "payload_sha256": "b" * 64,
            "vector_sha256": "c" * 64,
            "vector_kind": "unnamed-dense",
            "scope": {"tenant_id": "tenant-a", "project_id": "project-a"},
        },
    }
    monkeypatch.setattr(tool, "collection_names", lambda: [COLLECTION])
    monkeypatch.setattr(
        tool, "collection_info", lambda name: {"points_count": 1, "status": "green"}
    )
    monkeypatch.setattr(
        tool, "collection_semantics", lambda name, expected_count: semantics
    )
    monkeypatch.setattr(tool, "qdrant_version", lambda: "1.14.1")
    monkeypatch.setattr(tool, "alias_map", lambda: [])
    monkeypatch.setattr(
        tool,
        "request_json",
        lambda method, path, body=None: {
            "status": "ok",
            "result": {"name": "source.snapshot"},
        },
    )
    monkeypatch.setattr(
        tool,
        "download",
        lambda path, destination: destination.write_bytes(b"snapshot"),
    )

    tool.backup(argparse.Namespace(output=output, keep_server_snapshots=False))

    metadata = json.loads((output / "snapshots.json").read_text(encoding="utf-8"))
    assert metadata["schema_version"] == "auris-flow.qdrant-snapshots/v2"
    assert metadata["collections"][0]["semantics"] == semantics
    serialized = json.dumps(metadata, sort_keys=True)
    assert "top-secret" not in serialized
    assert '"vector":' not in serialized


def test_scroll_inventory_reads_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = load_tool()
    calls: list[dict[str, Any] | None] = []
    pages = {
        None: {
            "points": [point(POINT_B, marker="top-secret-b")],
            "next_page_offset": POINT_B,
        },
        POINT_B: {"points": [point(POINT_A)], "next_page_offset": None},
    }

    def request_json(
        method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        assert method == "POST"
        assert path == f"/collections/{COLLECTION}/points/scroll"
        calls.append(body)
        assert body is not None
        offset = body.get("offset")
        return {"status": "ok", "result": pages[offset]}

    monkeypatch.setattr(tool, "request_json", request_json)
    semantics = tool.collection_semantics(COLLECTION, expected_count=2)

    assert semantics["probe"]["point_id"] == POINT_A
    assert calls == [
        {"limit": 256, "with_payload": True, "with_vector": True},
        {
            "limit": 256,
            "with_payload": True,
            "with_vector": True,
            "offset": POINT_B,
        },
    ]


def test_exact_probe_read_must_match_recorded_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = load_tool()
    source = point(POINT_A)
    install_scroll(monkeypatch, tool, [source])
    semantics = tool.collection_semantics(COLLECTION, expected_count=1)

    def request_json(
        method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        del method, body
        if path.endswith("/points/scroll"):
            return {
                "status": "ok",
                "result": {"points": [source], "next_page_offset": None},
            }
        if path.endswith("/points"):
            return {
                "status": "ok",
                "result": [{**source, "vector": [0.125, 0.25, 0.75]}],
            }
        raise AssertionError(path)

    monkeypatch.setattr(tool, "request_json", request_json)
    with pytest.raises(tool.SnapshotError, match="probe vector hash"):
        tool.verify_collection_semantics(
            {"name": COLLECTION, "points_count": 1, "semantics": semantics}
        )


def test_legacy_semantic_metadata_schema_fails_closed(tmp_path: Path) -> None:
    tool = load_tool()
    root = tmp_path / "qdrant"
    root.mkdir()
    (root / "snapshots.json").write_text(
        json.dumps(
            {
                "schema_version": "auris-flow.qdrant-snapshots/v1",
                "qdrant_version": "1.14.1",
                "authority": "derived-rebuildable-from-mysql-and-minio",
                "collections": [],
                "aliases": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(tool.SnapshotError, match="unsupported.*schema"):
        tool.load_metadata(root)
