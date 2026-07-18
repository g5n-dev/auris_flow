from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_generator() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts" / "generate_supply_chain_evidence.py"
    spec = importlib.util.spec_from_file_location("auris_supply_chain_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fixture_project(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    backend = tmp_path / "backend"
    dagster = tmp_path / "production" / "dagster"
    frontend = tmp_path / "prototype" / "auris-flow-ui"
    backend.mkdir(parents=True)
    dagster.mkdir(parents=True)
    frontend.mkdir(parents=True)
    uv_lock = backend / "uv.lock"
    uv_lock.write_text(
        """
version = 1

[[package]]
name = "auris-flow-bff"
version = "0.1.0"
source = { virtual = "." }
dependencies = [{ name = "known-package" }, { name = "reviewed-package" }]

[[package]]
name = "known-package"
version = "1.2.3"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "reviewed-package"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }
""".lstrip(),
        encoding="utf-8",
    )
    dagster_uv_lock = dagster / "uv.lock"
    dagster_uv_lock.write_text(
        """
version = 1

[[package]]
name = "auris-flow-dagster"
version = "1.0.0"
source = { virtual = "." }
dependencies = [{ name = "dagster-lib" }]

[[package]]
name = "dagster-lib"
version = "7.8.9"
source = { registry = "https://pypi.org/simple" }
""".lstrip(),
        encoding="utf-8",
    )
    package_lock = frontend / "package-lock.json"
    package_lock.write_text(
        json.dumps(
            {
                "name": "auris-flow-ui",
                "version": "0.1.0",
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "auris-flow-ui", "version": "0.1.0"},
                    "node_modules/ui-lib": {
                        "version": "4.5.6",
                        "license": "Apache-2.0",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    exceptions = tmp_path / "config" / "release" / "license-review-exceptions.json"
    exceptions.parent.mkdir(parents=True)
    exceptions.write_text(
        json.dumps(
            {
                "schema_version": "auris.license-review-exceptions.v1",
                "exceptions": [
                    {
                        "ecosystem": "backend-python",
                        "name": "reviewed-package",
                        "version": "2.0.0",
                        "reason": "Upstream metadata is empty; the bundled license was reviewed.",
                        "reviewed_by": "release-security",
                        "reviewed_on": "2026-07-01",
                        "review_reference": "SEC-2026-0042",
                        "expires_on": "2026-08-01",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return uv_lock, dagster_uv_lock, package_lock, exceptions


def _python_inventory(*, reviewed_license: str = "") -> str:
    return json.dumps(
        [
            {
                "name": "known-package",
                "version": "1.2.3",
                "license_expression": "MIT",
                "license": "",
                "classifiers": [],
            },
            {
                "name": "reviewed-package",
                "version": "2.0.0",
                "license_expression": reviewed_license,
                "license": "",
                "classifiers": [],
            },
        ]
    )


def _dagster_python_inventory(*, license_expression: str = "BSD-3-Clause") -> str:
    return json.dumps(
        [
            {
                "name": "dagster-lib",
                "version": "7.8.9",
                "license_expression": license_expression,
                "license": "",
                "classifiers": [],
            }
        ]
    )


def _npm_sbom(*, serial: str, timestamp: str, license_id: str = "Apache-2.0") -> str:
    return json.dumps(
        {
            "$schema": "http://cyclonedx.org/schema/bom-1.5.schema.json",
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "serialNumber": serial,
            "version": 1,
            "metadata": {
                "timestamp": timestamp,
                "component": {
                    "bom-ref": "auris-flow-ui@0.1.0",
                    "type": "application",
                    "name": "auris-flow-ui",
                    "version": "0.1.0",
                    "properties": [
                        {
                            "name": "cdx:npm:package:path",
                            "value": "/private/build/auris-flow-ui",
                        }
                    ],
                },
            },
            "components": [
                {
                    "bom-ref": "ui-lib@4.5.6",
                    "type": "library",
                    "name": "ui-lib",
                    "version": "4.5.6",
                    "scope": "required",
                    "purl": "pkg:npm/ui-lib@4.5.6",
                    "licenses": [{"license": {"id": license_id}}],
                }
            ],
            "dependencies": [
                {"ref": "ui-lib@4.5.6", "dependsOn": []},
                {"ref": "auris-flow-ui@0.1.0", "dependsOn": ["ui-lib@4.5.6"]},
            ],
        }
    )


class FakeRunner:
    def __init__(
        self,
        backend_python_inventory: str,
        npm_sboms: list[str],
        *,
        dagster_python_inventory: str | None = None,
    ) -> None:
        self.backend_python_inventory = backend_python_inventory
        self.dagster_python_inventory = dagster_python_inventory or _dagster_python_inventory()
        self.npm_sboms = npm_sboms
        self.commands: list[tuple[tuple[str, ...], Path]] = []

    def __call__(self, command: list[str], *, cwd: Path) -> str:
        self.commands.append((tuple(command), cwd))
        if command[:2] == ["uv", "sync"]:
            return ""
        if command[-2:] == ["--all", "--json"]:
            return "{}"
        if command[:2] == ["npm", "ls"]:
            return "{}"
        if command[:2] == ["npm", "sbom"]:
            return self.npm_sboms.pop(0)
        if command[0].endswith("python") or command[0] == "python":
            if cwd.name == "dagster":
                return self.dagster_python_inventory
            return self.backend_python_inventory
        raise AssertionError(f"unexpected command: {command}")


def _generate(
    generator: ModuleType,
    tmp_path: Path,
    runner: FakeRunner,
    *,
    exceptions_path: Path | None,
    output_name: str = "evidence",
) -> dict[str, Any]:
    backend_uv_lock = tmp_path / "backend" / "uv.lock"
    dagster_uv_lock = tmp_path / "production" / "dagster" / "uv.lock"
    package_lock = tmp_path / "prototype" / "auris-flow-ui" / "package-lock.json"
    if not backend_uv_lock.is_file() or not dagster_uv_lock.is_file() or not package_lock.is_file():
        backend_uv_lock, dagster_uv_lock, package_lock, _ = _write_fixture_project(tmp_path)
    return generator.generate_evidence(
        root=tmp_path,
        backend_uv_lock_path=backend_uv_lock,
        dagster_uv_lock_path=dagster_uv_lock,
        package_lock_path=package_lock,
        output_dir=tmp_path / output_name,
        exceptions_path=exceptions_path,
        backend_python_executable=Path("backend-python"),
        dagster_python_executable=Path("dagster-python"),
        uv_executable="uv",
        npm_executable="npm",
        runner=runner,
        today=date(2026, 7, 18),
        source_commit="1" * 40,
    )


def test_generation_is_deterministic_sorted_and_path_sanitized(tmp_path: Path) -> None:
    generator = _load_generator()
    _, _, _, exceptions = _write_fixture_project(tmp_path)
    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence" / "python.cdx.json").write_text("stale combined graph", encoding="utf-8")
    first_runner = FakeRunner(
        _python_inventory(),
        [_npm_sbom(serial="urn:uuid:first", timestamp="2026-07-18T01:02:03Z")],
    )
    first = _generate(generator, tmp_path, first_runner, exceptions_path=exceptions)
    first_bytes = {
        path.name: path.read_bytes()
        for path in sorted((tmp_path / "evidence").iterdir())
        if path.is_file()
    }

    second_runner = FakeRunner(
        _python_inventory(),
        [_npm_sbom(serial="urn:uuid:second", timestamp="2030-01-01T00:00:00Z")],
    )
    second = _generate(generator, tmp_path, second_runner, exceptions_path=exceptions)
    second_bytes = {
        path.name: path.read_bytes()
        for path in sorted((tmp_path / "evidence").iterdir())
        if path.is_file()
    }

    assert first == second
    assert first_bytes == second_bytes
    commands = [command for command, _ in first_runner.commands]
    assert ("uv", "sync", "--frozen", "--all-extras", "--check") in commands
    assert ("npm", "ls", "--all", "--json") in commands
    assert any(command[:2] == ("npm", "sbom") for command in commands)
    rendered = b"\n".join(first_bytes.values()).decode()
    assert "/private/build" not in rendered
    assert "urn:uuid:" not in rendered
    assert "timestamp" not in rendered
    assert first["component_counts"] == {
        "backend-python": 2,
        "dagster-python": 1,
        "npm": 1,
        "total": 4,
    }
    assert first["source_commit"] == "1" * 40
    assert set(first_bytes) == {
        "backend-python.cdx.json",
        "dagster-python.cdx.json",
        "dependency-licenses.json",
        "evidence-manifest.json",
        "npm.cdx.json",
    }
    inventory = json.loads(first_bytes["dependency-licenses.json"])
    assert [
        (item["ecosystem"], item["name"], item["version"]) for item in inventory["dependencies"]
    ] == [
        ("backend-python", "known-package", "1.2.3"),
        ("backend-python", "reviewed-package", "2.0.0"),
        ("dagster-python", "dagster-lib", "7.8.9"),
        ("npm", "ui-lib", "4.5.6"),
    ]
    reviewed = inventory["dependencies"][1]
    assert reviewed["license_status"] == "reviewed-exception"
    assert reviewed["obligations"] == [
        "comply-with-reviewed-exception-and-upstream-license-terms",
        "retain-upstream-license-and-copyright-notices",
    ]
    assert reviewed["review_exception"]["review_reference"] == "SEC-2026-0042"
    approved = inventory["dependencies"][0]
    assert approved["license_status"] == "approved-compatible"
    assert approved["obligations"] == ["retain-upstream-license-and-copyright-notices"]
    assert inventory["policy"] == {
        "allowed_expression_operators": ["AND", "OR"],
        "allowed_license_identifiers": [
            "0BSD",
            "Apache-2.0",
            "BSD-2-Clause",
            "BSD-3-Clause",
            "ISC",
            "MIT",
            "MIT-0",
            "MPL-2.0",
            "PSF-2.0",
        ],
        "denied_without_exact_review_exception": [
            "license-outside-allowlist",
            "missing-or-unknown-license",
            "non-spdx-or-ambiguous-license",
            "spdx-license-exception",
        ],
        "review_exception_scope": "exact-ecosystem-name-version",
        "review_exception_schema": "auris.license-review-exceptions.v1",
    }


@pytest.mark.parametrize(
    "license_conclusion",
    [
        "BSD",
        "Dual License",
        "GPL-2.0-only",
        "MIT WITH Classpath-exception-2.0",
        "MIT OR",
    ],
)
def test_ambiguous_or_unapproved_license_fails_without_exact_exception(
    tmp_path: Path, license_conclusion: str
) -> None:
    generator = _load_generator()
    _write_fixture_project(tmp_path)
    runner = FakeRunner(
        _python_inventory(reviewed_license="MIT"),
        [_npm_sbom(serial="urn:uuid:test", timestamp="2026-07-18T00:00:00Z")],
        dagster_python_inventory=_dagster_python_inventory(license_expression=license_conclusion),
    )

    with pytest.raises(generator.EvidenceError, match="allowlist|ambiguous|SPDX"):
        _generate(generator, tmp_path, runner, exceptions_path=None)

    assert not (tmp_path / "evidence").exists()


def test_exact_review_exception_can_cover_unapproved_conclusion(
    tmp_path: Path,
) -> None:
    generator = _load_generator()
    _, _, _, exceptions = _write_fixture_project(tmp_path)
    runner = FakeRunner(
        _python_inventory(reviewed_license="GPL-2.0-only"),
        [_npm_sbom(serial="urn:uuid:test", timestamp="2026-07-18T00:00:00Z")],
    )

    _generate(generator, tmp_path, runner, exceptions_path=exceptions)

    inventory = json.loads(
        (tmp_path / "evidence" / "dependency-licenses.json").read_text(encoding="utf-8")
    )
    reviewed = next(
        item for item in inventory["dependencies"] if item["name"] == "reviewed-package"
    )
    assert reviewed["license"] == "GPL-2.0-only"
    assert reviewed["license_status"] == "reviewed-exception"
    assert reviewed["review_exception"]["review_reference"] == "SEC-2026-0042"


def test_high_confidence_alias_and_spdx_expression_are_approved() -> None:
    generator = _load_generator()

    dependencies = generator.apply_license_policy(
        [
            {
                "ecosystem": "npm",
                "name": "bsd-lib",
                "version": "1.0.0",
                "license": "3-Clause BSD License",
            },
            {
                "ecosystem": "npm",
                "name": "dual-compatible-lib",
                "version": "2.0.0",
                "license": "(MIT OR Apache-2.0)",
            },
        ],
        {},
    )

    assert dependencies[0]["license"] == "BSD-3-Clause"
    assert dependencies[0]["license_status"] == "approved-compatible"
    assert dependencies[1]["license"] == "(MIT OR Apache-2.0)"
    assert dependencies[1]["license_status"] == "approved-compatible"
    assert dependencies[1]["obligations"] == [
        "preserve-apache-notice-and-state-changes",
        "retain-upstream-license-and-copyright-notices",
    ]


@pytest.mark.parametrize("missing_ecosystem", ["backend-python", "dagster-python", "npm"])
def test_missing_or_unknown_license_fails_closed(tmp_path: Path, missing_ecosystem: str) -> None:
    generator = _load_generator()
    _, _, package_lock, _ = _write_fixture_project(tmp_path)
    if missing_ecosystem == "npm":
        document = json.loads(package_lock.read_text(encoding="utf-8"))
        document["packages"]["node_modules/ui-lib"]["license"] = "UNKNOWN"
        package_lock.write_text(json.dumps(document), encoding="utf-8")
    runner = FakeRunner(
        _python_inventory(
            reviewed_license=("UNKNOWN" if missing_ecosystem == "backend-python" else "MIT")
        ),
        [
            _npm_sbom(
                serial="urn:uuid:test",
                timestamp="2026-07-18T00:00:00Z",
                license_id="UNKNOWN" if missing_ecosystem == "npm" else "MIT",
            )
        ],
        dagster_python_inventory=_dagster_python_inventory(
            license_expression=(
                "UNKNOWN" if missing_ecosystem == "dagster-python" else "BSD-3-Clause"
            )
        ),
    )

    with pytest.raises(generator.EvidenceError, match="license"):
        _generate(generator, tmp_path, runner, exceptions_path=None)

    assert not (tmp_path / "evidence").exists()


def test_expired_or_unused_review_exception_fails_closed(tmp_path: Path) -> None:
    generator = _load_generator()
    _, _, _, exceptions = _write_fixture_project(tmp_path)
    document = json.loads(exceptions.read_text(encoding="utf-8"))
    document["exceptions"][0]["expires_on"] = "2026-07-17"
    exceptions.write_text(json.dumps(document), encoding="utf-8")
    runner = FakeRunner(
        _python_inventory(),
        [_npm_sbom(serial="urn:uuid:test", timestamp="2026-07-18T00:00:00Z")],
    )

    with pytest.raises(generator.EvidenceError, match="expired"):
        _generate(generator, tmp_path, runner, exceptions_path=exceptions)

    document["exceptions"][0]["expires_on"] = "2026-08-01"
    exceptions.write_text(json.dumps(document), encoding="utf-8")
    runner = FakeRunner(
        _python_inventory(reviewed_license="MIT"),
        [_npm_sbom(serial="urn:uuid:test", timestamp="2026-07-18T00:00:00Z")],
    )
    with pytest.raises(generator.EvidenceError, match="unused"):
        _generate(generator, tmp_path, runner, exceptions_path=exceptions)


def test_locked_install_version_mismatch_fails_before_writing(tmp_path: Path) -> None:
    generator = _load_generator()
    inventory = json.loads(_python_inventory(reviewed_license="MIT"))
    inventory[0]["version"] = "9.9.9"
    runner = FakeRunner(
        json.dumps(inventory),
        [_npm_sbom(serial="urn:uuid:test", timestamp="2026-07-18T00:00:00Z")],
    )

    with pytest.raises(generator.EvidenceError, match="locked version"):
        _generate(generator, tmp_path, runner, exceptions_path=None)

    assert not (tmp_path / "evidence").exists()


def test_review_exception_rejects_secret_or_absolute_path_material(tmp_path: Path) -> None:
    generator = _load_generator()
    _, _, _, exceptions = _write_fixture_project(tmp_path)
    document = json.loads(exceptions.read_text(encoding="utf-8"))
    document["exceptions"][0]["reason"] = (
        "Reviewed locally; password=not-release-evidence at /private/reviewer/notes."
    )
    exceptions.write_text(json.dumps(document), encoding="utf-8")
    runner = FakeRunner(
        _python_inventory(),
        [_npm_sbom(serial="urn:uuid:test", timestamp="2026-07-18T00:00:00Z")],
    )

    with pytest.raises(generator.EvidenceError, match="path|secret"):
        _generate(generator, tmp_path, runner, exceptions_path=exceptions)

    assert runner.commands == []
