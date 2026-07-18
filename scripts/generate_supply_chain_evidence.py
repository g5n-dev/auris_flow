#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKEND_UV_LOCK = ROOT / "backend" / "uv.lock"
DEFAULT_DAGSTER_UV_LOCK = ROOT / "production" / "dagster" / "uv.lock"
DEFAULT_PACKAGE_LOCK = ROOT / "prototype" / "auris-flow-ui" / "package-lock.json"
DEFAULT_OUTPUT = ROOT / "build" / "release-evidence"
DEFAULT_EXCEPTIONS = ROOT / "config" / "release" / "license-review-exceptions.json"
DEFAULT_BACKEND_PYTHON = ROOT / "backend" / ".venv" / "bin" / "python"
DEFAULT_DAGSTER_PYTHON = ROOT / "production" / "dagster" / ".venv" / "bin" / "python"

EXCEPTION_SCHEMA = "auris.license-review-exceptions.v1"
INVENTORY_SCHEMA = "auris.dependency-license-inventory.v1"
MANIFEST_SCHEMA = "auris.release-evidence-manifest.v1"
GENERATOR_VERSION = "2"
COMMIT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")

PYTHON_ECOSYSTEMS = frozenset({"backend-python", "dagster-python"})

Runner = Callable[..., str]

_UNKNOWN_LICENSES = {
    "",
    "n/a",
    "none",
    "null",
    "see license in package",
    "unlicensed",
    "unknown",
}
_LICENSE_ALIASES = {
    "0bsd": "0BSD",
    "apache 2": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "bsd 2-clause license": "BSD-2-Clause",
    "bsd 3-clause license": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "2-clause bsd license": "BSD-2-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "isc": "ISC",
    "isc license": "ISC",
    "mit": "MIT",
    "mit license": "MIT",
    "mit-0": "MIT-0",
    "mozilla public license 2.0": "MPL-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "mpl-2.0": "MPL-2.0",
    "psf-2.0": "PSF-2.0",
    "psfl": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
}
_APPROVED_LICENSE_IDENTIFIERS = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "MIT",
        "MIT-0",
        "MPL-2.0",
        "PSF-2.0",
    }
)
_BASE_LICENSE_OBLIGATION = "retain-upstream-license-and-copyright-notices"
_LICENSE_OBLIGATIONS = {
    "Apache-2.0": frozenset({"preserve-apache-notice-and-state-changes"}),
    "MPL-2.0": frozenset({"publish-modified-mpl-covered-file-source"}),
    "PSF-2.0": frozenset({"include-psf-license-and-change-summary"}),
}
_REVIEW_EXCEPTION_OBLIGATIONS = frozenset(
    {
        "comply-with-reviewed-exception-and-upstream-license-terms",
        _BASE_LICENSE_OBLIGATION,
    }
)
_SPDX_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*|[()]")
_PSF_LICENSE_TEXT_PREFIX = (
    "1. This LICENSE AGREEMENT is between the Python Software Foundation "
    '("PSF"), and the Individual or Organization'
)
_CLASSIFIER_LICENSES = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
}
_ALLOWED_EXCEPTION_FIELDS = {
    "ecosystem",
    "name",
    "version",
    "reason",
    "reviewed_by",
    "reviewed_on",
    "review_reference",
    "expires_on",
}
_SECRET_PATTERN = re.compile(
    r"(?i)(?:password|token|secret|api[_-]?key)\s*[=:]\s*[^\s,;]+"
)
_CREDENTIAL_URL_PATTERN = re.compile(r"://[^/@\s]+:[^/@\s]+@")
_WINDOWS_PATH_PATTERN = re.compile(r"(?:^|[\s\"'=])[A-Za-z]:[\\/]")
_POSIX_PATH_PATTERN = re.compile(r"(?:^|[\s\"'=])/(?!/)")

_PYTHON_METADATA_PROGRAM = r"""
import importlib.metadata as metadata
import json

rows = []
for distribution in metadata.distributions():
    package_metadata = distribution.metadata
    name = package_metadata.get("Name")
    if not name:
        continue
    rows.append(
        {
            "name": name,
            "version": distribution.version,
            "license_expression": package_metadata.get("License-Expression", ""),
            "license": package_metadata.get("License", ""),
            "classifiers": package_metadata.get_all("Classifier") or [],
        }
    )
print(json.dumps(rows, ensure_ascii=False, sort_keys=True))
""".strip()


