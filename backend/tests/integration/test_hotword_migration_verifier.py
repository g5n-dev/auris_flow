from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.verify_migrations import assert_hotword_seed_references

SEED_FIXTURE = (
    Path(__file__).resolve().parents[3] / "doc" / "backend-spec" / "seed-fixture-v0.1.json"
)


def _write_seed(tmp_path: Path, seed: dict[str, object]) -> Path:
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return seed_path


def test_hotword_seed_references_are_complete() -> None:
    assert_hotword_seed_references(SEED_FIXTURE)


def test_hotword_seed_rejects_dangling_pack_current_version(tmp_path: Path) -> None:
    seed = json.loads(SEED_FIXTURE.read_text(encoding="utf-8"))
    seed["hotword_governance"]["hotword_packs"][0]["current_version_id"] = "missing-version"

    with pytest.raises(AssertionError, match="current_version_id.*missing-version"):
        assert_hotword_seed_references(_write_seed(tmp_path, seed))


def test_hotword_seed_rejects_cross_module_version_reference(tmp_path: Path) -> None:
    seed = json.loads(SEED_FIXTURE.read_text(encoding="utf-8"))
    seed["tasking"]["task_versions"][0]["hotword_pack_version_id"] = "missing-version"

    with pytest.raises(AssertionError, match="hotword_pack_version_id.*missing-version"):
        assert_hotword_seed_references(_write_seed(tmp_path, seed))


def test_hotword_seed_rejects_dangling_badcase_evidence_object(tmp_path: Path) -> None:
    seed = json.loads(SEED_FIXTURE.read_text(encoding="utf-8"))
    badcase = next(
        item
        for item in seed["review_and_feedback"]["badcases"]
        if item.get("badcase_id") == "A-4107"
    )
    badcase["evidence_storage_object_id"] = "missing-evidence-object"
    badcase["evidence_ref"] = "storage-object:missing-evidence-object"

    with pytest.raises(AssertionError, match="evidence_storage_object_id.*missing storage"):
        assert_hotword_seed_references(_write_seed(tmp_path, seed))
