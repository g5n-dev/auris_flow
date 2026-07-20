#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SCREENSHOT_COUNT = 76
EXPECTED_WIDTH = 1440
EXPECTED_HEIGHT = 900
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CAPTURE_CONTRACT = {
    "browser": "chromium",
    "color_scheme": "light",
    "device_scale_factor": 1,
    "fixed_time": "2025-05-26T12:27:18+08:00",
    "locale": "zh-CN",
    "reduced_motion": "reduce",
    "timezone": "Asia/Shanghai",
    "viewport": {"height": EXPECTED_HEIGHT, "width": EXPECTED_WIDTH},
}
REQUIRED_GEOMETRY_KEYS = {
    "shell",
    "sidebar",
    "topbar",
    "workbench",
    "tabs",
    "selectedTab",
}
RECTANGLE_FIELDS = {"x", "y", "width", "height"}
RUNTIME_CONTRACT_PATH = ROOT / "production/visual/runtime-contract.json"
VISUAL_LOCK_PATH = ROOT / "production/visual/visual-baseline.lock.json"
CANONICAL_SEED_OVERLAY_PATH = ROOT / "production/visual/seed-overlay.json"
ARTIFACT_PACKAGE_NAME = "visual-baseline.tar"
ARTIFACT_MEDIA_TYPE = "application/vnd.auris-flow.visual-baseline.v1+tar"
GITHUB_ACTIONS_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
OFFICIAL_VISUAL_REPOSITORY = ("auris-flow", "auris-flow")
OCI_ARTIFACT_PATTERN = re.compile(
    r"^ghcr\.io/(?P<owner>[a-z0-9._-]+)/(?P<repository>[a-z0-9._-]+)/"
    r"visual-baseline@sha256:[0-9a-f]{64}$"
)
VISUAL_BUILD_IDENTITY_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repository>[A-Za-z0-9_.-]+)/"
    r"\.github/workflows/visual-baseline-build\.yml@refs/heads/"
    r"[A-Za-z0-9._/-]+$"
)
COMMIT_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
MAX_ARTIFACT_MEMBERS = EXPECTED_SCREENSHOT_COUNT + 4
MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024
RUNNER_CONTRACT_INPUTS = (
    ".github/workflows/visual-baseline-build.yml",
    ".github/workflows/visual-baseline-promotion.yml",
    "production/visual/Dockerfile",
    "production/visual/runtime-contract.json",
    "production/visual/runtime.mjs",
    "prototype/auris-flow-ui/package.json",
    "prototype/auris-flow-ui/package-lock.json",
    "prototype/auris-flow-ui/audit/visual-regression.config.mjs",
    "prototype/auris-flow-ui/audit/visual-regression.spec.mjs",
    "scripts/promote_visual_baseline.sh",
    "scripts/verify_visual_baseline.py",
    "scripts/visual_regression.sh",
)
SCENARIO_CONTRACT_INPUTS = (
    "prototype/auris-flow-ui/audit/visual-regression.config.mjs",
    "prototype/auris-flow-ui/audit/visual-regression.spec.mjs",
)
VISUAL_SOURCE_GIT_PATHS = (
    "backend/app",
    "backend/data",
    "backend/migrations",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "production/visual/Dockerfile",
    "production/visual/runtime-contract.json",
    "production/visual/runtime.mjs",
    "production/visual/seed-overlay.json",
    ".github/workflows/visual-baseline-build.yml",
    ".github/workflows/visual-baseline-promotion.yml",
    "prototype/auris-flow-ui/audit/visual-regression.config.mjs",
    "prototype/auris-flow-ui/audit/visual-regression.spec.mjs",
    "prototype/auris-flow-ui/index.html",
    "prototype/auris-flow-ui/package.json",
    "prototype/auris-flow-ui/package-lock.json",
    "prototype/auris-flow-ui/scripts",
    "prototype/auris-flow-ui/src",
    "prototype/auris-flow-ui/vite.config.ts",
    "scripts/promote_visual_baseline.sh",
    "scripts/verify_visual_baseline.py",
    "scripts/visual_regression.sh",
)
RELEASE_RUNTIME_KIND = "pinned-playwright-container"
HOST_RUNTIME_KIND = "host-diagnostics"
RUNTIME_REQUIRED_STRING_KEYS = {
    "runtime_kind",
    "platform",
    "runner_image",
    "runner_contract_sha256",
    "playwright_version",
    "browser_name",
    "browser_version",
    "node_version",
    "os_release",
    "reproducibility_scope",
}


class BaselineValidationError(ValueError):
    """Raised when a baseline cannot be safely generated or updated."""


@dataclass(frozen=True)
class BaselineFile:
    path: str
    sha256: str
    size: int
    width: int | None = None
    height: int | None = None

    def as_manifest_entry(self) -> dict[str, object]:
        entry: dict[str, object] = {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
        }
        if self.width is not None and self.height is not None:
            entry["png"] = {"height": self.height, "width": self.width}
        return entry


@dataclass(frozen=True)
class BaselineInventory:
    files: tuple[BaselineFile, ...]
    failures: tuple[str, ...]


@dataclass(frozen=True)
class VisualExecutionPolicy:
    visual_dir: Path
    runtime: str
    update: bool