class EvidenceError(RuntimeError):
    """Raised when release evidence cannot be generated safely and completely."""


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_runner(command: list[str], *, cwd: Path) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise EvidenceError(
            f"required command is unavailable: {Path(command[0]).name}"
        ) from exc
    if completed.returncode != 0:
        command_name = Path(command[0]).name
        raise EvidenceError(
            f"{command_name} evidence command failed with exit code {completed.returncode}"
        )
    return completed.stdout


def _read_json(path: Path, *, description: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid {description}") from exc


def _parse_json_output(raw: str, *, description: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"{description} did not return valid JSON") from exc


def _relative_input_path(path: Path, root: Path) -> str:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise EvidenceError(
            "release evidence inputs must be located inside the repository"
        ) from exc
    return relative.as_posix()


def _validate_safe_string(value: str) -> None:
    if (
        value.startswith(("/", "\\\\", "file://"))
        or "file://" in value.lower()
        or _WINDOWS_PATH_PATTERN.search(value)
        or _POSIX_PATH_PATTERN.search(value)
    ):
        raise EvidenceError("generated evidence contains an absolute filesystem path")
    if _SECRET_PATTERN.search(value) or _CREDENTIAL_URL_PATTERN.search(value):
        raise EvidenceError("generated evidence contains secret-like material")


def validate_evidence_document(value: Any) -> None:
    if isinstance(value, str):
        _validate_safe_string(value)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_safe_string(str(key))
            validate_evidence_document(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            validate_evidence_document(item)


def _normalize_license(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    if normalized.lower() in _UNKNOWN_LICENSES:
        return None
    if normalized.startswith(_PSF_LICENSE_TEXT_PREFIX):
        return "PSF-2.0"
    return _LICENSE_ALIASES.get(normalized.lower(), normalized)


def _tokenize_spdx_expression(expression: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(expression):
        if expression[position].isspace():
            position += 1
            continue
        match = _SPDX_TOKEN_PATTERN.match(expression, position)
        if match is None:
            raise ValueError("invalid SPDX token")
        tokens.append(match.group(0))
        position = match.end()
    if not tokens:
        raise ValueError("empty SPDX expression")
    return tokens


def _spdx_license_identifiers(expression: str) -> frozenset[str]:
    tokens = _tokenize_spdx_expression(expression)
    position = 0
    identifiers: set[str] = set()

    def parse_factor() -> None:
        nonlocal position
        if position >= len(tokens):
            raise ValueError("incomplete SPDX expression")
        token = tokens[position]
        if token == "(":
            position += 1
            parse_or_expression()
            if position >= len(tokens) or tokens[position] != ")":
                raise ValueError("unbalanced SPDX expression")
            position += 1
            return
        if token in {"AND", "OR", "WITH", ")"}:
            raise ValueError("invalid SPDX expression operand")
        identifiers.add(token)
        position += 1

    def parse_and_expression() -> None:
        nonlocal position
        parse_factor()
        while position < len(tokens) and tokens[position] == "AND":
            position += 1
            parse_factor()

    def parse_or_expression() -> None:
        nonlocal position
        parse_and_expression()
        while position < len(tokens) and tokens[position] == "OR":
            position += 1
            parse_and_expression()

    parse_or_expression()
    if position != len(tokens):
        raise ValueError("unsupported or invalid SPDX expression")
    return frozenset(identifiers)


def _approved_license_expression(expression: str) -> frozenset[str] | None:
    try:
        identifiers = _spdx_license_identifiers(expression)
    except ValueError:
        return None
    if not identifiers or not identifiers.issubset(_APPROVED_LICENSE_IDENTIFIERS):
        return None
    return identifiers


def _license_obligations(identifiers: frozenset[str]) -> list[str]:
    obligations = {_BASE_LICENSE_OBLIGATION}
    for identifier in identifiers:
        obligations.update(_LICENSE_OBLIGATIONS.get(identifier, ()))
    return sorted(obligations)


def _python_license(metadata: Mapping[str, Any]) -> str | None:
    expression = _normalize_license(metadata.get("license_expression"))
    if expression is not None:
        return expression
    declared = _normalize_license(metadata.get("license"))
    if declared is not None:
        return declared
    classifiers = metadata.get("classifiers", [])
    if not isinstance(classifiers, list):
        raise EvidenceError("Python package classifier metadata is invalid")
    conclusions = {
        _CLASSIFIER_LICENSES[classifier]
        for classifier in classifiers
        if isinstance(classifier, str) and classifier in _CLASSIFIER_LICENSES
    }
    if len(conclusions) == 1:
        return conclusions.pop()
    return None


def _cyclonedx_license(component: Mapping[str, Any]) -> str | None:
    licenses = component.get("licenses")
    if not isinstance(licenses, list) or not licenses:
        return None
    conclusions: list[str] = []
    for entry in licenses:
        if not isinstance(entry, Mapping):
            continue
        expression = _normalize_license(entry.get("expression"))
        if expression is not None:
            conclusions.append(expression)
            continue
        license_value = entry.get("license")
        if not isinstance(license_value, Mapping):
            continue
        conclusion = _normalize_license(license_value.get("id")) or _normalize_license(
            license_value.get("name")
        )
        if conclusion is not None:
            conclusions.append(conclusion)
    if not conclusions:
        return None
    unique = list(dict.fromkeys(conclusions))
    return " OR ".join(unique)


def _license_block(conclusion: str) -> list[dict[str, str]]:
    return [{"expression": conclusion}]


def _component_key(ecosystem: str, name: str, version: str) -> tuple[str, str, str]:
    return ecosystem, canonical_name(name), version


def load_review_exceptions(
    path: Path | None, *, today: date
) -> dict[tuple[str, str, str], dict[str, str]]:
    if path is None:
        return {}
    if not path.is_file():
        raise EvidenceError("configured license review exception file does not exist")
    document = _read_json(path, description="license review exception file")
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "exceptions",
    }:
        raise EvidenceError("license review exception document has unexpected fields")
    if document.get("schema_version") != EXCEPTION_SCHEMA:
        raise EvidenceError("unsupported license review exception schema")
    entries = document.get("exceptions")
    if not isinstance(entries, list):
        raise EvidenceError("license review exceptions must be a list")

    result: dict[tuple[str, str, str], dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _ALLOWED_EXCEPTION_FIELDS:
            raise EvidenceError(
                "license review exception has missing or unexpected fields"
            )
        if not all(
            isinstance(value, str) and value.strip() for value in entry.values()
        ):
            raise EvidenceError(
                "license review exception fields must be non-empty strings"
            )
        ecosystem = entry["ecosystem"].strip().lower()
        if ecosystem not in {"npm", *PYTHON_ECOSYSTEMS}:
            raise EvidenceError(
                "license review exception ecosystem must be backend-python, "
                "dagster-python, or npm"
            )
        if len(entry["reason"].strip()) < 20:
            raise EvidenceError("license review exception reason is too short")
        try:
            reviewed_on = date.fromisoformat(entry["reviewed_on"])
            expires_on = date.fromisoformat(entry["expires_on"])
        except ValueError as exc:
            raise EvidenceError(
                "license review exception dates must use YYYY-MM-DD"
            ) from exc
        if reviewed_on > today:
            raise EvidenceError("license review exception review date is in the future")
        if expires_on < today:
            raise EvidenceError("license review exception is expired")
        if expires_on < reviewed_on:
            raise EvidenceError(
                "license review exception expires before it was reviewed"
            )
        normalized = {key: value.strip() for key, value in entry.items()}
        normalized["ecosystem"] = ecosystem
        normalized["name"] = canonical_name(entry["name"])
        key = _component_key(ecosystem, entry["name"], entry["version"])
        if key in result:
            raise EvidenceError("duplicate license review exception")
        validate_evidence_document(normalized)
        result[key] = normalized
    return result


def _load_uv_packages(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise EvidenceError("invalid uv lock file") from exc
    packages = document.get("package")
    if not isinstance(packages, list):
        raise EvidenceError("uv lock file has no package graph")
    root_packages: list[dict[str, Any]] = []
    locked: dict[str, dict[str, Any]] = {}
    for package in packages:
        if not isinstance(package, dict):
            raise EvidenceError("uv lock package entry is invalid")
        name = package.get("name")
        version = package.get("version")
        source = package.get("source")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(source, dict)
        ):
            raise EvidenceError("uv lock package identity is invalid")
        if "virtual" in source or "editable" in source:
            root_packages.append(package)
            continue
        key = canonical_name(name)
        if key in locked:
            raise EvidenceError("uv lock contains duplicate normalized package names")
        locked[key] = package
    if len(root_packages) != 1:
        raise EvidenceError("uv lock must contain exactly one first-party project")
    return root_packages[0], locked


def _python_bom_ref(name: str, version: str) -> str:
    return f"pkg:pypi/{quote(canonical_name(name), safe='')}@{quote(version, safe='')}"


def build_python_evidence(
    *,
    ecosystem: str,
    uv_lock_path: Path,
    backend_dir: Path,
    python_executable: Path,
    uv_executable: str,
    runner: Runner,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if ecosystem not in PYTHON_ECOSYSTEMS:
        raise EvidenceError("unsupported Python evidence ecosystem")
    runner(
        [uv_executable, "sync", "--frozen", "--all-extras", "--check"],
        cwd=backend_dir,
    )
    raw_inventory = runner(
        [str(python_executable), "-c", _PYTHON_METADATA_PROGRAM],
        cwd=backend_dir,
    )
    installed = _parse_json_output(
        raw_inventory, description="Python package inspector"
    )
    if not isinstance(installed, list):
        raise EvidenceError("Python package inspector returned an invalid inventory")

    project, locked = _load_uv_packages(uv_lock_path)
    project_name = str(project["name"])
    project_version = str(project["version"])
    project_key = canonical_name(project_name)
    observed: dict[str, Mapping[str, Any]] = {}
    for item in installed:
        if not isinstance(item, Mapping):
            raise EvidenceError("Python installed package entry is invalid")
        name = item.get("name")
        version = item.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise EvidenceError("Python installed package identity is invalid")
        key = canonical_name(name)
        if key == project_key:
            continue
        package = locked.get(key)
        if package is None:
            raise EvidenceError(
                f"installed Python package is absent from uv.lock: {key}"
            )
        if package["version"] != version:
            raise EvidenceError(
                f"installed Python package has a different locked version: {key}"
            )
        if key in observed:
            raise EvidenceError(f"duplicate installed Python package metadata: {key}")
        observed[key] = item
    if not observed:
        raise EvidenceError(
            "Python locked installation contains no third-party packages"
        )

    components: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    reference_by_name: dict[str, str] = {}
    for key in sorted(observed):
        package = locked[key]
        name = str(package["name"])
        version = str(package["version"])
        conclusion = _python_license(observed[key])
        bom_ref = _python_bom_ref(name, version)
        reference_by_name[key] = bom_ref
        component: dict[str, Any] = {
            "bom-ref": bom_ref,
            "type": "library",
            "name": name,
            "version": version,
            "purl": bom_ref,
        }
        if conclusion is not None:
            component["licenses"] = _license_block(conclusion)
        components.append(component)
        inventory.append(
            {
                "ecosystem": ecosystem,
                "name": name,
                "version": version,
                "license": conclusion,
            }
        )

    project_ref = _python_bom_ref(project_name, project_version)
    dependencies: list[dict[str, Any]] = []
    for key in sorted(observed):
        package = locked[key]
        dependency_names = package.get("dependencies", [])
        if not isinstance(dependency_names, list):
            raise EvidenceError("uv lock dependency entry is invalid")
        depends_on = sorted(
            {
                reference_by_name[canonical_name(str(dependency["name"]))]
                for dependency in dependency_names
                if isinstance(dependency, dict)
                and "name" in dependency
                and canonical_name(str(dependency["name"])) in reference_by_name
            }
        )
        dependencies.append({"ref": reference_by_name[key], "dependsOn": depends_on})
    project_dependencies = project.get("dependencies", [])
    if not isinstance(project_dependencies, list):
        raise EvidenceError("uv lock project dependencies are invalid")
    root_depends_on = sorted(
        {
            reference_by_name[canonical_name(str(dependency["name"]))]
            for dependency in project_dependencies
            if isinstance(dependency, dict)
            and "name" in dependency
            and canonical_name(str(dependency["name"])) in reference_by_name
        }
    )
    dependencies.append({"ref": project_ref, "dependsOn": root_depends_on})
    dependencies.sort(key=lambda item: item["ref"])

    sbom = {
        "$schema": "http://cyclonedx.org/schema/bom-1.5.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "bom-ref": project_ref,
                "type": "application",
                "name": project_name,
                "version": project_version,
                "licenses": _license_block("Apache-2.0"),
                "purl": project_ref,
            },
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "auris-supply-chain-evidence",
                        "version": GENERATOR_VERSION,
                    }
                ]
            },
        },
        "components": components,
        "dependencies": dependencies,
    }
    return sbom, inventory


