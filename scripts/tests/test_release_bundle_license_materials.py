from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[2]
LICENSE_MATERIALS = (
    "third_party/licenses/README.md",
    "third_party/licenses/antlr4-python3-runtime-4.13.2.LICENSE.txt",
    "third_party/licenses/python-dateutil-2.9.0.post0.LICENSE",
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_release_bundle_and_readiness_require_license_materials() -> None:
    release_bundle = _load_module(
        "auris_release_bundle_license_test",
        ROOT / "scripts/release_bundle.py",
    )
    readiness = _load_module(
        "auris_readiness_license_test",
        ROOT / "scripts/check_platform_readiness.py",
    )

    assert release_bundle.LICENSE_MATERIAL_FILES == LICENSE_MATERIALS
    assert set(LICENSE_MATERIALS).issubset(release_bundle.REQUIRED_BUNDLE_FILES)
    assert set(LICENSE_MATERIALS).issubset(readiness.RELEASE_REQUIRED_TRACKED_PATHS)
    assert "LICENSE_MATERIAL_FILES" in inspect.getsource(release_bundle.assemble_bundle)
    conclusions = json.loads(
        (ROOT / "config/release/exact-artifact-license-conclusions.json").read_text(
            encoding="utf-8"
        )
    )["conclusions"]
    assert {entry["license_text_path"] for entry in conclusions} == set(
        LICENSE_MATERIALS[1:]
    )


def test_preserved_license_material_hashes_are_exact() -> None:
    expected_hashes = {
        "third_party/licenses/antlr4-python3-runtime-4.13.2.LICENSE.txt": (
            "3db1fb3ee79a4b4f9918fc4d0f6133bf18a3cf787f126cd22f8aa9b862281c0c"
        ),
        "third_party/licenses/python-dateutil-2.9.0.post0.LICENSE": (
            "9313256b27c4a1b7666c433acbf70a447383df0ea8c4b59bee0b6e412a281f92"
        ),
    }

    assert {
        relative_path: hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest()
        for relative_path in expected_hashes
    } == expected_hashes
