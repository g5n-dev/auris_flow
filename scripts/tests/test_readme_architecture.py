from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_readme_architecture_assets_are_safe_and_structurally_aligned() -> None:
    verifier = _load_module(
        "auris_readme_architecture_test",
        ROOT / "scripts/verify_readme_architecture.py",
    )

    assert verifier.validate_sources() == []
    light_failures, light_shape = verifier.validate_svg(verifier.DEFAULT_LIGHT)
    dark_failures, dark_shape = verifier.validate_svg(verifier.DEFAULT_DARK)

    assert light_failures == []
    assert dark_failures == []
    assert light_shape == dark_shape