def _npm_name_from_lock_path(path: str) -> str:
    marker = "node_modules/"
    if marker not in path:
        raise EvidenceError("npm lock package path is not a node_modules entry")
    name = path.rsplit(marker, 1)[1]
    if not name or name.startswith("/"):
        raise EvidenceError("npm lock package path has an invalid name")
    return name


def _load_npm_lock(
    path: Path,
) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    document = _read_json(path, description="npm package lock")
    if not isinstance(document, dict) or document.get("lockfileVersion") != 3:
        raise EvidenceError("npm package lock must use lockfileVersion 3")
    packages = document.get("packages")
    if not isinstance(packages, dict) or not isinstance(packages.get(""), dict):
        raise EvidenceError("npm package lock has no root package")
    locked: dict[tuple[str, str], dict[str, Any]] = {}
    for path_key, package in packages.items():
        if path_key == "":
            continue
        if not isinstance(path_key, str) or not isinstance(package, dict):
            raise EvidenceError("npm lock package entry is invalid")
        version = package.get("version")
        if not isinstance(version, str):
            raise EvidenceError("npm lock package version is invalid")
        name = _npm_name_from_lock_path(path_key)
        key = (name, version)
        if key in locked:
            continue
        locked[key] = package
    if not locked:
        raise EvidenceError("npm package lock contains no third-party packages")
    return packages[""], locked


