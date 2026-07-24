from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import urllib.error
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "production" / "backup" / "recovery_linkage.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("recovery_linkage", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _material(module: ModuleType) -> dict[str, object]:
    material = module.fixture_material()
    assert isinstance(material, dict)
    assert set(material) == {"authority_record", "object_bytes", "qdrant_point"}
    return material


def test_fixture_builds_one_deterministic_cross_store_proof() -> None:
    module = _load_module()
    material = _material(module)

    first = module.build_proof(**material)
    second = module.build_proof(**_material(module))

    assert first == second
    assert first["schema_version"] == "auris-flow.recovery-linkage-proof/v1"
    assert first["fixture_id"] == "release-recovery-linkage-v1"
    assert set(first) == {
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
    assert all(
        isinstance(value, str) and len(value) == 64
        for key, value in first.items()
        if key.endswith("_sha256")
    )
    assert module.validate_proof(first) == first


def test_authority_record_binds_exact_scope_object_and_qdrant_identity() -> None:
    module = _load_module()
    material = _material(module)
    authority = material["authority_record"]
    point = material["qdrant_point"]
    assert isinstance(authority, dict)
    assert isinstance(point, dict)
    data = authority["data"]
    payload = point["payload"]
    assert isinstance(data, dict)
    assert isinstance(payload, dict)

    assert authority == {
        "collection": "release_recovery_fixtures",
        "resource_key": "release-recovery-linkage-v1",
        "tenant_id": "auris_release",
        "project_id": "release_recovery_gate",
        "status": "verified",
        "trace_id": "trace_release_recovery_gate_0001",
        "data": data,
    }
    assert data["contains_customer_data"] is False
    assert data["object_ref"] == payload["object_ref"]
    assert data["qdrant_ref"] == {
        "collection": "auris_restore_gate",
        "point_id": point["id"],
        "point_identity_version": "fixed-fixture-v1",
    }
    assert payload["tenant_id"] == authority["tenant_id"]
    assert payload["project_id"] == authority["project_id"]
    assert payload["trace_id"] == authority["trace_id"]
    assert payload["authority_ref"] == {
        "collection": authority["collection"],
        "resource_key": authority["resource_key"],
    }


@pytest.mark.parametrize(
    ("target", "mutate", "error_fragment"),
    [
        (
            "authority_record",
            lambda value: value.update({"project_id": "other_project"}),
            "scope",
        ),
        (
            "authority_data",
            lambda value: value["object_ref"].update({"content_sha256": "0" * 64}),
            "object",
        ),
        (
            "object_bytes",
            lambda value: value + b"x",
            "object",
        ),
        (
            "qdrant_point",
            lambda value: value.update({"id": "00000000-0000-4000-8000-000000000000"}),
            "point",
        ),
        (
            "qdrant_payload",
            lambda value: value.update({"tenant_id": "other_tenant"}),
            "payload",
        ),
        (
            "qdrant_vector",
            lambda value: value.__setitem__(0, 0.0),
            "vector",
        ),
    ],
)
def test_cross_store_tampering_fails_closed(
    target: str,
    mutate,
    error_fragment: str,
) -> None:
    module = _load_module()
    material = copy.deepcopy(_material(module))
    authority = material["authority_record"]
    point = material["qdrant_point"]
    assert isinstance(authority, dict)
    assert isinstance(point, dict)

    if target == "authority_record":
        mutate(authority)
    elif target == "authority_data":
        data = authority["data"]
        assert isinstance(data, dict)
        mutate(data)
    elif target == "object_bytes":
        object_bytes = material["object_bytes"]
        assert isinstance(object_bytes, bytes)
        material["object_bytes"] = mutate(object_bytes)
    elif target == "qdrant_point":
        mutate(point)
    elif target == "qdrant_payload":
        payload = point["payload"]
        assert isinstance(payload, dict)
        mutate(payload)
    else:
        vector = point["vector"]
        assert isinstance(vector, list)
        mutate(vector)

    with pytest.raises(module.LinkageError, match=error_fragment):
        module.build_proof(**material)


def test_noncanonical_or_oversized_object_is_rejected() -> None:
    module = _load_module()
    material = _material(module)
    object_bytes = material["object_bytes"]
    assert isinstance(object_bytes, bytes)
    document = json.loads(object_bytes)
    material["object_bytes"] = json.dumps(document, indent=2).encode("utf-8")

    with pytest.raises(module.LinkageError, match="canonical"):
        module.build_proof(**material)

    material = _material(module)
    material["object_bytes"] = b"x" * (module.MAX_OBJECT_BYTES + 1)
    with pytest.raises(module.LinkageError, match="size"):
        module.build_proof(**material)


def test_proof_tampering_and_unknown_fields_are_rejected() -> None:
    module = _load_module()
    material = _material(module)
    proof = module.build_proof(**material)

    tampered = {**proof, "object_content_sha256": "f" * 64}
    with pytest.raises(module.LinkageError, match="linkage"):
        module.validate_proof(tampered)

    with pytest.raises(module.LinkageError, match="fields"):
        module.validate_proof({**proof, "operator_path": "/tmp/recovery"})


def test_public_proof_contains_no_raw_object_key_path_or_secret_shaped_field() -> None:
    module = _load_module()
    proof = module.build_proof(**_material(module))
    serialized = json.dumps(proof, sort_keys=True)

    assert "release-gate/" not in serialized
    assert "/Users/" not in serialized
    assert "/home/" not in serialized
    assert "password" not in serialized
    assert "secret" not in serialized
    assert "token" not in serialized


def test_expected_point_is_derived_from_independent_authority_and_object_reads() -> (
    None
):
    module = _load_module()
    material = _material(module)

    point = module.expected_point_from_authorities(
        authority_record=copy.deepcopy(material["authority_record"]),
        object_bytes=bytes(material["object_bytes"]),
    )

    assert point == material["qdrant_point"]
    point["payload"]["tenant_id"] = "other_tenant"
    assert material["qdrant_point"]["payload"]["tenant_id"] == "auris_release"


def test_build_proof_cli_reads_three_independent_regular_files(tmp_path: Path) -> None:
    module = _load_module()
    material = _material(module)
    authority_path = tmp_path / "authority.json"
    object_path = tmp_path / "object.json"
    point_path = tmp_path / "point.json"
    output_path = tmp_path / "proof.json"
    authority_path.write_text(
        json.dumps(material["authority_record"], sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    object_path.write_bytes(material["object_bytes"])
    point_path.write_text(
        json.dumps(material["qdrant_point"], sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    assert (
        module.main(
            [
                "build-proof",
                "--authority-json",
                os.fspath(authority_path),
                "--object-file",
                os.fspath(object_path),
                "--qdrant-point-json",
                os.fspath(point_path),
                "--output",
                os.fspath(output_path),
            ]
        )
        == 0
    )
    proof = json.loads(output_path.read_text(encoding="utf-8"))
    assert proof == module.build_proof(**material)
    assert output_path.stat().st_mode & 0o777 == 0o600


def test_json_input_rejects_duplicate_keys_symlinks_and_oversize(
    tmp_path: Path,
) -> None:
    module = _load_module()
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"tenant_id":"a","tenant_id":"b"}', encoding="utf-8")
    with pytest.raises(module.LinkageError, match="duplicate"):
        module.read_json_input(duplicate)

    regular = tmp_path / "regular.json"
    regular.write_text("{}", encoding="utf-8")
    linked = tmp_path / "linked.json"
    linked.symlink_to(regular)
    with pytest.raises(module.LinkageError, match="regular"):
        module.read_json_input(linked)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (module.MAX_JSON_INPUT_BYTES + 1))
    with pytest.raises(module.LinkageError, match="size"):
        module.read_json_input(oversized)


def test_bounded_stdin_capture_rejects_oversized_live_inputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    oversized_output = tmp_path / "oversized-object.json"
    monkeypatch.setattr(
        module.sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(b"x" * (module.MAX_OBJECT_BYTES + 1)),
            encoding="utf-8",
        ),
    )

    assert (
        module.main(
            [
                "capture-object-stdin",
                "--output",
                os.fspath(oversized_output),
            ]
        )
        == 2
    )
    assert not oversized_output.exists()

    duplicate_output = tmp_path / "duplicate-authority.json"
    monkeypatch.setattr(
        module.sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(b'{"fixture_id":"a","fixture_id":"b"}\n'),
            encoding="utf-8",
        ),
    )
    assert (
        module.main(
            [
                "capture-json-stdin",
                "--output",
                os.fspath(duplicate_output),
            ]
        )
        == 2
    )
    assert not duplicate_output.exists()


def test_bounded_stdin_capture_and_minio_stat_validate_exact_fixture(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    material = _material(module)
    object_output = tmp_path / "captured-object.json"
    monkeypatch.setattr(
        module.sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(material["object_bytes"]),
            encoding="utf-8",
        ),
    )

    assert (
        module.main(
            [
                "capture-object-stdin",
                "--output",
                os.fspath(object_output),
            ]
        )
        == 0
    )
    assert object_output.read_bytes() == material["object_bytes"]

    expected_size = len(material["object_bytes"])
    monkeypatch.setattr(
        module.sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(
                json.dumps({"status": "success", "size": expected_size}).encode()
            ),
            encoding="utf-8",
        ),
    )
    assert module.main(["validate-object-stat-stdin"]) == 0

    monkeypatch.setattr(
        module.sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(
                json.dumps({"status": "success", "size": expected_size + 1}).encode()
            ),
            encoding="utf-8",
        ),
    )
    assert module.main(["validate-object-stat-stdin"]) == 2