def _load_json(path: Path, label: str, failures: list[str]) -> Any | None:
    if not path.is_file() or path.is_symlink():
        failures.append(f"{label} must be a regular file: {path}")
        return None

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    try:
        return json.loads(
            path.read_text(encoding="utf-8"), parse_constant=reject_constant
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        failures.append(f"{label} is not valid strict JSON: {error}")
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _png_dimensions(path: Path, failures: list[str]) -> tuple[int, int] | None:
    try:
        header = path.read_bytes()[:24]
    except OSError as error:
        failures.append(f"unable to read PNG {path}: {error}")
        return None
    if len(header) < 24 or header[:8] != PNG_SIGNATURE:
        failures.append(f"PNG signature is invalid: {path}")
        return None
    if header[12:16] != b"IHDR" or struct.unpack(">I", header[8:12])[0] != 13:
        failures.append(f"PNG IHDR header is invalid: {path}")
        return None
    return struct.unpack(">II", header[16:24])


def _geometry_screenshot_name(geometry_name: str) -> str:
    name = geometry_name.replace("__", "-", 1)
    return re.sub(r"\s+", "-", name)


def _validate_geometry(geometry: Any, failures: list[str]) -> set[str]:
    if not isinstance(geometry, dict):
        failures.append("geometry.json root must be an object")
        return set()
    if len(geometry) != EXPECTED_SCREENSHOT_COUNT:
        failures.append(
            "geometry.json must contain "
            f"{EXPECTED_SCREENSHOT_COUNT} snapshots; found {len(geometry)}"
        )

    screenshot_names: set[str] = set()
    for snapshot_name, boxes in geometry.items():
        if not isinstance(snapshot_name, str) or not snapshot_name.endswith(".png"):
            failures.append(
                f"geometry snapshot key must end with .png: {snapshot_name!r}"
            )
            continue
        screenshot_names.add(_geometry_screenshot_name(snapshot_name))
        if not isinstance(boxes, dict):
            failures.append(f"geometry snapshot must be an object: {snapshot_name}")
            continue
        missing_keys = sorted(REQUIRED_GEOMETRY_KEYS - boxes.keys())
        if missing_keys:
            failures.append(
                f"geometry snapshot {snapshot_name} missing keys: {', '.join(missing_keys)}"
            )
        for box_name, rectangle in boxes.items():
            if not isinstance(rectangle, dict) or set(rectangle) != RECTANGLE_FIELDS:
                failures.append(
                    f"geometry rectangle {snapshot_name}/{box_name} must contain "
                    "x, y, width and height"
                )
                continue
            invalid_fields = [
                field
                for field, value in rectangle.items()
                if isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ]
            if invalid_fields:
                failures.append(
                    f"geometry rectangle {snapshot_name}/{box_name} has invalid numeric "
                    f"fields: {', '.join(sorted(invalid_fields))}"
                )
                continue
            if rectangle["width"] <= 0 or rectangle["height"] <= 0:
                failures.append(
                    f"geometry rectangle {snapshot_name}/{box_name} must have positive size"
                )
    return screenshot_names


def _validate_seed_overlay(seed_overlay: Any, failures: list[str]) -> None:
    if not isinstance(seed_overlay, dict):
        failures.append("seed-overlay.json root must be an object")
        return
    versions = seed_overlay.get("scene_profile_versions")
    if not isinstance(versions, list) or not versions:
        failures.append("seed-overlay.json must contain scene_profile_versions")
        return
    version_ids: set[str] = set()
    for index, version in enumerate(versions):
        if not isinstance(version, dict):
            failures.append(f"seed overlay entry {index} must be an object")
            continue
        version_id = version.get("scene_profile_version_id")
        if not isinstance(version_id, str) or not version_id:
            failures.append(
                f"seed overlay entry {index} has no scene_profile_version_id"
            )
        elif version_id in version_ids:
            failures.append(f"seed overlay has duplicate version id: {version_id}")
        else:
            version_ids.add(version_id)
        if not isinstance(version.get("manifest"), dict):
            failures.append(f"seed overlay entry {index} manifest must be an object")
        expected_hash = version.get("expected_manifest_sha256")
        if not isinstance(expected_hash, str) or not SHA256_PATTERN.fullmatch(
            expected_hash
        ):
            failures.append(
                f"seed overlay entry {index} expected_manifest_sha256 is invalid"
            )


def _inspect_baseline(
    baseline_dir: Path, *, validate_dimensions: bool = True
) -> BaselineInventory:
    failures: list[str] = []
    baseline_dir = baseline_dir.resolve()
    geometry_path = baseline_dir / "geometry.json"
    seed_overlay_path = baseline_dir / "seed-overlay.json"
    screenshots_dir = baseline_dir / "screenshots"

    geometry = _load_json(geometry_path, "geometry.json", failures)
    geometry_screenshots = _validate_geometry(geometry, failures)
    seed_overlay = _load_json(seed_overlay_path, "seed-overlay.json", failures)
    _validate_seed_overlay(seed_overlay, failures)

    screenshot_paths: list[Path] = []
    if not screenshots_dir.is_dir() or screenshots_dir.is_symlink():
        failures.append(f"screenshots must be a regular directory: {screenshots_dir}")
    else:
        entries = sorted(screenshots_dir.iterdir(), key=lambda path: path.name)
        unexpected = [path.name for path in entries if path.suffix.lower() != ".png"]
        if unexpected:
            failures.append(
                "screenshots directory contains non-PNG entries: "
                + ", ".join(unexpected)
            )
        screenshot_paths = [path for path in entries if path.suffix.lower() == ".png"]
    if len(screenshot_paths) != EXPECTED_SCREENSHOT_COUNT:
        failures.append(
            "screenshots directory must contain "
            f"{EXPECTED_SCREENSHOT_COUNT} PNGs; found {len(screenshot_paths)}"
        )

    screenshot_names = {path.name for path in screenshot_paths}
    if geometry_screenshots != screenshot_names:
        missing = sorted(geometry_screenshots - screenshot_names)
        extra = sorted(screenshot_names - geometry_screenshots)
        failures.append(
            "geometry/screenshot inventory mismatch; "
            f"missing screenshots={missing}, extra screenshots={extra}"
        )

    files: list[BaselineFile] = []
    for path, relative_path in (
        (geometry_path, "geometry.json"),
        (seed_overlay_path, "seed-overlay.json"),
    ):
        if path.is_file() and not path.is_symlink():
            files.append(
                BaselineFile(relative_path, _sha256(path), path.stat().st_size)
            )

    for screenshot_path in screenshot_paths:
        relative_path = f"screenshots/{screenshot_path.name}"
        if not screenshot_path.is_file() or screenshot_path.is_symlink():
            failures.append(f"screenshot must be a regular file: {relative_path}")
            continue
        dimensions = _png_dimensions(screenshot_path, failures)
        width = dimensions[0] if dimensions else None
        height = dimensions[1] if dimensions else None
        if (
            validate_dimensions
            and dimensions is not None
            and dimensions != (EXPECTED_WIDTH, EXPECTED_HEIGHT)
        ):
            failures.append(
                f"PNG {relative_path} must be {EXPECTED_WIDTH}x{EXPECTED_HEIGHT}; "
                f"found {width}x{height}"
            )
        files.append(
            BaselineFile(
                relative_path,
                _sha256(screenshot_path),
                screenshot_path.stat().st_size,
                width,
                height,
            )
        )
    return BaselineInventory(
        tuple(sorted(files, key=lambda item: item.path)), tuple(failures)
    )


def _content_digest(files: tuple[BaselineFile, ...]) -> str:
    payload = "".join(
        f"{item.path}\0{item.sha256}\0{item.size}\n" for item in files
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _contract_sha256(relative_paths: tuple[str, ...], label: str) -> str:
    files: list[BaselineFile] = []
    for relative_path in relative_paths:
        path = ROOT / relative_path
        if not path.is_file() or path.is_symlink():
            raise BaselineValidationError(
                f"{label} input must be a regular file: {relative_path}"
            )
        files.append(BaselineFile(relative_path, _sha256(path), path.stat().st_size))
    return _content_digest(tuple(files))


def scenario_contract_sha256() -> str:
    return _contract_sha256(SCENARIO_CONTRACT_INPUTS, "visual scenario contract")


def _current_source_commit() -> str:
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    commit = completed.stdout.strip().lower()
    if not COMMIT_PATTERN.fullmatch(commit):
        raise BaselineValidationError("unable to resolve an exact Git source commit")
    return commit


def playwright_lock_version() -> str:
    lock_path = ROOT / "prototype/auris-flow-ui/package-lock.json"
    lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
    version = (
        lock_data.get("packages", {}).get("node_modules/playwright", {}).get("version")
    )
    if not isinstance(version, str) or not version:
        raise BaselineValidationError(
            f"unable to resolve pinned Playwright version from {lock_path}"
        )
    return version


def _playwright_version() -> str:
    """Backward-compatible internal alias for older callers."""

    return playwright_lock_version()


def release_runtime_contract() -> dict[str, str]:
    failures: list[str] = []
    payload = _load_json(RUNTIME_CONTRACT_PATH, "visual runtime contract", failures)
    if failures or not isinstance(payload, dict):
        raise BaselineValidationError(
            "\n".join(failures) or "visual runtime contract is invalid"
        )
    required = {
        "runtime_kind",
        "platform",
        "runner_image",
        "reproducibility_scope",
    }
    if set(payload) != required:
        raise BaselineValidationError(
            "visual runtime contract must contain exactly: "
            + ", ".join(sorted(required))
        )
    if any(
        not isinstance(payload.get(key), str) or not payload[key] for key in required
    ):
        raise BaselineValidationError(
            "visual runtime contract fields must be non-empty strings"
        )
    if payload["runtime_kind"] != RELEASE_RUNTIME_KIND:
        raise BaselineValidationError(
            "visual runtime contract kind is not release-safe"
        )
    if payload["platform"] != "linux/amd64":
        raise BaselineValidationError(
            "visual release runtime must be pinned to linux/amd64"
        )
    image = payload["runner_image"]
    if not re.fullmatch(r"mcr\.microsoft\.com/playwright@sha256:[0-9a-f]{64}", image):
        raise BaselineValidationError(
            "visual runner image must use an exact MCR sha256 digest"
        )
    return {key: str(value) for key, value in payload.items()}


def runner_contract_sha256() -> str:
    return _contract_sha256(RUNNER_CONTRACT_INPUTS, "visual runner contract")


def _release_runtime_failures(environment: Any) -> list[str]:
    failures: list[str] = []
    if not isinstance(environment, dict):
        return ["reference_environment must be an object"]
    if environment.get("runtime_kind") != RELEASE_RUNTIME_KIND:
        failures.append(
            "release visual baseline requires the pinned Playwright container runtime"
        )
    missing = sorted(
        key
        for key in RUNTIME_REQUIRED_STRING_KEYS
        if not isinstance(environment.get(key), str) or not environment[key]
    )
    if missing:
        failures.append(
            "reference_environment missing release runtime fields: "
            + ", ".join(missing)
        )
        return failures
    try:
        contract = release_runtime_contract()
        contract_sha256 = runner_contract_sha256()
        locked_playwright = playwright_lock_version()
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        BaselineValidationError,
    ) as error:
        return [f"unable to validate visual release runtime contract: {error}"]
    expected = {
        "runtime_kind": RELEASE_RUNTIME_KIND,
        "platform": contract["platform"],
        "runner_image": contract["runner_image"],
        "runner_contract_sha256": contract_sha256,
        "playwright_version": locked_playwright,
        "browser_name": "chromium",
        "reproducibility_scope": contract["reproducibility_scope"],
    }
    for key, value in expected.items():
        if environment.get(key) != value:
            failures.append(
                f"reference_environment.{key} does not match pinned release runtime"
            )
    return failures


def resolve_visual_execution_policy(
    *,
    release_check: bool,
    update: bool,
    default_goal_dir: Path,
    frozen_root: Path,
    diagnostics_root: Path,
    goal_dir: Path | None = None,
    seed_overlay: Path | None = None,
    runtime: str | None = None,
) -> VisualExecutionPolicy:
    selected_runtime = runtime or "container"
    if selected_runtime not in {"container", "host"}:
        raise BaselineValidationError("visual runtime must be 'container' or 'host'")

    default_goal_dir = default_goal_dir.resolve()
    frozen_root = frozen_root.resolve()
    diagnostics_root = diagnostics_root.resolve()
    if release_check and update:
        raise BaselineValidationError(
            "strict release verification forbids visual baseline update mode"
        )

    if not update:
        if goal_dir is not None:
            raise BaselineValidationError(
                "frozen visual verification forbids AURIS_VISUAL_GOAL_DIR"
            )
        if seed_overlay is not None:
            raise BaselineValidationError(
                "frozen visual verification forbids AURIS_VISUAL_SEED_OVERLAY"
            )
        if selected_runtime != "container":
            raise BaselineValidationError(
                "frozen visual verification requires the pinned Playwright container"
            )
        return VisualExecutionPolicy(
            visual_dir=(default_goal_dir / "visual-regression").resolve(),
            runtime="container",
            update=False,
        )

    if goal_dir is None:
        raise BaselineValidationError(
            "visual candidate update requires an explicit AURIS_VISUAL_GOAL_DIR"
        )
    resolved_goal = goal_dir.resolve()
    if resolved_goal == diagnostics_root or not resolved_goal.is_relative_to(
        diagnostics_root
    ):
        raise BaselineValidationError(
            "visual candidate updates are limited to a named directory under "
            f"{diagnostics_root}: {resolved_goal}"
        )
    visual_dir = (resolved_goal / "visual-regression").resolve()
    assert_update_target_safe(visual_dir, frozen_root)
    return VisualExecutionPolicy(
        visual_dir=visual_dir,
        runtime=selected_runtime,
        update=True,
    )


def write_manifest(
    baseline_dir: Path,
    *,
    reference_platform: str | None = None,
    playwright_version: str | None = None,
    runtime_descriptor: dict[str, Any] | None = None,
    validate_dimensions: bool = True,
    source_commit: str | None = None,
) -> dict[str, object]:
    baseline_dir = baseline_dir.resolve()
    inventory = _inspect_baseline(baseline_dir, validate_dimensions=validate_dimensions)
    if inventory.failures:
        raise BaselineValidationError("\n".join(inventory.failures))
    if runtime_descriptor is not None and (
        reference_platform is not None or playwright_version is not None
    ):
        raise BaselineValidationError(
            "runtime_descriptor cannot be combined with host runtime overrides"
        )
    if runtime_descriptor is None:
        reference_environment: dict[str, object] = {
            "runtime_kind": HOST_RUNTIME_KIND,
            "platform": reference_platform
            or f"{sys.platform}-{platform.machine().lower()}",
            "playwright_version": playwright_version or playwright_lock_version(),
            "reproducibility_scope": "host diagnostics only; never release evidence",
        }
    else:
        reference_environment = dict(runtime_descriptor)
        runtime_failures = _release_runtime_failures(reference_environment)
        if runtime_failures:
            raise BaselineValidationError("\n".join(runtime_failures))

    locked_playwright = playwright_lock_version()
    if reference_environment.get("playwright_version") != locked_playwright:
        raise BaselineValidationError(
            "visual manifest Playwright version must equal package-lock: "
            f"{locked_playwright}"
        )

    resolved_source_commit = (source_commit or _current_source_commit()).lower()
    if not COMMIT_PATTERN.fullmatch(resolved_source_commit):
        raise BaselineValidationError(
            "visual manifest source_commit must be an exact Git commit id"
        )

    manifest: dict[str, object] = {
        "baseline_sha256": _content_digest(inventory.files),
        "capture_contract": CAPTURE_CONTRACT,
        "files": [item.as_manifest_entry() for item in inventory.files],
        "kind": "auris-flow-visual-regression-baseline",
        "reference_environment": reference_environment,
        "runner_contract_sha256": runner_contract_sha256(),
        "scenario_contract_sha256": scenario_contract_sha256(),
        "schema_version": 2,
        "seed_overlay_sha256": _sha256(baseline_dir / "seed-overlay.json"),
        "screenshot_count": EXPECTED_SCREENSHOT_COUNT,
        "source_commit": resolved_source_commit,
    }
    manifest_path = baseline_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _safe_manifest_file_entries(
    manifest: Any, baseline_dir: Path, failures: list[str]
) -> dict[str, dict[str, object]]:
    if not isinstance(manifest, dict):
        return {}
    entries = manifest.get("files")
    if not isinstance(entries, list):
        failures.append("manifest.json files must be an array")
        return {}
    result: dict[str, dict[str, object]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            failures.append(f"manifest file entry {index} must be an object")
            continue
        relative_path = entry.get("path")
        if not isinstance(relative_path, str) or not relative_path:
            failures.append(f"manifest file entry {index} has invalid path")
            continue
        candidate = Path(relative_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            failures.append(f"manifest file path escapes baseline: {relative_path}")
            continue
        resolved = (baseline_dir / candidate).resolve()
        if not resolved.is_relative_to(baseline_dir):
            failures.append(f"manifest file path escapes baseline: {relative_path}")
            continue
        if relative_path in result:
            failures.append(f"manifest has duplicate file path: {relative_path}")
            continue
        result[relative_path] = entry
    return result


def validate_baseline(
    baseline_dir: Path,
    *,
    runtime_descriptor: dict[str, Any] | None = None,
    require_release_runtime: bool = False,
) -> list[str]:
    baseline_dir = baseline_dir.resolve()
    inventory = _inspect_baseline(baseline_dir)
    failures = list(inventory.failures)
    manifest = _load_json(baseline_dir / "manifest.json", "manifest.json", failures)
    if not isinstance(manifest, dict):
        return failures

    for key, expected in (
        ("schema_version", 2),
        ("kind", "auris-flow-visual-regression-baseline"),
        ("screenshot_count", EXPECTED_SCREENSHOT_COUNT),
        ("capture_contract", CAPTURE_CONTRACT),
    ):
        if manifest.get(key) != expected:
            failures.append(f"manifest.json {key} does not match the capture contract")

    source_commit = manifest.get("source_commit")
    if not isinstance(source_commit, str) or not COMMIT_PATTERN.fullmatch(
        source_commit
    ):
        failures.append("manifest.json source_commit is not an exact Git commit id")
    try:
        expected_runner_contract = runner_contract_sha256()
        expected_scenario_contract = scenario_contract_sha256()
    except (OSError, BaselineValidationError) as error:
        failures.append(f"unable to validate visual source contracts: {error}")
    else:
        if manifest.get("runner_contract_sha256") != expected_runner_contract:
            failures.append("manifest.json runner_contract_sha256 drifted")
        if manifest.get("scenario_contract_sha256") != expected_scenario_contract:
            failures.append("manifest.json scenario_contract_sha256 drifted")
    seed_overlay_path = baseline_dir / "seed-overlay.json"
    if seed_overlay_path.is_file() and not seed_overlay_path.is_symlink():
        if manifest.get("seed_overlay_sha256") != _sha256(seed_overlay_path):
            failures.append("manifest.json seed_overlay_sha256 mismatch")

    environment = manifest.get("reference_environment")
    if not isinstance(environment, dict):
        failures.append("manifest.json reference_environment must be an object")
    else:
        for key in ("platform", "playwright_version", "reproducibility_scope"):
            if not isinstance(environment.get(key), str) or not environment[key]:
                failures.append(f"manifest.json reference_environment.{key} is missing")
        try:
            locked_playwright = playwright_lock_version()
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            failures.append(f"unable to read package-lock Playwright version: {error}")
        else:
            if environment.get("playwright_version") != locked_playwright:
                failures.append(
                    "manifest.json Playwright version does not match package-lock"
                )
        if (
            require_release_runtime
            or environment.get("runtime_kind") == RELEASE_RUNTIME_KIND
        ):
            failures.extend(_release_runtime_failures(environment))
        if runtime_descriptor is not None and environment != runtime_descriptor:
            failures.append(
                "manifest.json reference_environment does not match runtime descriptor"
            )

    manifest_files = _safe_manifest_file_entries(manifest, baseline_dir, failures)
    actual_files = {item.path: item.as_manifest_entry() for item in inventory.files}
    if set(manifest_files) != set(actual_files):
        failures.append(
            "manifest/baseline inventory mismatch; "
            f"missing={sorted(set(actual_files) - set(manifest_files))}, "
            f"extra={sorted(set(manifest_files) - set(actual_files))}"
        )
    for relative_path in sorted(set(manifest_files) & set(actual_files)):
        expected_entry = manifest_files[relative_path]
        actual_entry = actual_files[relative_path]
        if expected_entry.get("sha256") != actual_entry["sha256"]:
            failures.append(f"sha256 mismatch: {relative_path}")
        if expected_entry.get("size") != actual_entry["size"]:
            failures.append(f"size mismatch: {relative_path}")
        if expected_entry.get("png") != actual_entry.get("png"):
            failures.append(f"PNG dimensions mismatch in manifest: {relative_path}")

    content_digest = _content_digest(inventory.files)
    if manifest.get("baseline_sha256") != content_digest:
        failures.append("manifest.json baseline_sha256 mismatch")
    expected_root_entries = {
        "geometry.json",
        "manifest.json",
        "screenshots",
        "seed-overlay.json",
    }
    if baseline_dir.is_dir() and not baseline_dir.is_symlink():
        actual_root_entries = {entry.name for entry in baseline_dir.iterdir()}
        if actual_root_entries != expected_root_entries:
            failures.append(
                "baseline directory contains unexpected entries; "
                f"missing={sorted(expected_root_entries - actual_root_entries)}, "
                f"extra={sorted(actual_root_entries - expected_root_entries)}"
            )
    return failures


def _approved_artifact_failures(artifact: Any) -> list[str]:
    failures: list[str] = []
    if not isinstance(artifact, dict):
        return ["approved visual baseline lock artifact must be an object"]
    required_keys = {
        "approval_reference",
        "baseline_sha256",
        "job_workflow_sha",
        "manifest_sha256",
        "media_type",
        "package_sha256",
        "reference",
        "runner_contract_sha256",
        "scenario_contract_sha256",
        "scenario_count",
        "seed_overlay_sha256",
        "signature_identity",
        "signature_issuer",
        "source_commit",
    }
    if set(artifact) != required_keys:
        failures.append(
            "approved visual baseline lock artifact must contain exactly: "
            + ", ".join(sorted(required_keys))
        )
        return failures
    reference = artifact.get("reference")
    artifact_match = (
        OCI_ARTIFACT_PATTERN.fullmatch(reference)
        if isinstance(reference, str)
        else None
    )
    if artifact_match is None:
        failures.append(
            "visual baseline artifact reference must be an immutable "
            "ghcr.io/<owner>/<repo>/visual-baseline@sha256 digest"
        )
    elif (
        artifact_match.group("owner").lower(),
        artifact_match.group("repository").lower(),
    ) != OFFICIAL_VISUAL_REPOSITORY:
        failures.append(
            "visual baseline artifact reference must belong to the "
            "official Auris Flow repository"
        )
    if artifact.get("media_type") != ARTIFACT_MEDIA_TYPE:
        failures.append("visual baseline artifact media_type is not supported")
    for key in (
        "baseline_sha256",
        "manifest_sha256",
        "package_sha256",
        "runner_contract_sha256",
        "scenario_contract_sha256",
        "seed_overlay_sha256",
    ):
        value = artifact.get(key)
        if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
            failures.append(f"visual baseline lock artifact.{key} is not a sha256")
    source_commit = artifact.get("source_commit")
    if not isinstance(source_commit, str) or not COMMIT_PATTERN.fullmatch(
        source_commit
    ):
        failures.append(
            "visual baseline lock artifact.source_commit is not an exact Git commit"
        )
    job_workflow_sha = artifact.get("job_workflow_sha")
    if not isinstance(job_workflow_sha, str) or not COMMIT_PATTERN.fullmatch(
        job_workflow_sha
    ):
        failures.append(
            "visual baseline lock artifact.job_workflow_sha is not an exact Git commit"
        )
    elif job_workflow_sha != source_commit:
        failures.append(
            "visual baseline lock artifact.job_workflow_sha must equal source_commit"
        )
    if artifact.get("scenario_count") != EXPECTED_SCREENSHOT_COUNT:
        failures.append(
            "visual baseline lock artifact.scenario_count must be "
            f"{EXPECTED_SCREENSHOT_COUNT}"
        )
    approval_reference = artifact.get("approval_reference")
    if (
        not isinstance(approval_reference, str)
        or not approval_reference.strip()
        or approval_reference.strip().upper() in {"PENDING", "TBD", "TODO"}
    ):
        failures.append(
            "visual baseline lock artifact.approval_reference is still a placeholder"
        )
    signature_identity = artifact.get("signature_identity")
    identity_match = (
        VISUAL_BUILD_IDENTITY_PATTERN.fullmatch(signature_identity)
        if isinstance(signature_identity, str)
        and ".." not in signature_identity
        and "//" not in signature_identity.removeprefix("https://")
        and not signature_identity.endswith("/")
        else None
    )
    if identity_match is None:
        failures.append(
            "visual baseline lock artifact.signature_identity must be the exact "
            "visual-baseline-build.yml identity on a GitHub branch"
        )
    if artifact_match is not None and identity_match is not None:
        artifact_repository = (
            artifact_match.group("owner").lower(),
            artifact_match.group("repository").lower(),
        )
        signer_repository = (
            identity_match.group("owner").lower(),
            identity_match.group("repository").lower(),
        )
        if artifact_repository != signer_repository:
            failures.append(
                "visual baseline artifact and signature identity must belong to "
                "the same GitHub repository"
            )
        if signer_repository != OFFICIAL_VISUAL_REPOSITORY:
            failures.append(
                "visual baseline signature identity must belong to the "
                "official Auris Flow repository"
            )
    if artifact.get("signature_issuer") != GITHUB_ACTIONS_OIDC_ISSUER:
        failures.append(
            "visual baseline lock artifact.signature_issuer must be the GitHub "
            "Actions token issuer"
        )
    return failures


def _load_visual_lock(lock_path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    failures: list[str] = []
    payload = _load_json(lock_path, "visual baseline lock", failures)
    if not isinstance(payload, dict):
        return None, failures
    required_keys = {"artifact", "kind", "reason", "schema_version", "status"}
    if set(payload) != required_keys:
        failures.append(
            "visual baseline lock must contain exactly: "
            + ", ".join(sorted(required_keys))
        )
        return payload, failures
    if payload.get("schema_version") != 1:
        failures.append("visual baseline lock schema_version must be 1")
    if payload.get("kind") != "auris-flow-visual-baseline-lock":
        failures.append("visual baseline lock kind is invalid")
    status = payload.get("status")
    if status not in {"PENDING", "APPROVED"}:
        failures.append("visual baseline lock status must be PENDING or APPROVED")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        failures.append("visual baseline lock reason must be a non-empty string")
    if status == "PENDING" and payload.get("artifact") is not None:
        failures.append("PENDING visual baseline lock must not contain an artifact")
    if status == "APPROVED":
        failures.extend(_approved_artifact_failures(payload.get("artifact")))
    return payload, failures


def validate_visual_baseline_lock(
    lock_path: Path = VISUAL_LOCK_PATH,
    *,
    require_approved: bool = False,
    canonical_seed_path: Path = CANONICAL_SEED_OVERLAY_PATH,
) -> list[str]:
    payload, failures = _load_visual_lock(lock_path)
    if payload is None:
        return failures
    if require_approved and payload.get("status") != "APPROVED":
        failures.append(
            "visual baseline lock status is PENDING; no approved linux/amd64 OCI "
            "artifact is available"
        )
        return failures
    if payload.get("status") != "APPROVED":
        return failures
    artifact = payload.get("artifact")
    if not isinstance(artifact, dict):
        return failures
    try:
        expected_runner = runner_contract_sha256()
        expected_scenarios = scenario_contract_sha256()
    except (OSError, BaselineValidationError) as error:
        failures.append(f"unable to validate visual lock contracts: {error}")
    else:
        if artifact.get("runner_contract_sha256") != expected_runner:
            failures.append("visual baseline lock runner contract drifted")
        if artifact.get("scenario_contract_sha256") != expected_scenarios:
            failures.append("visual baseline lock scenario contract drifted")
    if not canonical_seed_path.is_file() or canonical_seed_path.is_symlink():
        failures.append(
            f"canonical visual seed overlay must be a regular file: {canonical_seed_path}"
        )
    elif artifact.get("seed_overlay_sha256") != _sha256(canonical_seed_path):
        failures.append("visual baseline lock canonical seed overlay drifted")
    return failures


def _manifest_for_lock(baseline_dir: Path) -> dict[str, Any]:
    failures = validate_baseline(baseline_dir, require_release_runtime=True)
    if failures:
        raise BaselineValidationError("\n".join(failures))
    manifest_failures: list[str] = []
    manifest = _load_json(
        baseline_dir / "manifest.json", "visual baseline manifest", manifest_failures
    )
    if manifest_failures or not isinstance(manifest, dict):
        raise BaselineValidationError(
            "\n".join(manifest_failures) or "visual baseline manifest is invalid"
        )
    return manifest


def create_artifact_package(baseline_dir: Path, package_path: Path) -> Path:
    baseline_dir = baseline_dir.resolve()
    manifest = _manifest_for_lock(baseline_dir)
    manifest_entries = _safe_manifest_file_entries(manifest, baseline_dir, [])
    relative_paths = ("manifest.json", *sorted(manifest_entries))
    package_path = package_path.resolve()
    if package_path.exists() and package_path.is_symlink():
        raise BaselineValidationError("visual artifact package must not be a symlink")
    package_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(package_path, "w", format=tarfile.USTAR_FORMAT) as archive:
        for relative_path in relative_paths:
            source = baseline_dir / relative_path
            if not source.is_file() or source.is_symlink():
                raise BaselineValidationError(
                    f"visual artifact member must be a regular file: {relative_path}"
                )
            info = archive.gettarinfo(str(source), arcname=relative_path)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            info.mode = 0o644
            with source.open("rb") as handle:
                archive.addfile(info, handle)
    return package_path


def write_visual_baseline_lock(
    lock_path: Path,
    *,
    baseline_dir: Path,
    package_path: Path,
    artifact_ref: str,
    approval_reference: str,
    signature_identity: str,
    signature_issuer: str,
) -> dict[str, Any]:
    if not OCI_ARTIFACT_PATTERN.fullmatch(artifact_ref):
        raise BaselineValidationError(
            "visual baseline artifact reference must be an immutable "
            "ghcr.io/<owner>/<repo>/visual-baseline@sha256 digest"
        )
    manifest = _manifest_for_lock(baseline_dir)
    package_path = package_path.resolve()
    if not package_path.is_file() or package_path.is_symlink():
        raise BaselineValidationError(
            f"visual artifact package must be a regular file: {package_path}"
        )
    payload: dict[str, Any] = {
        "artifact": {
            "approval_reference": approval_reference.strip(),
            "baseline_sha256": manifest["baseline_sha256"],
            "job_workflow_sha": manifest["source_commit"],
            "manifest_sha256": _sha256(baseline_dir / "manifest.json"),
            "media_type": ARTIFACT_MEDIA_TYPE,
            "package_sha256": _sha256(package_path),
            "reference": artifact_ref,
            "runner_contract_sha256": manifest["runner_contract_sha256"],
            "scenario_contract_sha256": manifest["scenario_contract_sha256"],
            "scenario_count": manifest["screenshot_count"],
            "seed_overlay_sha256": manifest["seed_overlay_sha256"],
            "signature_identity": signature_identity,
            "signature_issuer": signature_issuer,
            "source_commit": manifest["source_commit"],
        },
        "kind": "auris-flow-visual-baseline-lock",
        "reason": "Approved immutable visual baseline; update only through promotion.",
        "schema_version": 1,
        "status": "APPROVED",
    }
    failures = _approved_artifact_failures(payload["artifact"])
    if failures:
        raise BaselineValidationError("\n".join(failures))
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise BaselineValidationError(
            "visual baseline lock output must be an existing regular file or a new path"
        )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _extract_artifact_package(package_path: Path, destination: Path) -> None:
    destination = destination.resolve()
    if destination.exists() and (
        not destination.is_dir()
        or destination.is_symlink()
        or any(destination.iterdir())
    ):
        raise BaselineValidationError(
            f"visual artifact destination must be a new empty directory: {destination}"
        )
    destination.mkdir(parents=True, exist_ok=True)
    try:
        archive = tarfile.open(package_path, "r:")
    except (OSError, tarfile.TarError) as error:
        raise BaselineValidationError(
            f"visual artifact package is invalid: {error}"
        ) from error
    with archive:
        members = archive.getmembers()
        if len(members) > MAX_ARTIFACT_MEMBERS:
            raise BaselineValidationError(
                "visual artifact package has too many members"
            )
        total_size = sum(member.size for member in members)
        if total_size > MAX_ARTIFACT_BYTES:
            raise BaselineValidationError(
                "visual artifact package exceeds the size limit"
            )
        seen: set[str] = set()
        for member in members:
            relative_path = Path(member.name)
            if (
                not member.isreg()
                or relative_path.is_absolute()
                or ".." in relative_path.parts
                or member.name in seen
            ):
                raise BaselineValidationError(
                    f"unsafe visual artifact member: {member.name}"
                )
            seen.add(member.name)
            target = (destination / relative_path).resolve()
            if not target.is_relative_to(destination):
                raise BaselineValidationError(
                    f"visual artifact member escapes destination: {member.name}"
                )
        for member in members:
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise BaselineValidationError(
                    f"unable to read visual artifact member: {member.name}"
                )
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def _verify_repository_binding(source_commit: str) -> str:
    release_commit = _current_source_commit()
    try:
        ancestor = subprocess.run(
            ("git", "merge-base", "--is-ancestor", source_commit, release_commit),
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        changed = subprocess.run(
            (
                "git",
                "diff",
                "--name-only",
                source_commit,
                release_commit,
                "--",
                *VISUAL_SOURCE_GIT_PATHS,
            ),
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise BaselineValidationError(
            f"unable to verify visual artifact source commit: {error}"
        ) from error
    if ancestor.returncode != 0:
        raise BaselineValidationError(
            "visual artifact source commit is not an ancestor of the release commit"
        )
    changed_paths = {line for line in changed.stdout.splitlines() if line}
    if changed_paths:
        raise BaselineValidationError(
            "visual inputs changed after the baseline artifact source commit: "
            f"{sorted(changed_paths)}"
        )
    return release_commit


def _verify_materialized_against_lock(
    baseline_dir: Path,
    artifact: dict[str, Any],
    *,
    canonical_seed_path: Path,
    runtime_descriptor: dict[str, Any] | None = None,
) -> None:
    failures = validate_baseline(
        baseline_dir,
        runtime_descriptor=runtime_descriptor,
        require_release_runtime=True,
    )
    if failures:
        raise BaselineValidationError("\n".join(failures))
    manifest_failures: list[str] = []
    manifest = _load_json(
        baseline_dir / "manifest.json", "visual baseline manifest", manifest_failures
    )
    if manifest_failures or not isinstance(manifest, dict):
        raise BaselineValidationError("\n".join(manifest_failures))
    expected_pairs = {
        "baseline_sha256": manifest.get("baseline_sha256"),
        "runner_contract_sha256": manifest.get("runner_contract_sha256"),
        "scenario_contract_sha256": manifest.get("scenario_contract_sha256"),
        "scenario_count": manifest.get("screenshot_count"),
        "seed_overlay_sha256": manifest.get("seed_overlay_sha256"),
        "source_commit": manifest.get("source_commit"),
    }
    for key, actual in expected_pairs.items():
        if artifact.get(key) != actual:
            raise BaselineValidationError(
                f"downloaded visual manifest does not match lock field: {key}"
            )
    if artifact.get("manifest_sha256") != _sha256(baseline_dir / "manifest.json"):
        raise BaselineValidationError(
            "downloaded visual manifest sha256 does not match the lock"
        )
    if not canonical_seed_path.is_file() or canonical_seed_path.is_symlink():
        raise BaselineValidationError(
            f"canonical visual seed overlay must be a regular file: {canonical_seed_path}"
        )
    if _sha256(canonical_seed_path) != artifact.get("seed_overlay_sha256"):
        raise BaselineValidationError(
            "downloaded visual artifact does not match the canonical seed overlay"
        )


def verify_visual_artifact_signature(
    artifact_ref: str,
    *,
    signature_identity: str,
    signature_issuer: str,
    cosign_binary: str = "cosign",
) -> None:
    artifact_match = OCI_ARTIFACT_PATTERN.fullmatch(artifact_ref)
    if artifact_match is None:
        raise BaselineValidationError(
            "visual signature verification requires an immutable ghcr.io digest"
        )
    artifact_repository = (
        artifact_match.group("owner").lower(),
        artifact_match.group("repository").lower(),
    )
    if artifact_repository != OFFICIAL_VISUAL_REPOSITORY:
        raise BaselineValidationError(
            "visual artifact must belong to the official Auris Flow repository"
        )
    identity_match = VISUAL_BUILD_IDENTITY_PATTERN.fullmatch(signature_identity)
    if (
        identity_match is None
        or ".." in signature_identity
        or "//" in signature_identity.removeprefix("https://")
        or signature_identity.endswith("/")
    ):
        raise BaselineValidationError(
            "visual signature identity is not the protected visual build workflow"
        )
    identity_repository = (
        identity_match.group("owner").lower(),
        identity_match.group("repository").lower(),
    )
    if artifact_repository != identity_repository:
        raise BaselineValidationError(
            "visual artifact and signature identity belong to different repositories"
        )
    if identity_repository != OFFICIAL_VISUAL_REPOSITORY:
        raise BaselineValidationError(
            "visual signature identity must belong to the "
            "official Auris Flow repository"
        )
    if signature_issuer != GITHUB_ACTIONS_OIDC_ISSUER:
        raise BaselineValidationError(
            "visual signature issuer is not the GitHub Actions token issuer"
        )
    try:
        completed = subprocess.run(
            (
                cosign_binary,
                "verify",
                artifact_ref,
                "--certificate-identity",
                signature_identity,
                "--certificate-oidc-issuer",
                signature_issuer,
            ),
            cwd=ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BaselineValidationError(
            "Cosign is required to verify visual baseline provenance"
        ) from error
    if completed.returncode != 0:
        raise BaselineValidationError(
            "visual baseline signature is missing or has an untrusted workflow identity"
        )


def verify_visual_oci_provenance(
    artifact_ref: str,
    *,
    source_commit: str,
    oras_binary: str = "oras",
) -> None:
    artifact_match = OCI_ARTIFACT_PATTERN.fullmatch(artifact_ref)
    if artifact_match is None:
        raise BaselineValidationError(
            "visual OCI provenance requires an immutable GHCR digest"
        )
    if (
        artifact_match.group("owner").lower(),
        artifact_match.group("repository").lower(),
    ) != OFFICIAL_VISUAL_REPOSITORY:
        raise BaselineValidationError(
            "visual OCI provenance must belong to the official Auris Flow repository"
        )
    if not COMMIT_PATTERN.fullmatch(source_commit):
        raise BaselineValidationError(
            "visual OCI provenance source commit must be exact"
        )
    try:
        completed = subprocess.run(
            (oras_binary, "manifest", "fetch", artifact_ref),
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BaselineValidationError(
            "ORAS is required to verify visual OCI provenance"
        ) from error
    if completed.returncode != 0:
        raise BaselineValidationError("unable to read visual OCI provenance")
    try:
        manifest = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise BaselineValidationError(
            "visual OCI manifest is not valid JSON"
        ) from error
    annotations = manifest.get("annotations") if isinstance(manifest, dict) else None
    if not isinstance(annotations, dict):
        raise BaselineValidationError("visual OCI manifest annotations are missing")
    expected = {
        "org.opencontainers.image.revision": source_commit,
        "io.auris.visual.job-workflow-sha": source_commit,
    }
    for key, value in expected.items():
        if annotations.get(key) != value:
            raise BaselineValidationError(
                f"visual OCI provenance annotation does not match: {key}"
            )


def materialize_locked_baseline(
    lock_path: Path,
    destination: Path,
    *,
    canonical_seed_path: Path = CANONICAL_SEED_OVERLAY_PATH,
    runtime_descriptor: dict[str, Any] | None = None,
    check_repository_binding: bool = True,
    oras_binary: str = "oras",
    verify_signature: bool = False,
    cosign_binary: str = "cosign",
) -> Path:
    failures = validate_visual_baseline_lock(
        lock_path,
        require_approved=True,
        canonical_seed_path=canonical_seed_path,
    )
    if failures:
        raise BaselineValidationError("\n".join(failures))
    payload, load_failures = _load_visual_lock(lock_path)
    if (
        load_failures
        or payload is None
        or not isinstance(payload.get("artifact"), dict)
    ):
        raise BaselineValidationError("\n".join(load_failures))
    artifact: dict[str, Any] = payload["artifact"]
    if check_repository_binding:
        _verify_repository_binding(str(artifact["source_commit"]))
    if verify_signature:
        verify_visual_artifact_signature(
            str(artifact["reference"]),
            signature_identity=str(artifact["signature_identity"]),
            signature_issuer=str(artifact["signature_issuer"]),
            cosign_binary=cosign_binary,
        )
        verify_visual_oci_provenance(
            str(artifact["reference"]),
            source_commit=str(artifact["job_workflow_sha"]),
            oras_binary=oras_binary,
        )
    destination = destination.resolve()
    with tempfile.TemporaryDirectory(prefix="auris-visual-oci-") as temporary:
        pull_dir = Path(temporary) / "pull"
        pull_dir.mkdir()
        try:
            completed = subprocess.run(
                (
                    oras_binary,
                    "pull",
                    str(artifact["reference"]),
                    "--output",
                    str(pull_dir),
                ),
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise BaselineValidationError(
                "ORAS is required to download the approved visual baseline artifact"
            ) from error
        if completed.returncode != 0:
            raise BaselineValidationError(
                "unable to download the approved visual baseline OCI artifact"
            )
        pulled_entries = list(pull_dir.iterdir())
        package_path = pull_dir / ARTIFACT_PACKAGE_NAME
        if (
            [entry.name for entry in pulled_entries] != [ARTIFACT_PACKAGE_NAME]
            or not package_path.is_file()
            or package_path.is_symlink()
        ):
            raise BaselineValidationError(
                f"visual OCI artifact must contain only {ARTIFACT_PACKAGE_NAME}"
            )
        if _sha256(package_path) != artifact.get("package_sha256"):
            raise BaselineValidationError(
                "downloaded visual artifact package sha256 does not match the lock"
            )
        _extract_artifact_package(package_path, destination)
    _verify_materialized_against_lock(
        destination,
        artifact,
        canonical_seed_path=canonical_seed_path,
        runtime_descriptor=runtime_descriptor,
    )
    return destination


def write_visual_evidence(
    lock_path: Path,
    baseline_dir: Path,
    output_path: Path,
    *,
    canonical_seed_path: Path = CANONICAL_SEED_OVERLAY_PATH,
    runtime_descriptor: dict[str, Any] | None = None,
    check_repository_binding: bool = True,
    cosign_binary: str = "cosign",
    oras_binary: str = "oras",
) -> dict[str, Any]:
    output_path = output_path.resolve()
    if output_path.exists():
        if output_path.is_dir() or output_path.is_symlink():
            raise BaselineValidationError(
                f"visual evidence output must be a regular file path: {output_path}"
            )
        output_path.unlink()
    failures = validate_visual_baseline_lock(
        lock_path,
        require_approved=True,
        canonical_seed_path=canonical_seed_path,
    )
    if failures:
        raise BaselineValidationError("\n".join(failures))
    payload, load_failures = _load_visual_lock(lock_path)
    if (
        load_failures
        or payload is None
        or not isinstance(payload.get("artifact"), dict)
    ):
        raise BaselineValidationError("\n".join(load_failures))
    artifact: dict[str, Any] = payload["artifact"]
    verify_visual_artifact_signature(
        str(artifact["reference"]),
        signature_identity=str(artifact["signature_identity"]),
        signature_issuer=str(artifact["signature_issuer"]),
        cosign_binary=cosign_binary,
    )
    verify_visual_oci_provenance(
        str(artifact["reference"]),
        source_commit=str(artifact["job_workflow_sha"]),
        oras_binary=oras_binary,
    )
    baseline_source_commit = str(artifact["source_commit"])
    source_commit = baseline_source_commit
    if check_repository_binding:
        source_commit = _verify_repository_binding(baseline_source_commit)
    _verify_materialized_against_lock(
        baseline_dir,
        artifact,
        canonical_seed_path=canonical_seed_path,
        runtime_descriptor=runtime_descriptor,
    )
    reference = str(artifact["reference"])
    evidence: dict[str, Any] = {
        "baseline_oci_digest": reference.rsplit("@", 1)[1],
        "baseline_oci_ref": reference,
        "baseline_sha256": artifact["baseline_sha256"],
        "baseline_source_commit": baseline_source_commit,
        "job_workflow_sha": artifact["job_workflow_sha"],
        "kind": "auris-flow-visual-regression-evidence",
        "manifest_sha256": artifact["manifest_sha256"],
        "passed": EXPECTED_SCREENSHOT_COUNT,
        "runner_contract_sha256": artifact["runner_contract_sha256"],
        "scenario_count": EXPECTED_SCREENSHOT_COUNT,
        "schema_version": 1,
        "signature_identity": artifact["signature_identity"],
        "signature_issuer": artifact["signature_issuer"],
        "source_commit": source_commit,
        "status": "ok",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return evidence


def promote_oci_baseline(
    *,
    artifact_ref: str,
    source_commit: str,
    approval_reference: str,
    signature_identity: str,
    signature_issuer: str,
    lock_path: Path = VISUAL_LOCK_PATH,
    canonical_seed_path: Path = CANONICAL_SEED_OVERLAY_PATH,
    oras_binary: str = "oras",
    cosign_binary: str = "cosign",
) -> dict[str, Any]:
    source_commit = source_commit.lower()
    if not COMMIT_PATTERN.fullmatch(source_commit):
        raise BaselineValidationError(
            "visual promotion source_commit must be an exact Git commit id"
        )
    _verify_repository_binding(source_commit)
    if not OCI_ARTIFACT_PATTERN.fullmatch(artifact_ref):
        raise BaselineValidationError(
            "visual promotion requires an immutable "
            "ghcr.io/<owner>/<repo>/visual-baseline@sha256 reference"
        )
    verify_visual_artifact_signature(
        artifact_ref,
        signature_identity=signature_identity,
        signature_issuer=signature_issuer,
        cosign_binary=cosign_binary,
    )
    verify_visual_oci_provenance(
        artifact_ref,
        source_commit=source_commit,
        oras_binary=oras_binary,
    )
    with tempfile.TemporaryDirectory(prefix="auris-visual-promotion-") as temporary:
        temporary_root = Path(temporary)
        pull_dir = temporary_root / "pull"
        pull_dir.mkdir()
        try:
            completed = subprocess.run(
                (
                    oras_binary,
                    "pull",
                    artifact_ref,
                    "--output",
                    str(pull_dir),
                ),
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise BaselineValidationError(
                "ORAS is required to verify the visual promotion candidate"
            ) from error
        if completed.returncode != 0:
            raise BaselineValidationError(
                "unable to download the visual promotion candidate"
            )
        entries = list(pull_dir.iterdir())
        package_path = pull_dir / ARTIFACT_PACKAGE_NAME
        if (
            [entry.name for entry in entries] != [ARTIFACT_PACKAGE_NAME]
            or not package_path.is_file()
            or package_path.is_symlink()
        ):
            raise BaselineValidationError(
                f"visual OCI artifact must contain only {ARTIFACT_PACKAGE_NAME}"
            )
        baseline_dir = temporary_root / "baseline"
        _extract_artifact_package(package_path, baseline_dir)
        manifest = _manifest_for_lock(baseline_dir)
        if manifest.get("source_commit") != source_commit:
            raise BaselineValidationError(
                "visual promotion artifact source_commit does not match the approved commit"
            )
        if not canonical_seed_path.is_file() or canonical_seed_path.is_symlink():
            raise BaselineValidationError(
                f"canonical visual seed overlay must be a regular file: {canonical_seed_path}"
            )
        if manifest.get("seed_overlay_sha256") != _sha256(canonical_seed_path):
            raise BaselineValidationError(
                "visual promotion artifact does not match the canonical seed overlay"
            )
        return write_visual_baseline_lock(
            lock_path,
            baseline_dir=baseline_dir,
            package_path=package_path,
            artifact_ref=artifact_ref,
            approval_reference=approval_reference,
            signature_identity=signature_identity,
            signature_issuer=signature_issuer,
        )


def assert_update_target_safe(target: Path, frozen_root: Path) -> None:
    target = target.resolve()
    frozen_root = frozen_root.resolve()
    if target == frozen_root or target.is_relative_to(frozen_root):
        raise BaselineValidationError(
            "refusing to update the frozen visual baseline; choose a diagnostics "
            f"directory outside {frozen_root}: {target}"
        )
    if target.exists() and not target.is_dir():
        raise BaselineValidationError(
            f"visual update target must be a directory: {target}"
        )
    if target.is_dir():
        symlinks = sorted(
            path.relative_to(target).as_posix()
            for path in target.rglob("*")
            if path.is_symlink()
        )
        if symlinks:
            raise BaselineValidationError(
                "visual update target must not contain symlinks: " + ", ".join(symlinks)
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and content-address the Auris Flow visual baseline."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("baseline_dir", type=Path)
    verify_parser.add_argument("--runtime-descriptor", type=Path)
    verify_parser.add_argument("--require-release-runtime", action="store_true")

    write_parser = subparsers.add_parser("write-manifest")
    write_parser.add_argument("baseline_dir", type=Path)
    write_parser.add_argument("--reference-platform")
    write_parser.add_argument("--playwright-version")
    write_parser.add_argument("--runtime-descriptor", type=Path)
    write_parser.add_argument("--source-commit")

    lock_parser = subparsers.add_parser("validate-lock")
    lock_parser.add_argument("--lock", type=Path, default=VISUAL_LOCK_PATH)
    lock_parser.add_argument("--require-approved", action="store_true")

    materialize_parser = subparsers.add_parser("materialize-locked")
    materialize_parser.add_argument("--lock", type=Path, default=VISUAL_LOCK_PATH)
    materialize_parser.add_argument("--destination", type=Path, required=True)
    materialize_parser.add_argument("--runtime-descriptor", type=Path)
    materialize_parser.add_argument("--oras-binary", default="oras")
    materialize_parser.add_argument("--verify-signature", action="store_true")
    materialize_parser.add_argument("--cosign-binary", default="cosign")

    package_parser = subparsers.add_parser("create-package")
    package_parser.add_argument("baseline_dir", type=Path)
    package_parser.add_argument("package_path", type=Path)

    evidence_parser = subparsers.add_parser("write-evidence")
    evidence_parser.add_argument("baseline_dir", type=Path)
    evidence_parser.add_argument("--lock", type=Path, default=VISUAL_LOCK_PATH)
    evidence_parser.add_argument("--output", type=Path, required=True)
    evidence_parser.add_argument("--runtime-descriptor", type=Path)
    evidence_parser.add_argument("--cosign-binary", default="cosign")
    evidence_parser.add_argument("--oras-binary", default="oras")

    promote_parser = subparsers.add_parser("promote-oci")
    promote_parser.add_argument("--artifact-ref", required=True)
    promote_parser.add_argument("--source-commit", required=True)
    promote_parser.add_argument("--approval-reference", required=True)
    promote_parser.add_argument("--signature-identity", required=True)
    promote_parser.add_argument("--signature-issuer", required=True)
    promote_parser.add_argument("--lock-output", type=Path, default=VISUAL_LOCK_PATH)
    promote_parser.add_argument("--oras-binary", default="oras")
    promote_parser.add_argument("--cosign-binary", default="cosign")

    target_parser = subparsers.add_parser("check-update-target")
    target_parser.add_argument("--target", type=Path, required=True)
    target_parser.add_argument("--frozen-root", type=Path, required=True)

    subparsers.add_parser("runner-contract-sha256")

    policy_parser = subparsers.add_parser("check-execution-policy")
    policy_parser.add_argument("--release-check", choices=("0", "1"), required=True)
    policy_parser.add_argument("--update", choices=("0", "1"), required=True)
    policy_parser.add_argument("--default-goal-dir", type=Path, required=True)
    policy_parser.add_argument("--frozen-root", type=Path, required=True)
    policy_parser.add_argument("--diagnostics-root", type=Path, required=True)
    policy_parser.add_argument("--goal-dir", type=Path)
    policy_parser.add_argument("--seed-overlay", type=Path)
    policy_parser.add_argument("--runtime", choices=("container", "host"))
    return parser


def _read_runtime_descriptor(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    failures: list[str] = []
    payload = _load_json(path, "visual runtime descriptor", failures)
    if failures or not isinstance(payload, dict):
        raise BaselineValidationError(
            "\n".join(failures) or "runtime descriptor is invalid"
        )
    return payload


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.command == "verify":
            failures = validate_baseline(
                args.baseline_dir,
                runtime_descriptor=_read_runtime_descriptor(args.runtime_descriptor),
                require_release_runtime=args.require_release_runtime,
            )
            if failures:
                for failure in failures:
                    print(f"visual baseline: {failure}", file=sys.stderr)
                return 1
            print(f"visual baseline verified: {args.baseline_dir.resolve()}")
            return 0
        if args.command == "write-manifest":
            manifest = write_manifest(
                args.baseline_dir,
                reference_platform=args.reference_platform,
                playwright_version=args.playwright_version,
                runtime_descriptor=_read_runtime_descriptor(args.runtime_descriptor),
                source_commit=args.source_commit,
            )
            print(
                "visual baseline manifest written: "
                f"{args.baseline_dir.resolve()} ({manifest['baseline_sha256']})"
            )
            return 0
        if args.command == "validate-lock":
            failures = validate_visual_baseline_lock(
                args.lock, require_approved=args.require_approved
            )
            if failures:
                for failure in failures:
                    print(f"visual baseline: {failure}", file=sys.stderr)
                return 1
            print(f"visual baseline lock verified: {args.lock.resolve()}")
            return 0
        if args.command == "materialize-locked":
            destination = materialize_locked_baseline(
                args.lock,
                args.destination,
                runtime_descriptor=_read_runtime_descriptor(args.runtime_descriptor),
                oras_binary=args.oras_binary,
                verify_signature=args.verify_signature,
                cosign_binary=args.cosign_binary,
            )
            print(f"visual baseline materialized: {destination}")
            return 0
        if args.command == "create-package":
            package_path = create_artifact_package(args.baseline_dir, args.package_path)
            print(
                f"visual baseline package created: {package_path} ({_sha256(package_path)})"
            )
            return 0
        if args.command == "write-evidence":
            evidence = write_visual_evidence(
                args.lock,
                args.baseline_dir,
                args.output,
                runtime_descriptor=_read_runtime_descriptor(args.runtime_descriptor),
                cosign_binary=args.cosign_binary,
                oras_binary=args.oras_binary,
            )
            print(
                "visual baseline evidence written: "
                f"{args.output.resolve()} ({evidence['baseline_oci_digest']})"
            )
            return 0
        if args.command == "promote-oci":
            lock = promote_oci_baseline(
                artifact_ref=args.artifact_ref,
                source_commit=args.source_commit,
                approval_reference=args.approval_reference,
                signature_identity=args.signature_identity,
                signature_issuer=args.signature_issuer,
                lock_path=args.lock_output,
                oras_binary=args.oras_binary,
                cosign_binary=args.cosign_binary,
            )
            print(
                "visual baseline lock promoted: "
                f"{args.lock_output.resolve()} ({lock['artifact']['reference']})"
            )
            return 0
        if args.command == "check-update-target":
            assert_update_target_safe(args.target, args.frozen_root)
            print(f"visual update target accepted: {args.target.resolve()}")
            return 0
        if args.command == "runner-contract-sha256":
            print(runner_contract_sha256())
            return 0
        if args.command == "check-execution-policy":
            policy = resolve_visual_execution_policy(
                release_check=args.release_check == "1",
                update=args.update == "1",
                goal_dir=args.goal_dir,
                seed_overlay=args.seed_overlay,
                runtime=args.runtime,
                default_goal_dir=args.default_goal_dir,
                frozen_root=args.frozen_root,
                diagnostics_root=args.diagnostics_root,
            )
            print(
                json.dumps(
                    {
                        "runtime": policy.runtime,
                        "update": policy.update,
                        "visual_dir": str(policy.visual_dir),
                    },
                    sort_keys=True,
                )
            )
            return 0
    except (BaselineValidationError, OSError, ValueError) as error:
        print(f"visual baseline: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