def _npm_bom_ref(name: str, version: str) -> str:
    return f"pkg:npm/{quote(name, safe='/')}@{quote(version, safe='')}"


def _safe_hashes(component: Mapping[str, Any]) -> list[dict[str, str]]:
    hashes = component.get("hashes", [])
    if not isinstance(hashes, list):
        return []
    result: list[dict[str, str]] = []
    for item in hashes:
        if not isinstance(item, Mapping):
            continue
        algorithm = item.get("alg")
        content = item.get("content")
        if (
            isinstance(algorithm, str)
            and isinstance(content, str)
            and re.fullmatch(r"[A-Fa-f0-9]+", content)
        ):
            result.append({"alg": algorithm.upper(), "content": content.lower()})
    return sorted(result, key=lambda item: (item["alg"], item["content"]))


def build_npm_evidence(
    *,
    package_lock_path: Path,
    frontend_dir: Path,
    npm_executable: str,
    runner: Runner,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    runner([npm_executable, "ls", "--all", "--json"], cwd=frontend_dir)
    raw_sbom = runner(
        [
            npm_executable,
            "sbom",
            "--package-lock-only",
            "--sbom-format",
            "cyclonedx",
            "--sbom-type",
            "application",
        ],
        cwd=frontend_dir,
    )
    source_sbom = _parse_json_output(raw_sbom, description="npm sbom")
    if not isinstance(source_sbom, dict) or source_sbom.get("bomFormat") != "CycloneDX":
        raise EvidenceError("npm sbom output is not CycloneDX")
    root_package, locked = _load_npm_lock(package_lock_path)
    root_name = root_package.get("name")
    root_version = root_package.get("version")
    if not isinstance(root_name, str) or not isinstance(root_version, str):
        raise EvidenceError("npm root package identity is invalid")

    source_components = source_sbom.get("components")
    if not isinstance(source_components, list):
        raise EvidenceError("npm sbom contains no component graph")
    observed: dict[tuple[str, str], Mapping[str, Any]] = {}
    original_ref_to_key: dict[str, tuple[str, str]] = {}
    for source_component in source_components:
        if not isinstance(source_component, Mapping):
            raise EvidenceError("npm sbom component entry is invalid")
        name = source_component.get("name")
        version = source_component.get("version")
        original_ref = source_component.get("bom-ref")
        if not isinstance(name, str) or not isinstance(version, str):
            raise EvidenceError("npm sbom component identity is invalid")
        key = (name, version)
        if key not in locked:
            raise EvidenceError(
                f"npm sbom component is absent from package-lock.json: {name}"
            )
        if key in observed:
            raise EvidenceError(
                f"npm sbom contains a duplicate component: {name}@{version}"
            )
        observed[key] = source_component
        if isinstance(original_ref, str):
            original_ref_to_key[original_ref] = key
    if set(observed) != set(locked):
        missing = sorted(set(locked) - set(observed))
        name, version = missing[0]
        raise EvidenceError(f"npm locked installation is missing {name}@{version}")

    components: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    reference_by_key: dict[tuple[str, str], str] = {}
    for key in sorted(observed, key=lambda item: (item[0].lower(), item[1])):
        name, version = key
        source = observed[key]
        conclusion = _cyclonedx_license(source)
        lock_conclusion = _normalize_license(locked[key].get("license"))
        if (
            conclusion is not None
            and lock_conclusion is not None
            and conclusion != lock_conclusion
        ):
            raise EvidenceError(
                f"npm license metadata differs from package-lock.json: {name}"
            )
        conclusion = conclusion or lock_conclusion
        bom_ref = _npm_bom_ref(name, version)
        reference_by_key[key] = bom_ref
        normalized_component: dict[str, Any] = {
            "bom-ref": bom_ref,
            "type": "library",
            "name": name,
            "version": version,
            "purl": bom_ref,
        }
        scope = source.get("scope")
        if scope in {"excluded", "optional", "required"}:
            normalized_component["scope"] = scope
        if conclusion is not None:
            normalized_component["licenses"] = _license_block(conclusion)
        hashes = _safe_hashes(source)
        if hashes:
            normalized_component["hashes"] = hashes
        components.append(normalized_component)
        inventory.append(
            {
                "ecosystem": "npm",
                "name": name,
                "version": version,
                "license": conclusion,
            }
        )

    source_metadata = source_sbom.get("metadata")
    original_root_ref: str | None = None
    if isinstance(source_metadata, Mapping):
        source_root = source_metadata.get("component")
        if isinstance(source_root, Mapping) and isinstance(
            source_root.get("bom-ref"), str
        ):
            original_root_ref = source_root["bom-ref"]
    root_ref = _npm_bom_ref(root_name, root_version)
    original_ref_to_new: dict[str, str] = {
        original: reference_by_key[key] for original, key in original_ref_to_key.items()
    }
    if original_root_ref is not None:
        original_ref_to_new[original_root_ref] = root_ref
    dependencies: dict[str, set[str]] = {
        reference: set() for reference in [root_ref, *reference_by_key.values()]
    }
    source_dependencies = source_sbom.get("dependencies", [])
    if not isinstance(source_dependencies, list):
        raise EvidenceError("npm sbom dependency graph is invalid")
    for dependency in source_dependencies:
        if not isinstance(dependency, Mapping) or not isinstance(
            dependency.get("ref"), str
        ):
            raise EvidenceError("npm sbom dependency edge is invalid")
        normalized_ref = original_ref_to_new.get(dependency["ref"])
        if normalized_ref is None:
            continue
        depends_on = dependency.get("dependsOn", [])
        if not isinstance(depends_on, list):
            raise EvidenceError("npm sbom dependency targets are invalid")
        for target in depends_on:
            if not isinstance(target, str) or target not in original_ref_to_new:
                raise EvidenceError(
                    "npm sbom dependency references an unknown component"
                )
            dependencies[normalized_ref].add(original_ref_to_new[target])
    normalized_dependencies = [
        {"ref": reference, "dependsOn": sorted(targets)}
        for reference, targets in sorted(dependencies.items())
    ]

    sbom = {
        "$schema": "http://cyclonedx.org/schema/bom-1.5.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "bom-ref": root_ref,
                "type": "application",
                "name": root_name,
                "version": root_version,
                "licenses": _license_block("Apache-2.0"),
                "purl": root_ref,
            },
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "auris-supply-chain-evidence",
                        "version": GENERATOR_VERSION,
                    }
                ]
            },
        },
        "components": components,
        "dependencies": normalized_dependencies,
    }
    return sbom, inventory