def test_json_decoder_and_canonicalizer_fail_closed_without_tracebacks() -> None:
    module = _load_module()

    with pytest.raises(module.LinkageError, match="invalid"):
        module._decode_json(
            b'{"oversized_integer":' + (b"9" * 5000) + b"}",
            label="hostile JSON",
        )

    recursive: list[object] = []
    recursive.append(recursive)
    with pytest.raises(module.LinkageError, match="canonical"):
        module._canonical_bytes(recursive, label="recursive value")


def test_publish_proof_stdin_validates_before_exclusive_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    proof = module.build_proof(**_material(module))
    output = tmp_path / "published-proof.json"
    monkeypatch.setattr(
        module.sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(json.dumps(proof).encode()),
            encoding="utf-8",
        ),
    )

    assert (
        module.main(
            [
                "publish-proof-stdin",
                "--output",
                os.fspath(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text(encoding="utf-8")) == proof

    invalid_output = tmp_path / "invalid-proof.json"
    monkeypatch.setattr(
        module.sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(json.dumps({**proof, "linkage_sha256": "0" * 64}).encode()),
            encoding="utf-8",
        ),
    )
    assert (
        module.main(
            [
                "publish-proof-stdin",
                "--output",
                os.fspath(invalid_output),
            ]
        )
        == 2
    )
    assert not invalid_output.exists()


def test_receipt_binds_backup_release_challenge_phase_project_and_both_proofs() -> None:
    module = _load_module()
    proof = module.build_proof(**_material(module))
    context = {
        "phase": "snapshot",
        "challenge": "11" * 32,
        "backup_id": "auris-flow-20260724T010203Z-a" + "1" * 11,
        "backup_manifest_sha256": "2" * 64,
        "source_commit": "3" * 40,
        "release_tag": "v1.0.0-rc.1",
        "release_metadata_sha256": "4" * 64,
        "drill_project": "auris-flow-restore-drill-" + "5" * 12,
    }

    receipt = module.build_receipt(
        source_proof=proof,
        observed_proof=copy.deepcopy(proof),
        **context,
    )

    assert receipt["schema_version"] == "auris-flow.recovery-linkage-receipt/v1"
    assert receipt["phase"] == "snapshot"
    assert receipt["source_proof_sha256"] == receipt["observed_proof_sha256"]
    assert "challenge" not in receipt
    assert module.validate_receipt(receipt) == receipt


def test_receipt_rejects_cross_run_splice_and_tampering() -> None:
    module = _load_module()
    proof = module.build_proof(**_material(module))
    changed = {**proof, "linkage_sha256": "f" * 64}
    common = {
        "phase": "rebuild",
        "challenge": "11" * 32,
        "backup_id": "auris-flow-20260724T010203Z-a" + "1" * 11,
        "backup_manifest_sha256": "2" * 64,
        "source_commit": "3" * 40,
        "release_tag": "v1.0.0-rc.1",
        "release_metadata_sha256": "4" * 64,
        "drill_project": "auris-flow-restore-drill-" + "5" * 12,
    }

    with pytest.raises(module.LinkageError, match="proof"):
        module.build_receipt(
            source_proof=proof,
            observed_proof=changed,
            **common,
        )

    receipt = module.build_receipt(
        source_proof=proof,
        observed_proof=proof,
        **common,
    )
    with pytest.raises(module.LinkageError, match="receipt"):
        module.validate_receipt({**receipt, "drill_project": "other-project"})


def test_qdrant_seed_derives_from_authorities_and_requires_empty_collection(
    monkeypatch,
) -> None:
    module = _load_module()
    material = _material(module)
    calls: list[tuple[str, str, object | None]] = []

    def request(
        method: str, path: str, body: object | None = None
    ) -> dict[str, object]:
        calls.append((method, path, body))
        if method == "GET" and path == "/collections/auris_restore_gate":
            raise module.QdrantNotFound("missing")
        if method == "GET" and "/points/" in path:
            return {"status": "ok", "result": copy.deepcopy(material["qdrant_point"])}
        if path.endswith("/points/scroll"):
            return {
                "status": "ok",
                "result": {
                    "points": [copy.deepcopy(material["qdrant_point"])],
                    "next_page_offset": None,
                },
            }
        return {"status": "ok", "result": True}

    point = module.qdrant_seed_from_authorities(
        authority_record=material["authority_record"],
        object_bytes=material["object_bytes"],
        request=request,
    )

    assert point == material["qdrant_point"]
    assert [call[:2] for call in calls] == [
        ("GET", "/collections/auris_restore_gate"),
        ("PUT", "/collections/auris_restore_gate"),
        ("PUT", "/collections/auris_restore_gate/points?wait=true"),
        (
            "GET",
            "/collections/auris_restore_gate/points/"
            + material["qdrant_point"]["id"]
            + "?with_payload=true&with_vector=true",
        ),
        ("POST", "/collections/auris_restore_gate/points/scroll"),
    ]
    create = calls[1][2]
    assert create == {"vectors": {"distance": "Dot", "size": 8}}
    upsert = calls[2][2]
    assert upsert == {"points": [material["qdrant_point"]]}
    scroll = calls[4][2]
    assert isinstance(scroll, dict)
    assert scroll["limit"] == 2
    assert scroll["with_payload"] is True
    assert scroll["with_vector"] is True


def test_qdrant_seed_refuses_existing_collection_before_any_write() -> None:
    module = _load_module()
    material = _material(module)
    calls: list[tuple[str, str]] = []

    def request(
        method: str, path: str, body: object | None = None
    ) -> dict[str, object]:
        del body
        calls.append((method, path))
        return {"status": "ok", "result": {"status": "green"}}

    with pytest.raises(module.LinkageError, match="empty"):
        module.qdrant_seed_from_authorities(
            authority_record=material["authority_record"],
            object_bytes=material["object_bytes"],
            request=request,
        )
    assert calls == [("GET", "/collections/auris_restore_gate")]


def test_qdrant_live_read_rejects_wrong_payload_or_duplicate_match() -> None:
    module = _load_module()
    material = _material(module)
    wrong = copy.deepcopy(material["qdrant_point"])
    wrong["payload"]["project_id"] = "other_project"

    def wrong_request(
        method: str, path: str, body: object | None = None
    ) -> dict[str, object]:
        del body
        if method == "GET":
            return {"status": "ok", "result": wrong}
        return {
            "status": "ok",
            "result": {"points": [wrong], "next_page_offset": None},
        }

    with pytest.raises(module.LinkageError, match="payload"):
        module.qdrant_read_live_point(request=wrong_request)

    def duplicate_request(
        method: str, path: str, body: object | None = None
    ) -> dict[str, object]:
        del path, body
        if method == "GET":
            return {"status": "ok", "result": copy.deepcopy(material["qdrant_point"])}
        return {
            "status": "ok",
            "result": {
                "points": [
                    copy.deepcopy(material["qdrant_point"]),
                    copy.deepcopy(material["qdrant_point"]),
                ],
                "next_page_offset": None,
            },
        }

    with pytest.raises(module.LinkageError, match="exactly one"):
        module.qdrant_read_live_point(request=duplicate_request)


def test_qdrant_redirect_handler_never_replays_api_key() -> None:
    module = _load_module()
    handler = module._RejectRedirectHandler()
    request = module.urllib.request.Request(
        "http://qdrant:6333/collections",
        headers={"api-key": "must-not-leave-origin"},
    )

    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(
            request,
            None,
            307,
            "redirect",
            {},
            "http://169.254.169.254/latest/meta-data",
        )


def test_write_fixture_cli_publishes_only_authorities_not_a_raw_point(
    tmp_path: Path,
) -> None:
    module = _load_module()
    authority_path = tmp_path / "authority.json"
    object_path = tmp_path / "object.json"

    assert (
        module.main(
            [
                "write-fixture",
                "--authority-output",
                os.fspath(authority_path),
                "--object-output",
                os.fspath(object_path),
            ]
        )
        == 0
    )
    material = _material(module)
    assert (
        json.loads(authority_path.read_text(encoding="utf-8"))
        == material["authority_record"]
    )
    assert object_path.read_bytes() == material["object_bytes"]
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "authority.json",
        "object.json",
    ]


def test_build_receipt_cli_round_trips_strict_public_receipt(tmp_path: Path) -> None:
    module = _load_module()
    proof = module.build_proof(**_material(module))
    source_path = tmp_path / "source-proof.json"
    observed_path = tmp_path / "observed-proof.json"
    receipt_path = tmp_path / "receipt.json"
    source_path.write_text(json.dumps(proof), encoding="utf-8")
    observed_path.write_text(json.dumps(proof), encoding="utf-8")

    assert (
        module.main(
            [
                "build-receipt",
                "--source-proof",
                os.fspath(source_path),
                "--observed-proof",
                os.fspath(observed_path),
                "--phase",
                "snapshot",
                "--challenge",
                "11" * 32,
                "--backup-id",
                "auris-flow-20260724T010203Z-a" + "1" * 11,
                "--backup-manifest-sha256",
                "2" * 64,
                "--source-commit",
                "3" * 40,
                "--release-tag",
                "v1.0.0-rc.1",
                "--release-metadata-sha256",
                "4" * 64,
                "--drill-project",
                "auris-flow-restore-drill-" + "5" * 12,
                "--output",
                os.fspath(receipt_path),
            ]
        )
        == 0
    )
    assert (
        module.validate_receipt(json.loads(receipt_path.read_text(encoding="utf-8")))[
            "phase"
        ]
        == "snapshot"
    )


def test_mysql_seed_and_export_sql_are_fixed_hex_not_interpolated_input() -> None:
    module = _load_module()

    seed = module.mysql_seed_sql().decode("ascii")
    export = module.mysql_export_sql().decode("ascii")

    assert "INSERT INTO auris_flow.json_resources" in seed
    assert "CAST(CONVERT(UNHEX(" in seed
    assert "SELECT JSON_OBJECT(" in export
    assert "FROM auris_flow.json_resources" in export
    assert "release-gate/" not in seed
    assert "release-recovery-linkage-v1" not in seed
    assert "\\!" not in seed
    assert "\nsource " not in seed.casefold()
    assert "\nsystem " not in seed.casefold()
    assert "--" not in seed
