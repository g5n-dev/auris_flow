from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _load_renderer() -> ModuleType:
    path = ROOT / "scripts" / "render_release_compose.py"
    spec = importlib.util.spec_from_file_location("auris_release_compose", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lock(renderer: ModuleType) -> dict[str, object]:
    return renderer.create_image_lock(
        release_tag="v1.0.0-rc.1",
        source_commit="1" * 40,
        images={
            "api": f"ghcr.io/auris/flow-api:v1.0.0-rc.1@sha256:{DIGEST_A}",
            "database": f"mysql:8.4.5@sha256:{DIGEST_B}",
        },
    )


def test_release_render_removes_build_and_embeds_exact_provenance() -> None:
    renderer = _load_renderer()
    image_lock = _lock(renderer)
    source = {
        "name": "auris-flow",
        "services": {
            "api": {"image": "auris-flow-api:dev", "build": {"context": "."}},
            "database": {"image": "mysql:8.4.5"},
        },
    }

    result = renderer.render_release_document(source, image_lock)

    assert "build" not in result["services"]["api"]
    assert result["services"]["api"]["image"].endswith(f"@sha256:{DIGEST_A}")
    assert result["services"]["database"]["image"].endswith(f"@sha256:{DIGEST_B}")
    assert result["x-auris-release"] == {
        "schema_version": "auris.release-image-lock.v1",
        "release_tag": "v1.0.0-rc.1",
        "source_commit": "1" * 40,
    }
    assert source["services"]["api"]["build"] == {"context": "."}


@pytest.mark.parametrize(
    "reference",
    [
        "ghcr.io/auris/flow-api:latest@sha256:" + DIGEST_A,
        "ghcr.io/auris/flow-api:v1.0.0",
        "ghcr.io/auris/flow-api@sha256:" + DIGEST_A,
        "${AURIS_BFF_IMAGE}@sha256:" + DIGEST_A,
        "ghcr.io/auris/flow-api:v1.0.0@sha256:" + "A" * 64,
    ],
)
def test_release_image_reference_policy_fails_closed(reference: str) -> None:
    renderer = _load_renderer()

    with pytest.raises(renderer.ReleaseComposeError):
        renderer.validate_image_reference(reference)


def test_release_render_rejects_missing_or_unexpected_service_lock() -> None:
    renderer = _load_renderer()
    image_lock = _lock(renderer)
    source = {"services": {"api": {"image": "api:dev"}}}

    with pytest.raises(renderer.ReleaseComposeError, match="does not match"):
        renderer.render_release_document(source, image_lock)


def test_scan_plan_covers_each_unique_digest_and_both_architectures() -> None:
    renderer = _load_renderer()
    image_lock = renderer.create_image_lock(
        release_tag="v1.0.0-rc.1",
        source_commit="1" * 40,
        images={
            "api": f"ghcr.io/auris/api:v1.0.0-rc.1@sha256:{DIGEST_A}",
            "worker": f"ghcr.io/auris/api:v1.0.0-rc.1@sha256:{DIGEST_A}",
            "database": f"mysql:8.4.5@sha256:{DIGEST_B}",
        },
    )

    plan = renderer.build_image_scan_plan(image_lock)

    assert plan["schema_version"] == "auris.release-image-scan-plan.v1"
    assert len(plan["images"]) == 2
    api = next(item for item in plan["images"] if item["digest"] == f"sha256:{DIGEST_A}")
    assert api["services"] == ["api", "worker"]
    assert api["platforms"] == ["linux/amd64", "linux/arm64"]


def test_artifact_manifest_binds_checksums_to_commit_and_tag(tmp_path: Path) -> None:
    renderer = _load_renderer()
    image_lock = _lock(renderer)
    (tmp_path / "sbom").mkdir()
    (tmp_path / "sbom" / "api.cdx.json").write_text("{}\n", encoding="utf-8")
    lock_path = tmp_path / "images.lock.json"
    lock_path.write_text(json.dumps(image_lock), encoding="utf-8")
    manifest_path = tmp_path / "release-manifest.json"
    checksums_path = tmp_path / "SHA256SUMS"

    manifest, checksums = renderer.build_artifact_manifest(
        base_dir=tmp_path,
        image_lock=image_lock,
        excluded=[manifest_path, checksums_path],
    )

    assert manifest["release_tag"] == "v1.0.0-rc.1"
    assert manifest["source_commit"] == "1" * 40
    assert [artifact["path"] for artifact in manifest["artifacts"]] == [
        "images.lock.json",
        "sbom/api.cdx.json",
    ]
    assert "images.lock.json" in checksums
    assert "sbom/api.cdx.json" in checksums


def test_artifact_manifest_can_limit_checksums_to_published_regular_assets(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer()
    image_lock = _lock(renderer)
    published = tmp_path / "auris-flow-v1.0.0-rc.1-deployment.tar.gz"
    published.write_bytes(b"published")
    (tmp_path / "internal-scan.json").write_text("{}\n", encoding="utf-8")

    manifest, checksums = renderer.build_artifact_manifest(
        base_dir=tmp_path,
        image_lock=image_lock,
        excluded=[],
        included=[published],
    )

    assert [artifact["path"] for artifact in manifest["artifacts"]] == [published.name]
    assert published.name in checksums
    assert "internal-scan.json" not in checksums


def test_cli_create_lock_is_deterministic(tmp_path: Path) -> None:
    renderer = _load_renderer()
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    common = [
        "create-lock",
        "--release-tag",
        "v1.0.0",
        "--source-commit",
        "2" * 40,
        "--image",
        f"database=mysql:8.4.5@sha256:{DIGEST_B}",
        "--image",
        f"api=ghcr.io/auris/api:v1.0.0@sha256:{DIGEST_A}",
    ]

    assert renderer.main([*common, "--output", str(first)]) == 0
    assert renderer.main([*common, "--output", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