def apply_license_policy(
    inventory: list[dict[str, Any]],
    exceptions: Mapping[tuple[str, str, str], dict[str, str]],
) -> list[dict[str, Any]]:
    consumed: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for item in inventory:
        ecosystem = str(item["ecosystem"])
        name = str(item["name"])
        version = str(item["version"])
        key = _component_key(ecosystem, name, version)
        conclusion = _normalize_license(item.get("license"))
        record: dict[str, Any] = {
            "ecosystem": ecosystem,
            "name": name,
            "version": version,
            "license": conclusion,
        }
        approved_identifiers = (
            _approved_license_expression(conclusion) if conclusion is not None else None
        )
        if approved_identifiers is None:
            exception = exceptions.get(key)
            if exception is None:
                raise EvidenceError(
                    "dependency license is not an approved SPDX expression and has "
                    "no exact reviewed exception: "
                    f"{ecosystem}:{name}@{version}"
                )
            consumed.add(key)
            record["license_status"] = "reviewed-exception"
            record["obligations"] = sorted(_REVIEW_EXCEPTION_OBLIGATIONS)
            record["review_exception"] = {
                field: exception[field]
                for field in (
                    "reason",
                    "reviewed_by",
                    "reviewed_on",
                    "review_reference",
                    "expires_on",
                )
            }
        else:
            record["license_status"] = "approved-compatible"
            record["obligations"] = _license_obligations(approved_identifiers)
        result.append(record)
    unused = set(exceptions) - consumed
    if unused:
        ecosystem, name, version = sorted(unused)[0]
        raise EvidenceError(
            f"unused license review exception: {ecosystem}:{name}@{version}"
        )
    return sorted(
        result,
        key=lambda item: (
            item["ecosystem"],
            canonical_name(item["name"]),
            item["version"],
        ),
    )


def _json_bytes(document: Any) -> bytes:
    validate_evidence_document(document)
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def generate_evidence(
    *,
    root: Path,
    backend_uv_lock_path: Path,
    dagster_uv_lock_path: Path,
    package_lock_path: Path,
    output_dir: Path,
    exceptions_path: Path | None,
    backend_python_executable: Path,
    dagster_python_executable: Path,
    uv_executable: str,
    npm_executable: str,
    runner: Runner = default_runner,
    today: date | None = None,
    source_commit: str,
) -> dict[str, Any]:
    normalized_source_commit = source_commit.strip().lower()
    if COMMIT_PATTERN.fullmatch(normalized_source_commit) is None:
        raise EvidenceError("source commit must be an exact Git object id")
    effective_today = today or date.today()
    for path, description in (
        (backend_uv_lock_path, "backend uv lock file"),
        (dagster_uv_lock_path, "Dagster uv lock file"),
        (package_lock_path, "npm package lock"),
    ):
        if not path.is_file():
            raise EvidenceError(f"missing {description}")
        _relative_input_path(path, root)
    if exceptions_path is not None:
        _relative_input_path(exceptions_path, root)

    exceptions = load_review_exceptions(exceptions_path, today=effective_today)
    backend_python_sbom, backend_python_inventory = build_python_evidence(
        ecosystem="backend-python",
        uv_lock_path=backend_uv_lock_path,
        backend_dir=backend_uv_lock_path.parent,
        python_executable=backend_python_executable,
        uv_executable=uv_executable,
        runner=runner,
    )
    dagster_python_sbom, dagster_python_inventory = build_python_evidence(
        ecosystem="dagster-python",
        uv_lock_path=dagster_uv_lock_path,
        backend_dir=dagster_uv_lock_path.parent,
        python_executable=dagster_python_executable,
        uv_executable=uv_executable,
        runner=runner,
    )
    npm_sbom, npm_inventory = build_npm_evidence(
        package_lock_path=package_lock_path,
        frontend_dir=package_lock_path.parent,
        npm_executable=npm_executable,
        runner=runner,
    )
    dependencies = apply_license_policy(
        backend_python_inventory + dagster_python_inventory + npm_inventory,
        exceptions,
    )
    license_inventory = {
        "schema_version": INVENTORY_SCHEMA,
        "dependencies": dependencies,
        "policy": {
            "allowed_expression_operators": ["AND", "OR"],
            "allowed_license_identifiers": sorted(_APPROVED_LICENSE_IDENTIFIERS),
            "denied_without_exact_review_exception": [
                "license-outside-allowlist",
                "missing-or-unknown-license",
                "non-spdx-or-ambiguous-license",
                "spdx-license-exception",
            ],
            "review_exception_scope": "exact-ecosystem-name-version",
            "review_exception_schema": EXCEPTION_SCHEMA,
        },
    }

    artifact_documents = {
        "backend-python.cdx.json": backend_python_sbom,
        "dagster-python.cdx.json": dagster_python_sbom,
        "dependency-licenses.json": license_inventory,
        "npm.cdx.json": npm_sbom,
    }
    artifact_bytes = {
        name: _json_bytes(document) for name, document in artifact_documents.items()
    }
    input_paths = [backend_uv_lock_path, dagster_uv_lock_path, package_lock_path]
    if exceptions_path is not None:
        input_paths.append(exceptions_path)
    component_counts = {
        "backend-python": len(backend_python_inventory),
        "dagster-python": len(dagster_python_inventory),
        "npm": len(npm_inventory),
        "total": (
            len(backend_python_inventory)
            + len(dagster_python_inventory)
            + len(npm_inventory)
        ),
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "source_commit": normalized_source_commit,
        "generator": {
            "name": "auris-supply-chain-evidence",
            "version": GENERATOR_VERSION,
        },
        "component_counts": component_counts,
        "source_inputs": [
            {
                "path": _relative_input_path(path, root),
                "sha256": sha256_file(path),
            }
            for path in sorted(
                input_paths, key=lambda item: _relative_input_path(item, root)
            )
        ],
        "artifacts": [
            {"path": name, "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in sorted(artifact_bytes.items())
        ],
    }
    manifest_bytes = _json_bytes(manifest)

    output_dir.mkdir(parents=True, exist_ok=True)
    legacy_combined_python_sbom = output_dir / "python.cdx.json"
    if legacy_combined_python_sbom.is_file():
        legacy_combined_python_sbom.unlink()
    for name, content in sorted(artifact_bytes.items()):
        (output_dir / name).write_bytes(content)
    (output_dir / "evidence-manifest.json").write_bytes(manifest_bytes)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate deterministic CycloneDX SBOMs and a fail-closed dependency "
            "license inventory from synchronized locked installations."
        )
    )
    parser.add_argument("--backend-uv-lock", type=Path, default=DEFAULT_BACKEND_UV_LOCK)
    parser.add_argument("--dagster-uv-lock", type=Path, default=DEFAULT_DAGSTER_UV_LOCK)
    parser.add_argument("--package-lock", type=Path, default=DEFAULT_PACKAGE_LOCK)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--backend-python", type=Path, default=DEFAULT_BACKEND_PYTHON)
    parser.add_argument("--dagster-python", type=Path, default=DEFAULT_DAGSTER_PYTHON)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--npm", default="npm")
    parser.add_argument(
        "--source-commit",
        help="Exact source commit; defaults to the current repository HEAD.",
    )
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=DEFAULT_EXCEPTIONS,
        help="Reviewed, expiring license exception policy JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        source_commit = args.source_commit
        if source_commit is None:
            source_commit = subprocess.run(
                ("git", "rev-parse", "--verify", "HEAD^{commit}"),
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        manifest = generate_evidence(
            root=ROOT,
            backend_uv_lock_path=args.backend_uv_lock,
            dagster_uv_lock_path=args.dagster_uv_lock,
            package_lock_path=args.package_lock,
            output_dir=args.output,
            exceptions_path=args.exceptions,
            backend_python_executable=args.backend_python,
            dagster_python_executable=args.dagster_python,
            uv_executable=args.uv,
            npm_executable=args.npm,
            source_commit=source_commit,
        )
    except (EvidenceError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Supply-chain evidence failed closed: {exc}", file=sys.stderr)
        return 1
    counts = manifest["component_counts"]
    try:
        output_label = args.output.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        output_label = args.output.name
    print(
        "Supply-chain evidence ok: "
        f"{counts['backend-python']} backend Python + "
        f"{counts['dagster-python']} Dagster Python + "
        f"{counts['npm']} npm dependencies; "
        f"output={output_label}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
