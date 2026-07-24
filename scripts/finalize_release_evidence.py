#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from verify_production_path_gate import (  # noqa: E402
    validate_evidence as validate_production_path_evidence,
)
from verify_backup_restore_gate import (  # noqa: E402
    FormalEvidenceError,
    validate_evidence as validate_backup_restore_evidence,
    validate_release_bindings as validate_backup_restore_release_bindings,
    verify_signed_release_bundle,
    verify_sigstore_attestation as verify_backup_restore_sigstore_attestation,
)


ROOT = SCRIPTS_DIR.parent
DEFAULT_EVIDENCE_DIR = ROOT / "build" / "release-evidence"
FINAL_MANIFEST = "release-gate-manifest.json"
COMMIT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
WINDOWS_ABSOLUTE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024 * 1024
SUPPLY_SOURCE_INPUTS = frozenset(
    {
        "backend/uv.lock",
        "config/release/exact-artifact-license-conclusions.json",
        "config/release/license-review-exceptions.json",
        "production/dagster/uv.lock",
        "prototype/auris-flow-ui/package-lock.json",
    }
)
APPROVED_LICENSE_IDENTIFIERS = frozenset(
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
BASE_LICENSE_OBLIGATION = "retain-upstream-license-and-copyright-notices"
LICENSE_OBLIGATIONS = {
    "Apache-2.0": frozenset({"preserve-apache-notice-and-state-changes"}),
    "MPL-2.0": frozenset({"publish-modified-mpl-covered-file-source"}),
    "PSF-2.0": frozenset({"include-psf-license-and-change-summary"}),
}
REVIEW_EXCEPTION_OBLIGATIONS = frozenset(
    {
        "comply-with-reviewed-exception-and-upstream-license-terms",
        BASE_LICENSE_OBLIGATION,
    }
)
LICENSE_POLICY = {
    "allowed_expression_operators": ["AND", "OR"],
    "allowed_license_identifiers": sorted(APPROVED_LICENSE_IDENTIFIERS),
    "denied_without_exact_conclusion_or_review_exception": [
        "license-outside-allowlist",
        "missing-or-unknown-license",
        "non-spdx-or-ambiguous-license",
        "spdx-license-exception",
    ],
    "exact_artifact_conclusion_scope": (
        "all-locked-artifacts-for-exact-ecosystem-name-version-sha256"
    ),
    "exact_artifact_conclusion_schema": ("auris.exact-artifact-license-conclusions.v2"),
    "exact_artifact_license_text_scope": (
        "repository-relative-nonempty-regular-file-sha256"
    ),
    "review_exception_scope": "exact-ecosystem-name-version",
    "review_exception_schema": "auris.license-review-exceptions.v1",
}
SPDX_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*|[()]")
GITHUB_ACTIONS_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
OFFICIAL_VISUAL_REPOSITORY = ("g5n-dev", "auris_flow")
VISUAL_OCI_REF_PATTERN = re.compile(
    r"ghcr\.io/(?P<owner>[a-z0-9._-]+)/(?P<repository>[a-z0-9._-]+)/"
    r"visual-baseline@(?P<digest>sha256:[0-9a-f]{64})"
)
VISUAL_SIGNATURE_IDENTITY_PATTERN = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repository>[A-Za-z0-9_.-]+)/"
    r"\.github/workflows/visual-baseline-build\.yml@refs/heads/"
    r"(?P<branch>[A-Za-z0-9._/-]+)"
)
FRONTEND_CANDIDATE_OCI_REF_PATTERN = re.compile(
    r"ghcr\.io/g5n-dev/auris_flow/frontend-bundle-candidate@"
    r"(?P<digest>sha256:[0-9a-f]{64})"
)
FRONTEND_APPROVAL_OCI_REF_PATTERN = re.compile(
    r"ghcr\.io/g5n-dev/auris_flow/frontend-bundle-approval@"
    r"(?P<digest>sha256:[0-9a-f]{64})"
)
FRONTEND_CANDIDATE_SIGNATURE_IDENTITY_PATTERN = re.compile(
    r"https://github\.com/g5n-dev/auris_flow/\.github/workflows/"
    r"frontend-bundle-candidate\.yml@refs/heads/(?P<branch>[A-Za-z0-9._/-]+)"
)
FRONTEND_APPROVAL_SIGNATURE_IDENTITY_PATTERN = re.compile(
    r"https://github\.com/g5n-dev/auris_flow/\.github/workflows/"
    r"frontend-bundle-promotion\.yml@refs/heads/(?P<branch>[A-Za-z0-9._/-]+)"
)
FRONTEND_BUNDLE_VERIFIED_CHECKS = frozenset(
    {
        "approval-cosign-signature",
        "approval-statement-binding",
        "approved-lock",
        "candidate-cosign-signature",
        "candidate-lock-binding",
        "candidate-oci-provenance",
        "candidate-source-ancestor",
        "current-release-build-binding",
        "exact-candidate-payload",
        "frontend-subtree-unchanged",
    }
)
RELEASE_MARKER_ENVIRONMENT = {
    "implementation_name": sys.implementation.name,
    "os_name": os.name,
    "platform_machine": platform.machine(),
    "platform_python_implementation": platform.python_implementation(),
    "python_full_version": platform.python_version(),
    "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
    "sys_platform": sys.platform,
}
VERSION_MARKER_NAMES = frozenset({"python_full_version", "python_version"})
REVIEW_EXCEPTION_FIELDS = frozenset(
    {
        "reason",
        "reviewed_by",
        "reviewed_on",
        "review_reference",
        "expires_on",
    }
)
COMMITTED_REVIEW_EXCEPTION_FIELDS = REVIEW_EXCEPTION_FIELDS | {
    "ecosystem",
    "name",
    "version",
}
COMMITTED_ARTIFACT_CONCLUSION_FIELDS = frozenset(
    {
        "artifact_sha256",
        "concluded_license",
        "declared_license",
        "ecosystem",
        "license_text_path",
        "license_text_sha256",
        "name",
        "version",
    }
)
ARTIFACT_CONCLUSION_EVIDENCE_FIELDS = frozenset(
    {
        "artifact_sha256s",
        "kind",
        "license_text_path",
        "license_text_sha256",
    }
)
REQUIREMENT_PATTERN = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)=="
    r"(?P<version>[A-Za-z0-9][A-Za-z0-9.+!_-]*)"
    r"(?:\s*;\s*(?P<marker>.+))?"
)
REQUIREMENT_HASH_PATTERN = re.compile(r"--hash=sha256:(?P<digest>[0-9a-f]{64})")

SUPPLY_ARTIFACTS = frozenset(
    {
        "backend-python.cdx.json",
        "dagster-python.cdx.json",
        "dependency-licenses.json",
        "npm.cdx.json",
    }
)
CORE_EVIDENCE = frozenset(
    {
        "clean-clone.json",
        "frontend-bundle.json",
        "visual-regression.json",
        "real-stack-gate.json",
        "real-dagster-gate.json",
        "product-dagster-gate.json",
        "production-path-gate.json",
        "backup-restore-gate.json",
        "backup-restore-gate.sigstore.json",
        "evidence-manifest.json",
    }
)
OPTIONAL_RELEASE_EVIDENCE = frozenset(
    {
        "backend-runtime-requirements.txt",
        "dagster-runtime-requirements.txt",
        "backend-python-audit.json",
        "dagster-python-audit.json",
        "npm-audit.json",
    }
)
ALLOWED_EVIDENCE = (
    SUPPLY_ARTIFACTS | CORE_EVIDENCE | OPTIONAL_RELEASE_EVIDENCE | {FINAL_MANIFEST}
)


class EvidenceError(RuntimeError):
    """A release-evidence integrity failure safe to show in CI output."""


PackageIdentity = tuple[str, str]


class SupplyExpectations(NamedTuple):
    component_identities: dict[str, frozenset[PackageIdentity]]
    python_runtime_identities: dict[str, frozenset[PackageIdentity]]
    python_hashes: dict[str, dict[PackageIdentity, frozenset[str]]]
    npm_lock_entry_count: int


def _normalize_commit(value: str) -> str:
    normalized = value.strip().lower()
    if COMMIT_PATTERN.fullmatch(normalized) is None:
        raise EvidenceError("source_commit must be an exact Git object id")
    return normalized


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"evidence JSON contains duplicate key: {key}")
        result[key] = value
    return result


def _read_regular_bytes(path: Path, *, size_limit: int = MAX_JSON_BYTES) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvidenceError(
            f"required evidence is not a regular file: {path.name}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise EvidenceError(
                f"release evidence is not a non-empty regular file: {path.name}"
            )
        if before.st_size > size_limit:
            raise EvidenceError(f"evidence file exceeds the size limit: {path.name}")
        chunks: list[bytes] = []
        remaining = size_limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(raw) > size_limit:
            raise EvidenceError(f"evidence file exceeds the size limit: {path.name}")
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or len(raw) != before.st_size:
            raise EvidenceError(f"evidence changed while being read: {path.name}")
        return raw
    finally:
        os.close(descriptor)


def _load_json_bytes(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except EvidenceError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"evidence JSON is invalid: {label}") from exc
    if not isinstance(payload, dict):
        raise EvidenceError(f"evidence JSON must be an object: {label}")
    _reject_local_absolute_paths(payload, label=label)
    return payload


def _load_json_object(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = _read_regular_bytes(path)
    payload = _load_json_bytes(raw, label=path.name)
    return payload, raw


def _reject_local_absolute_paths(value: Any, *, label: str) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _reject_local_absolute_paths(nested, label=label)
        return
    if isinstance(value, list):
        for nested in value:
            _reject_local_absolute_paths(nested, label=label)
        return
    if not isinstance(value, str):
        return
    normalized = value.strip()
    if (
        normalized.startswith(("/", "~/", "file://"))
        or WINDOWS_ABSOLUTE_PATTERN.match(normalized) is not None
    ):
        raise EvidenceError(f"evidence contains a local absolute path: {label}")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _require_exact_fields(
    payload: dict[str, Any], *, filename: str, fields: frozenset[str]
) -> None:
    actual = set(payload)
    if actual != fields:
        missing = sorted(fields - actual)
        unexpected = sorted(actual - fields)
        raise EvidenceError(
            f"{filename} fields are invalid; missing={missing}, unexpected={unexpected}"
        )


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _canonical_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _normalize_license_text_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise EvidenceError(
            "exact-artifact license text path must be repository-relative"
        )
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.parts[:2] != ("third_party", "licenses")
        or len(relative.parts) < 3
    ):
        raise EvidenceError(
            "exact-artifact license text path must be under third_party/licenses"
        )
    return relative.as_posix()


def _read_repository_source_bytes(
    repository_root: Path,
    relative_path: str,
) -> bytes:
    try:
        root = repository_root.resolve(strict=True)
    except OSError as exc:
        raise EvidenceError("supply-chain repository root does not exist") from exc
    cursor = root
    parts = PurePosixPath(relative_path).parts
    for index, part in enumerate(parts):
        cursor /= part
        try:
            metadata = cursor.lstat()
        except OSError as exc:
            raise EvidenceError(
                f"supply-chain source input does not exist: {relative_path}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            if index == len(parts) - 1:
                raise EvidenceError(
                    "supply-chain source input must be a regular file, not a "
                    f"symlink: {relative_path}"
                )
            raise EvidenceError(
                f"supply-chain source input path contains a symlink: {relative_path}"
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise EvidenceError(
                f"supply-chain source input parent is not a directory: {relative_path}"
            )
    try:
        cursor.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise EvidenceError(
            f"supply-chain source input escapes the repository: {relative_path}"
        ) from exc
    return _read_regular_bytes(cursor, size_limit=MAX_TEXT_BYTES)


def _marker_operand(
    node: ast.expr,
    *,
    environment: dict[str, str],
) -> tuple[str, bool]:
    if isinstance(node, ast.Name):
        if node.id not in environment:
            raise EvidenceError(f"uv.lock uses an unsupported marker name: {node.id}")
        return environment[node.id], node.id in VERSION_MARKER_NAMES
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, False
    raise EvidenceError("uv.lock marker operands must be names or quoted strings")


def _numeric_version(value: str) -> tuple[int, ...]:
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value) is None:
        raise EvidenceError("uv.lock Python version marker is unsupported")
    parts = [int(part) for part in value.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _compare_marker_values(
    left: str,
    right: str,
    *,
    operator: ast.cmpop,
    version_comparison: bool,
) -> bool:
    if isinstance(operator, (ast.In, ast.NotIn)):
        result = left in right
        return not result if isinstance(operator, ast.NotIn) else result

    comparable_left: Any = left
    comparable_right: Any = right
    if version_comparison:
        left_version = _numeric_version(left)
        right_version = _numeric_version(right)
        width = max(len(left_version), len(right_version))
        comparable_left = left_version + (0,) * (width - len(left_version))
        comparable_right = right_version + (0,) * (width - len(right_version))

    if isinstance(operator, ast.Eq):
        return comparable_left == comparable_right
    if isinstance(operator, ast.NotEq):
        return comparable_left != comparable_right
    if isinstance(operator, ast.Lt):
        return comparable_left < comparable_right
    if isinstance(operator, ast.LtE):
        return comparable_left <= comparable_right
    if isinstance(operator, ast.Gt):
        return comparable_left > comparable_right
    if isinstance(operator, ast.GtE):
        return comparable_left >= comparable_right
    raise EvidenceError("uv.lock uses an unsupported marker comparison")


def _evaluate_marker_node(node: ast.expr, *, environment: dict[str, str]) -> bool:
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        values = [
            _evaluate_marker_node(value, environment=environment)
            for value in node.values
        ]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise EvidenceError("uv.lock chained markers are unsupported")
        left, left_is_version = _marker_operand(node.left, environment=environment)
        right, right_is_version = _marker_operand(
            node.comparators[0], environment=environment
        )
        return _compare_marker_values(
            left,
            right,
            operator=node.ops[0],
            version_comparison=left_is_version or right_is_version,
        )
    raise EvidenceError("uv.lock marker expression is unsupported")


def _marker_applies(expression: object, *, extras: set[str]) -> bool:
    if expression is None:
        return True
    if not isinstance(expression, str) or not expression.strip():
        raise EvidenceError("uv.lock dependency marker is invalid")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise EvidenceError("uv.lock dependency marker is invalid") from exc
    contexts = extras or {""}
    for extra in contexts:
        environment = {**RELEASE_MARKER_ENVIRONMENT, "extra": extra}
        if _evaluate_marker_node(tree.body, environment=environment):
            return True
    return False


def _locked_package_hashes(package: dict[str, Any], *, filename: str) -> frozenset[str]:
    artifacts: list[object] = []
    if "sdist" in package:
        artifacts.append(package["sdist"])
    wheels = package.get("wheels", [])
    if not isinstance(wheels, list):
        raise EvidenceError(f"{filename} package wheels are invalid")
    artifacts.extend(wheels)
    hashes: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise EvidenceError(f"{filename} package artifact is invalid")
        digest = artifact.get("hash")
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        ):
            raise EvidenceError(f"{filename} package artifact hash is invalid")
        hashes.add(digest)
    if not hashes:
        raise EvidenceError(f"{filename} package has no SHA-256 locked artifacts")
    return frozenset(hashes)


def _uv_dependency_closure(
    root: dict[str, Any],
    packages: dict[str, dict[str, Any]],
    *,
    include_root_extras: bool,
    filename: str,
) -> frozenset[PackageIdentity]:
    root_optional = root.get("optional-dependencies", {})
    if not isinstance(root_optional, dict) or not all(
        isinstance(name, str) and isinstance(entries, list)
        for name, entries in root_optional.items()
    ):
        raise EvidenceError(f"{filename} root optional dependencies are invalid")
    root_extras = set(root_optional) if include_root_extras else set()
    queue: list[tuple[str, frozenset[str]]] = []

    def enqueue(dependency: object, *, contexts: set[str]) -> None:
        if not isinstance(dependency, dict) or not set(dependency).issubset(
            {"name", "marker", "extra"}
        ):
            raise EvidenceError(f"{filename} dependency entry is invalid")
        name = dependency.get("name")
        if not _nonempty_text(name):
            raise EvidenceError(f"{filename} dependency name is invalid")
        if not _marker_applies(dependency.get("marker"), extras=contexts):
            return
        requested_extras = dependency.get("extra", [])
        if not isinstance(requested_extras, list) or not all(
            _nonempty_text(item) for item in requested_extras
        ):
            raise EvidenceError(f"{filename} dependency extras are invalid")
        queue.append(
            (
                _canonical_package_name(str(name)),
                frozenset(str(item) for item in requested_extras),
            )
        )

    root_dependencies = root.get("dependencies", [])
    if not isinstance(root_dependencies, list):
        raise EvidenceError(f"{filename} root dependencies are invalid")
    for dependency in root_dependencies:
        enqueue(dependency, contexts={"", *root_extras})
    if include_root_extras:
        for extra, dependencies in root_optional.items():
            for dependency in dependencies:
                enqueue(dependency, contexts={extra})

    activated_extras: dict[str, frozenset[str]] = {}
    identities: dict[str, str] = {}
    while queue:
        name, requested_extras = queue.pop()
        package = packages.get(name)
        if package is None:
            raise EvidenceError(
                f"{filename} closure references an absent package: {name}"
            )
        previous_extras = activated_extras.get(name)
        if previous_extras is not None and requested_extras.issubset(previous_extras):
            continue
        merged_extras = (previous_extras or frozenset()) | requested_extras
        activated_extras[name] = merged_extras
        identities[name] = str(package["version"])
        contexts = {"", *merged_extras}

        dependencies = package.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise EvidenceError(f"{filename} package dependencies are invalid")
        for dependency in dependencies:
            enqueue(dependency, contexts=contexts)

        optional = package.get("optional-dependencies", {})
        if not isinstance(optional, dict):
            raise EvidenceError(f"{filename} package optional dependencies are invalid")
        unknown_extras = set(merged_extras) - set(optional)
        if unknown_extras:
            raise EvidenceError(
                f"{filename} requests an undefined package extra: "
                + ", ".join(sorted(unknown_extras))
            )
        for extra in merged_extras:
            optional_dependencies = optional[extra]
            if not isinstance(optional_dependencies, list):
                raise EvidenceError(
                    f"{filename} package optional dependency list is invalid"
                )
            for dependency in optional_dependencies:
                enqueue(dependency, contexts={extra})

    return frozenset(identities.items())


def _parse_uv_lock(
    raw: bytes,
    *,
    filename: str,
) -> tuple[
    frozenset[PackageIdentity],
    frozenset[PackageIdentity],
    dict[PackageIdentity, frozenset[str]],
]:
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise EvidenceError(f"{filename} is not a valid uv.lock") from exc
    package_entries = document.get("package") if isinstance(document, dict) else None
    if document.get("version") != 1 or not isinstance(package_entries, list):
        raise EvidenceError(f"{filename} package graph is invalid")

    roots: list[dict[str, Any]] = []
    packages: dict[str, dict[str, Any]] = {}
    hashes_by_name: dict[str, frozenset[str]] = {}
    for package in package_entries:
        if not isinstance(package, dict):
            raise EvidenceError(f"{filename} package entry is invalid")
        name = package.get("name")
        version = package.get("version")
        source = package.get("source")
        if (
            not _nonempty_text(name)
            or not _nonempty_text(version)
            or not isinstance(source, dict)
        ):
            raise EvidenceError(f"{filename} package identity is invalid")
        if "virtual" in source or "editable" in source:
            roots.append(package)
            continue
        if "registry" not in source:
            raise EvidenceError(f"{filename} contains a non-registry dependency")
        canonical_name = _canonical_package_name(str(name))
        if canonical_name in packages:
            raise EvidenceError(f"{filename} contains a duplicate package name")
        packages[canonical_name] = package
        hashes_by_name[canonical_name] = _locked_package_hashes(
            package, filename=filename
        )
    if len(roots) != 1 or not packages:
        raise EvidenceError(f"{filename} must contain one project and dependencies")

    all_extras = _uv_dependency_closure(
        roots[0],
        packages,
        include_root_extras=True,
        filename=filename,
    )
    runtime = _uv_dependency_closure(
        roots[0],
        packages,
        include_root_extras=False,
        filename=filename,
    )
    if not all_extras or not runtime or not runtime.issubset(all_extras):
        raise EvidenceError(f"{filename} runtime dependency closure is invalid")
    hashes = {
        (name, str(package["version"])): hashes_by_name[name]
        for name, package in packages.items()
    }
    return all_extras, runtime, hashes


def _parse_npm_lock(
    raw: bytes,
    *,
    filename: str,
) -> tuple[frozenset[PackageIdentity], int]:
    document = _load_json_bytes(raw, label=filename)
    packages = document.get("packages")
    if (
        document.get("lockfileVersion") != 3
        or not isinstance(packages, dict)
        or not isinstance(packages.get(""), dict)
    ):
        raise EvidenceError(f"{filename} must be a package-lock v3 graph")
    identities: set[PackageIdentity] = set()
    package_count = 0
    npm_name_pattern = re.compile(
        r"(?:@[A-Za-z0-9._-]+/[A-Za-z0-9._-]+|[A-Za-z0-9._-]+)"
    )
    for path, package in packages.items():
        if path == "":
            continue
        package_count += 1
        if (
            not isinstance(path, str)
            or "node_modules/" not in path
            or not isinstance(package, dict)
        ):
            raise EvidenceError(f"{filename} package entry is invalid")
        name = path.rsplit("node_modules/", 1)[1]
        version = package.get("version")
        if npm_name_pattern.fullmatch(name) is None or not _nonempty_text(version):
            raise EvidenceError(f"{filename} package identity is invalid")
        identity = (_canonical_package_name(name), str(version))
        if identity in identities:
            raise EvidenceError(f"{filename} contains a duplicate package identity")
        identities.add(identity)
    if not identities or package_count != len(identities):
        raise EvidenceError(f"{filename} dependency closure is invalid")
    return frozenset(identities), package_count


def _spdx_license_identifiers(expression: str) -> frozenset[str]:
    tokens: list[str] = []
    position = 0
    while position < len(expression):
        if expression[position].isspace():
            position += 1
            continue
        match = SPDX_TOKEN_PATTERN.match(expression, position)
        if match is None:
            raise EvidenceError("dependency license expression is not valid SPDX")
        tokens.append(match.group(0))
        position = match.end()
    if not tokens:
        raise EvidenceError("dependency license expression is empty")

    position = 0
    identifiers: set[str] = set()

    def parse_factor() -> None:
        nonlocal position
        if position >= len(tokens):
            raise EvidenceError("dependency SPDX expression is incomplete")
        token = tokens[position]
        if token == "(":
            position += 1
            parse_or_expression()
            if position >= len(tokens) or tokens[position] != ")":
                raise EvidenceError("dependency SPDX expression is unbalanced")
            position += 1
            return
        if token in {"AND", "OR", "WITH", ")"}:
            raise EvidenceError("dependency SPDX expression operand is invalid")
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
        raise EvidenceError("dependency SPDX expression is unsupported or invalid")
    return frozenset(identifiers)


def _expected_license_obligations(identifiers: frozenset[str]) -> frozenset[str]:
    obligations = {BASE_LICENSE_OBLIGATION}
    for identifier in identifiers:
        obligations.update(LICENSE_OBLIGATIONS.get(identifier, ()))
    return frozenset(obligations)


def _require_common(
    payload: dict[str, Any],
    *,
    filename: str,
    schema_version: str | int,
    source_commit: str,
) -> None:
    if payload.get("schema_version") != schema_version:
        raise EvidenceError(f"{filename} schema_version is invalid")
    if payload.get("status") != "ok":
        raise EvidenceError(f"{filename} status is not ok")
    if payload.get("source_commit") != source_commit:
        raise EvidenceError(f"{filename} source_commit does not match the release")


def _validate_supply_manifest(
    evidence_dir: Path,
    payload: dict[str, Any],
    *,
    source_commit: str,
    repository_root: Path,
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    _require_exact_fields(
        payload,
        filename="evidence-manifest.json",
        fields=frozenset(
            {
                "schema_version",
                "source_commit",
                "generator",
                "component_counts",
                "source_inputs",
                "artifacts",
            }
        ),
    )
    if payload.get("schema_version") != "auris.release-evidence-manifest.v1":
        raise EvidenceError("evidence-manifest.json schema_version is invalid")
    if payload.get("source_commit") != source_commit:
        raise EvidenceError("evidence-manifest.json source_commit does not match")
    if payload.get("generator") != {
        "name": "auris-supply-chain-evidence",
        "version": "4",
    }:
        raise EvidenceError("evidence-manifest.json generator is invalid")
    component_counts = payload.get("component_counts")
    if (
        not isinstance(component_counts, dict)
        or set(component_counts) != {"backend-python", "dagster-python", "npm", "total"}
        or not all(_positive_int(component_counts.get(key)) for key in component_counts)
        or component_counts["total"]
        != component_counts["backend-python"]
        + component_counts["dagster-python"]
        + component_counts["npm"]
    ):
        raise EvidenceError("evidence-manifest.json component_counts are invalid")

    source_inputs = payload.get("source_inputs")
    if not isinstance(source_inputs, list):
        raise EvidenceError("evidence-manifest.json source_inputs must be an array")
    seen_inputs: set[str] = set()
    source_input_bytes: dict[str, bytes] = {}
    for item in source_inputs:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise EvidenceError("supply-chain source input entry is invalid")
        relative_path = item.get("path")
        expected_sha256 = item.get("sha256")
        if (
            not isinstance(relative_path, str)
            or (
                relative_path not in SUPPLY_SOURCE_INPUTS
                and _normalize_license_text_path(relative_path) != relative_path
            )
            or relative_path in seen_inputs
            or not isinstance(expected_sha256, str)
            or SHA256_PATTERN.fullmatch(expected_sha256) is None
        ):
            raise EvidenceError("supply-chain source input is invalid or duplicated")
        source_bytes = _read_repository_source_bytes(repository_root, relative_path)
        current_sha256 = _sha256_bytes(source_bytes)
        if current_sha256 != expected_sha256:
            raise EvidenceError(
                f"supply-chain source input sha256 mismatch: {relative_path}"
            )
        seen_inputs.add(relative_path)
        source_input_bytes[relative_path] = source_bytes
    try:
        conclusion_bytes = source_input_bytes[
            "config/release/exact-artifact-license-conclusions.json"
        ]
    except KeyError as exc:
        raise EvidenceError(
            "supply-chain source input manifest is missing the conclusion policy"
        ) from exc
    committed_conclusions = _load_committed_artifact_conclusions(
        conclusion_bytes,
        source_input_bytes=source_input_bytes,
    )
    expected_inputs = SUPPLY_SOURCE_INPUTS | {
        conclusion["license_text_path"] for conclusion in committed_conclusions.values()
    }
    if seen_inputs != expected_inputs:
        missing_inputs = ", ".join(sorted(expected_inputs - seen_inputs))
        unexpected_inputs = ", ".join(sorted(seen_inputs - expected_inputs))
        raise EvidenceError(
            "supply-chain source input manifest does not exactly match the "
            "committed policy; "
            f"missing={missing_inputs or '-'}, unexpected={unexpected_inputs or '-'}"
        )

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise EvidenceError("evidence-manifest.json artifacts must be an array")
    seen: set[str] = set()
    artifact_bytes: dict[str, bytes] = {}
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise EvidenceError("supply-chain artifact entry is invalid")
        relative_path = item.get("path")
        expected_sha256 = item.get("sha256")
        if (
            not isinstance(relative_path, str)
            or relative_path not in SUPPLY_ARTIFACTS
            or relative_path in seen
        ):
            raise EvidenceError("supply-chain artifact path is invalid or duplicated")
        if (
            not isinstance(expected_sha256, str)
            or SHA256_PATTERN.fullmatch(expected_sha256) is None
        ):
            raise EvidenceError("supply-chain artifact sha256 is invalid")
        raw = _read_regular_bytes(evidence_dir / relative_path)
        if _sha256_bytes(raw) != expected_sha256:
            raise EvidenceError(
                f"supply-chain artifact sha256 mismatch: {relative_path}"
            )
        artifact_bytes[relative_path] = raw
        seen.add(relative_path)
    if seen != SUPPLY_ARTIFACTS:
        missing = ", ".join(sorted(SUPPLY_ARTIFACTS - seen))
        raise EvidenceError(f"supply-chain manifest is incomplete: {missing}")
    return artifact_bytes, source_input_bytes


def _require_sha256(value: object, *, label: str, prefixed: bool = False) -> None:
    expected = r"sha256:[0-9a-f]{64}" if prefixed else r"[0-9a-f]{64}"
    if not isinstance(value, str) or re.fullmatch(expected, value) is None:
        raise EvidenceError(f"{label} is not a valid SHA-256")


def _require_timestamp(value: object, *, label: str) -> None:
    if not isinstance(value, str):
        raise EvidenceError(f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EvidenceError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise EvidenceError(f"{label} timestamp must include a timezone")


def _validate_clean_clone(payload: dict[str, Any], *, source_commit: str) -> None:
    filename = "clean-clone.json"
    _require_exact_fields(
        payload,
        filename=filename,
        fields=frozenset(
            {
                "completed_at",
                "git_object_isolation",
                "readiness_scope",
                "reproducibility_scope",
                "schema_version",
                "source_commit",
                "status",
                "toolchain",
                "verified_steps",
            }
        ),
    )
    _require_common(
        payload,
        filename=filename,
        schema_version="auris.clean-clone-evidence.v1",
        source_commit=source_commit,
    )
    if (
        payload.get("reproducibility_scope") != "functional-locked-source"
        or payload.get("git_object_isolation") != "clone-no-local-without-alternates"
    ):
        raise EvidenceError(f"{filename} isolation proof is invalid")
    if payload.get("readiness_scope") != "release":
        raise EvidenceError(f"{filename} readiness_scope must be release")
    toolchain = payload.get("toolchain")
    if (
        not isinstance(toolchain, dict)
        or set(toolchain) != {"node", "npm", "python_request", "uv"}
        or not all(_nonempty_text(value) for value in toolchain.values())
    ):
        raise EvidenceError(f"{filename} toolchain proof is invalid")
    expected_steps = {
        "locked-dependency-install",
        "database-migrations",
        "backend-tests-and-smoke",
        "dagster-tests",
        "frontend-build-and-bundle-policy",
        "release-readiness",
        "secret-history-scan",
        "final-clean-tree",
    }
    steps = payload.get("verified_steps")
    if (
        not isinstance(steps, list)
        or set(steps) != expected_steps
        or len(steps) != len(expected_steps)
    ):
        raise EvidenceError(f"{filename} verified_steps are incomplete")
    _require_timestamp(payload.get("completed_at"), label=filename)


def _validate_production_path(
    payload: dict[str, Any], *, source_commit: str, repository_root: Path
) -> None:
    errors = validate_production_path_evidence(
        payload,
        root=repository_root,
        expected_commit=source_commit,
    )
    if errors:
        raise EvidenceError("production path evidence is invalid: " + "; ".join(errors))


def _validate_backup_restore(
    payload: dict[str, Any], *, source_commit: str, repository_root: Path
) -> None:
    errors = validate_backup_restore_evidence(
        payload,
        root=repository_root,
        expected_commit=source_commit,
    )
    if errors:
        raise EvidenceError(
            "backup/restore evidence is invalid: " + "; ".join(errors)
        )


def _validate_visual(payload: dict[str, Any], *, source_commit: str) -> None:
    filename = "visual-regression.json"
    _require_exact_fields(
        payload,
        filename=filename,
        fields=frozenset(
            {
                "baseline_oci_digest",
                "baseline_oci_ref",
                "baseline_sha256",
                "baseline_source_commit",
                "job_workflow_sha",
                "kind",
                "manifest_sha256",
                "passed",
                "runner_contract_sha256",
                "scenario_count",
                "schema_version",
                "signature_identity",
                "signature_issuer",
                "source_commit",
                "status",
            }
        ),
    )
    _require_common(
        payload,
        filename=filename,
        schema_version=1,
        source_commit=source_commit,
    )
    if (
        payload.get("kind") != "auris-flow-visual-regression-evidence"
        or payload.get("scenario_count") != 76
        or payload.get("passed") != 76
    ):
        raise EvidenceError(f"{filename} does not prove all 76 scenarios")
    _require_sha256(payload.get("baseline_oci_digest"), label=filename, prefixed=True)
    for field in ("baseline_sha256", "manifest_sha256", "runner_contract_sha256"):
        _require_sha256(payload.get(field), label=f"{filename} {field}")
    baseline_commit = payload.get("baseline_source_commit")
    if (
        not isinstance(baseline_commit, str)
        or COMMIT_PATTERN.fullmatch(baseline_commit) is None
    ):
        raise EvidenceError(f"{filename} baseline_source_commit is invalid")
    job_workflow_sha = payload.get("job_workflow_sha")
    if (
        not isinstance(job_workflow_sha, str)
        or COMMIT_PATTERN.fullmatch(job_workflow_sha) is None
        or job_workflow_sha != baseline_commit
    ):
        raise EvidenceError(
            f"{filename} job_workflow_sha must equal baseline_source_commit"
        )
    reference = payload.get("baseline_oci_ref")
    reference_match = (
        VISUAL_OCI_REF_PATTERN.fullmatch(reference)
        if isinstance(reference, str)
        else None
    )
    if (
        reference_match is None
        or reference_match.group("digest") != payload["baseline_oci_digest"]
    ):
        raise EvidenceError(f"{filename} baseline OCI reference is invalid")
    reference_repository = (
        reference_match.group("owner").lower(),
        reference_match.group("repository").lower(),
    )
    if reference_repository != OFFICIAL_VISUAL_REPOSITORY:
        raise EvidenceError(
            f"{filename} baseline OCI reference is not from the "
            "official Auris Flow repository"
        )
    signature_identity = payload.get("signature_identity")
    if not isinstance(signature_identity, str):
        raise EvidenceError(f"{filename} signature identity is invalid")
    identity_match = VISUAL_SIGNATURE_IDENTITY_PATTERN.fullmatch(signature_identity)
    if (
        identity_match is None
        or ".." in signature_identity
        or "//" in signature_identity.removeprefix("https://")
        or signature_identity.endswith("/")
    ):
        raise EvidenceError(f"{filename} signature identity is invalid")
    identity_repository = (
        identity_match.group("owner").lower(),
        identity_match.group("repository").lower(),
    )
    if reference_repository != identity_repository:
        raise EvidenceError(
            f"{filename} OCI reference and signature identity repository differ"
        )
    if identity_repository != OFFICIAL_VISUAL_REPOSITORY:
        raise EvidenceError(
            f"{filename} signature identity is not from the "
            "official Auris Flow repository"
        )
    if payload.get("signature_issuer") != GITHUB_ACTIONS_OIDC_ISSUER:
        raise EvidenceError(f"{filename} signature issuer is invalid")


def _require_frontend_git_object(value: object, *, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise EvidenceError(f"{label} is not an exact Git object id")
    return value


def _require_frontend_workflow_identity(
    value: object,
    *,
    label: str,
    pattern: re.Pattern[str],
) -> str:
    if (
        not isinstance(value, str)
        or pattern.fullmatch(value) is None
        or ".." in value
        or "//" in value.removeprefix("https://")
        or value.endswith("/")
    ):
        raise EvidenceError(f"{label} is invalid")
    return value


def _require_frontend_totals(value: object, *, label: str) -> dict[str, int]:
    expected = {
        "allBrotliBytes",
        "allRawBytes",
        "jsBrotliBytes",
        "jsRawBytes",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise EvidenceError(f"{label} fields are invalid")
    if not all(
        isinstance(total, int)
        and not isinstance(total, bool)
        and 0 <= total <= (2**53 - 1)
        for total in value.values()
    ):
        raise EvidenceError(f"{label} values are invalid")
    return value


def _require_frontend_approval_reference(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value.strip()) < 8
        or re.search(
            r"(?:^|[-_\s])(?:pending|todo|tbd|placeholder|example|replace-me)"
            r"(?:$|[-_\s])",
            value.strip(),
            re.IGNORECASE,
        )
        is not None
    ):
        raise EvidenceError("frontend bundle approval reference is invalid")
    return value.strip()


def validate_frontend_bundle_lock(
    lock: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    _require_exact_fields(
        lock,
        filename="frontend bundle lock",
        fields=frozenset(
            {"approval", "candidate", "kind", "reason", "schema_version", "status"}
        ),
    )
    if (
        lock.get("schema_version") != 3
        or lock.get("kind") != "auris-flow-frontend-bundle-lock"
        or lock.get("status") != "APPROVED"
        or not _nonempty_text(lock.get("reason"))
    ):
        raise EvidenceError("frontend bundle lock is not an approved schema-v3 lock")

    candidate = lock.get("candidate")
    approval = lock.get("approval")
    if not isinstance(candidate, dict):
        raise EvidenceError("frontend bundle candidate must be an object")
    if not isinstance(approval, dict):
        raise EvidenceError("frontend bundle approval must be an object")
    _require_exact_fields(
        candidate,
        filename="frontend bundle candidate",
        fields=frozenset(
            {
                "artifact_ref",
                "brotli_manifest_sha256",
                "build_workflow_sha",
                "bundle_report_sha256",
                "candidate_sha256",
                "dist_inventory_sha256",
                "frontend_tree",
                "package_lock_sha256",
                "repository_tree",
                "signature_identity",
                "signature_issuer",
                "source_commit",
                "totals",
                "vite_manifest_sha256",
            }
        ),
    )
    _require_exact_fields(
        approval,
        filename="frontend bundle approval",
        fields=frozenset(
            {
                "approval_reference",
                "artifact_ref",
                "environment",
                "promotion_workflow_sha",
                "rebuild_evidence_sha256",
                "run_attempt",
                "run_id",
                "signature_identity",
                "signature_issuer",
                "statement_sha256",
            }
        ),
    )

    candidate_reference = candidate.get("artifact_ref")
    if (
        not isinstance(candidate_reference, str)
        or FRONTEND_CANDIDATE_OCI_REF_PATTERN.fullmatch(candidate_reference) is None
    ):
        raise EvidenceError(
            "frontend bundle candidate artifact must be an immutable official GHCR digest"
        )
    approval_reference = approval.get("artifact_ref")
    if (
        not isinstance(approval_reference, str)
        or FRONTEND_APPROVAL_OCI_REF_PATTERN.fullmatch(approval_reference) is None
    ):
        raise EvidenceError(
            "frontend bundle approval artifact must be an immutable official GHCR digest"
        )

    candidate_source = _require_frontend_git_object(
        candidate.get("source_commit"), label="frontend bundle candidate source_commit"
    )
    _require_frontend_git_object(
        candidate.get("repository_tree"),
        label="frontend bundle candidate repository_tree",
    )
    _require_frontend_git_object(
        candidate.get("frontend_tree"), label="frontend bundle candidate frontend_tree"
    )
    build_workflow_sha = _require_frontend_git_object(
        candidate.get("build_workflow_sha"),
        label="frontend bundle candidate build_workflow_sha",
    )
    if build_workflow_sha != candidate_source:
        raise EvidenceError(
            "frontend bundle candidate build_workflow_sha must equal source_commit"
        )
    promotion_workflow_sha = _require_frontend_git_object(
        approval.get("promotion_workflow_sha"),
        label="frontend bundle approval promotion_workflow_sha",
    )
    if promotion_workflow_sha != candidate_source:
        raise EvidenceError(
            "frontend bundle approval promotion_workflow_sha must equal candidate source_commit"
        )

    for field in (
        "brotli_manifest_sha256",
        "bundle_report_sha256",
        "candidate_sha256",
        "dist_inventory_sha256",
        "package_lock_sha256",
        "vite_manifest_sha256",
    ):
        _require_sha256(
            candidate.get(field), label=f"frontend bundle candidate {field}"
        )
    for field in ("rebuild_evidence_sha256", "statement_sha256"):
        _require_sha256(approval.get(field), label=f"frontend bundle approval {field}")
    _require_frontend_totals(candidate.get("totals"), label="frontend bundle totals")
    candidate_identity = _require_frontend_workflow_identity(
        candidate.get("signature_identity"),
        label="frontend bundle candidate signature identity",
        pattern=FRONTEND_CANDIDATE_SIGNATURE_IDENTITY_PATTERN,
    )
    approval_identity = _require_frontend_workflow_identity(
        approval.get("signature_identity"),
        label="frontend bundle approval signature identity",
        pattern=FRONTEND_APPROVAL_SIGNATURE_IDENTITY_PATTERN,
    )
    candidate_identity_match = FRONTEND_CANDIDATE_SIGNATURE_IDENTITY_PATTERN.fullmatch(
        candidate_identity
    )
    approval_identity_match = FRONTEND_APPROVAL_SIGNATURE_IDENTITY_PATTERN.fullmatch(
        approval_identity
    )
    assert candidate_identity_match is not None
    assert approval_identity_match is not None
    if candidate_identity_match.group("branch") != approval_identity_match.group(
        "branch"
    ):
        raise EvidenceError("frontend bundle candidate and approval branches differ")
    if candidate.get("signature_issuer") != GITHUB_ACTIONS_OIDC_ISSUER:
        raise EvidenceError("frontend bundle candidate signature issuer is invalid")
    if approval.get("signature_issuer") != GITHUB_ACTIONS_OIDC_ISSUER:
        raise EvidenceError("frontend bundle approval signature issuer is invalid")
    _require_frontend_approval_reference(approval.get("approval_reference"))
    if approval.get("environment") != "frontend-bundle-production":
        raise EvidenceError("frontend bundle approval environment is invalid")
    run_id = approval.get("run_id")
    if not isinstance(run_id, str) or re.fullmatch(r"[1-9][0-9]*", run_id) is None:
        raise EvidenceError("frontend bundle approval run_id is invalid")
    if not _positive_int(approval.get("run_attempt")):
        raise EvidenceError("frontend bundle approval run_attempt is invalid")
    return candidate, approval


def _verify_frontend_bundle_repository_binding(
    *,
    candidate_source_commit: str,
    candidate_repository_tree: str,
    frontend_tree: str,
    source_commit: str,
    repository_root: Path,
) -> None:
    try:
        ancestor = subprocess.run(
            (
                "git",
                "merge-base",
                "--is-ancestor",
                candidate_source_commit,
                source_commit,
            ),
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
        )
        resolved: dict[str, str] = {}
        for label, reference in (
            ("candidate repository tree", f"{candidate_source_commit}^{{tree}}"),
            (
                "candidate frontend subtree",
                f"{candidate_source_commit}:prototype/auris-flow-ui",
            ),
            ("release frontend subtree", f"{source_commit}:prototype/auris-flow-ui"),
        ):
            resolved[label] = subprocess.run(
                ("git", "rev-parse", "--verify", reference),
                cwd=repository_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EvidenceError(
            "unable to verify frontend bundle repository binding"
        ) from exc
    if ancestor.returncode != 0:
        raise EvidenceError(
            "frontend bundle candidate source commit is not an ancestor of the release"
        )
    if resolved["candidate repository tree"] != candidate_repository_tree:
        raise EvidenceError(
            "frontend bundle candidate repository tree does not match Git"
        )
    if resolved["candidate frontend subtree"] != frontend_tree:
        raise EvidenceError(
            "frontend bundle candidate frontend subtree does not match Git"
        )
    if resolved["release frontend subtree"] != frontend_tree:
        raise EvidenceError(
            "frontend bundle release frontend subtree changed after the candidate"
        )


def _validate_frontend_bundle(
    payload: dict[str, Any],
    *,
    source_commit: str,
    repository_root: Path,
    check_repository_binding: bool,
) -> None:
    filename = "frontend-bundle.json"
    _require_exact_fields(
        payload,
        filename=filename,
        fields=frozenset(
            {
                "approval_artifact_digest",
                "approval_artifact_ref",
                "approval_reference",
                "approval_signature_identity",
                "approval_signature_issuer",
                "approval_statement_sha256",
                "artifact_digest",
                "artifact_ref",
                "brotli_manifest_sha256",
                "build_workflow_sha",
                "bundle_report_sha256",
                "candidate_repository_tree",
                "candidate_sha256",
                "candidate_source_commit",
                "dist_inventory_sha256",
                "frontend_tree",
                "kind",
                "lock_sha256",
                "package_lock_sha256",
                "promotion_workflow_sha",
                "rebuild_evidence_sha256",
                "schema_version",
                "signature_identity",
                "signature_issuer",
                "source_commit",
                "status",
                "totals",
                "verified_checks",
                "vite_manifest_sha256",
            }
        ),
    )
    _require_common(
        payload,
        filename=filename,
        schema_version="auris.frontend-bundle-evidence.v1",
        source_commit=source_commit,
    )
    if payload.get("kind") != "auris-flow-frontend-bundle-evidence":
        raise EvidenceError(f"{filename} kind is invalid")

    candidate_reference = payload.get("artifact_ref")
    candidate_match = (
        FRONTEND_CANDIDATE_OCI_REF_PATTERN.fullmatch(candidate_reference)
        if isinstance(candidate_reference, str)
        else None
    )
    if candidate_match is None:
        raise EvidenceError(f"{filename} candidate artifact reference is invalid")
    approval_reference = payload.get("approval_artifact_ref")
    approval_match = (
        FRONTEND_APPROVAL_OCI_REF_PATTERN.fullmatch(approval_reference)
        if isinstance(approval_reference, str)
        else None
    )
    if approval_match is None:
        raise EvidenceError(f"{filename} approval artifact reference is invalid")
    if payload.get("artifact_digest") != candidate_match.group("digest"):
        raise EvidenceError(f"{filename} candidate artifact digest is invalid")
    if payload.get("approval_artifact_digest") != approval_match.group("digest"):
        raise EvidenceError(f"{filename} approval artifact digest is invalid")
    _require_frontend_workflow_identity(
        payload.get("signature_identity"),
        label=f"{filename} candidate signature identity",
        pattern=FRONTEND_CANDIDATE_SIGNATURE_IDENTITY_PATTERN,
    )
    _require_frontend_workflow_identity(
        payload.get("approval_signature_identity"),
        label=f"{filename} approval signature identity",
        pattern=FRONTEND_APPROVAL_SIGNATURE_IDENTITY_PATTERN,
    )
    if payload.get("signature_issuer") != GITHUB_ACTIONS_OIDC_ISSUER:
        raise EvidenceError(f"{filename} candidate signature issuer is invalid")
    if payload.get("approval_signature_issuer") != GITHUB_ACTIONS_OIDC_ISSUER:
        raise EvidenceError(f"{filename} approval signature issuer is invalid")
    for field in (
        "approval_statement_sha256",
        "brotli_manifest_sha256",
        "bundle_report_sha256",
        "candidate_sha256",
        "dist_inventory_sha256",
        "lock_sha256",
        "package_lock_sha256",
        "rebuild_evidence_sha256",
        "vite_manifest_sha256",
    ):
        _require_sha256(payload.get(field), label=f"{filename} {field}")
    for field in (
        "build_workflow_sha",
        "candidate_repository_tree",
        "candidate_source_commit",
        "frontend_tree",
        "promotion_workflow_sha",
    ):
        _require_frontend_git_object(payload.get(field), label=f"{filename} {field}")
    _require_frontend_approval_reference(payload.get("approval_reference"))
    _require_frontend_totals(payload.get("totals"), label=f"{filename} totals")
    verified_checks = payload.get("verified_checks")
    if (
        not isinstance(verified_checks, list)
        or len(verified_checks) != len(FRONTEND_BUNDLE_VERIFIED_CHECKS)
        or set(verified_checks) != FRONTEND_BUNDLE_VERIFIED_CHECKS
        or not all(isinstance(item, str) for item in verified_checks)
    ):
        raise EvidenceError(f"{filename} verified_checks are incomplete")

    lock_path = repository_root / "production/frontend/frontend-bundle.lock.json"
    lock, lock_raw = _load_json_object(lock_path)
    candidate, approval = validate_frontend_bundle_lock(lock)
    if payload.get("lock_sha256") != _sha256_bytes(lock_raw):
        raise EvidenceError(f"{filename} lock_sha256 does not match committed lock")
    candidate_bindings = {
        "artifact_ref": "artifact_ref",
        "brotli_manifest_sha256": "brotli_manifest_sha256",
        "build_workflow_sha": "build_workflow_sha",
        "bundle_report_sha256": "bundle_report_sha256",
        "candidate_repository_tree": "repository_tree",
        "candidate_sha256": "candidate_sha256",
        "candidate_source_commit": "source_commit",
        "dist_inventory_sha256": "dist_inventory_sha256",
        "frontend_tree": "frontend_tree",
        "package_lock_sha256": "package_lock_sha256",
        "signature_identity": "signature_identity",
        "signature_issuer": "signature_issuer",
        "totals": "totals",
        "vite_manifest_sha256": "vite_manifest_sha256",
    }
    for evidence_field, lock_field in candidate_bindings.items():
        if payload.get(evidence_field) != candidate.get(lock_field):
            raise EvidenceError(
                f"{filename} {evidence_field} does not match committed lock"
            )
    approval_bindings = {
        "approval_artifact_ref": "artifact_ref",
        "approval_reference": "approval_reference",
        "approval_signature_identity": "signature_identity",
        "approval_signature_issuer": "signature_issuer",
        "approval_statement_sha256": "statement_sha256",
        "promotion_workflow_sha": "promotion_workflow_sha",
        "rebuild_evidence_sha256": "rebuild_evidence_sha256",
    }
    for evidence_field, lock_field in approval_bindings.items():
        if payload.get(evidence_field) != approval.get(lock_field):
            raise EvidenceError(
                f"{filename} {evidence_field} does not match committed lock"
            )
    package_lock_raw = _read_regular_bytes(
        repository_root / "prototype/auris-flow-ui/package-lock.json"
    )
    if candidate.get("package_lock_sha256") != _sha256_bytes(package_lock_raw):
        raise EvidenceError(
            f"{filename} package_lock_sha256 does not match committed package lock"
        )
    if check_repository_binding:
        _verify_frontend_bundle_repository_binding(
            candidate_source_commit=str(candidate["source_commit"]),
            candidate_repository_tree=str(candidate["repository_tree"]),
            frontend_tree=str(candidate["frontend_tree"]),
            source_commit=source_commit,
            repository_root=repository_root,
        )


def _validate_real_stack(payload: dict[str, Any], *, source_commit: str) -> None:
    filename = "real-stack-gate.json"
    _require_exact_fields(
        payload,
        filename=filename,
        fields=frozenset(
            {
                "schema_version",
                "status",
                "source_commit",
                "execution_environment",
                "validated_at",
                "run_id",
                "source_artifacts",
                "database",
                "qdrant",
                "object_storage",
                "http_range",
                "rejected_fallback_markers",
            }
        ),
    )
    _require_common(
        payload,
        filename=filename,
        schema_version="auris.real-stack-gate.v2",
        source_commit=source_commit,
    )
    if payload.get("execution_environment") != "compose-dependencies":
        raise EvidenceError(f"{filename} execution_environment is invalid")
    _require_timestamp(payload.get("validated_at"), label=filename)
    if not _nonempty_text(payload.get("run_id")):
        raise EvidenceError(f"{filename} run_id is missing")
    source_artifacts = payload.get("source_artifacts")
    if not isinstance(source_artifacts, dict) or set(source_artifacts) != {
        "ui_bff_sha256",
        "outbox_sha256",
    }:
        raise EvidenceError(f"{filename} source artifact proof is invalid")
    for digest in source_artifacts.values():
        _require_sha256(digest, label=f"{filename} source artifact")
    database = payload.get("database")
    if (
        not isinstance(database, dict)
        or set(database)
        != {
            "backend",
            "artifact_ref",
            "run_record_count",
            "verified_audio_storage_object",
        }
        or database.get("backend") != "mysql"
        or not _nonempty_text(database.get("artifact_ref"))
        or not _positive_int(database.get("run_record_count"))
    ):
        raise EvidenceError(f"{filename} MySQL proof is invalid")
    expected_audio_storage_object = {
        "storage_object_id": "sto_rec_A_1001_20250526_122300",
        "provider": "minio",
        "bucket": "auris-flow-local",
        "status": "verified",
    }
    if database.get("verified_audio_storage_object") != expected_audio_storage_object:
        raise EvidenceError(f"{filename} MySQL audio storage proof is invalid")
    qdrant = payload.get("qdrant")
    if (
        not isinstance(qdrant, dict)
        or qdrant.get("mode") != "real_qdrant"
        or not _positive_int(qdrant.get("dispatch_count"))
        or not _positive_int(qdrant.get("recall_count"))
    ):
        raise EvidenceError(f"{filename} Qdrant proof is invalid")
    storage = payload.get("object_storage")
    if (
        not isinstance(storage, dict)
        or storage.get("mode") != "real"
        or storage.get("provider") != expected_audio_storage_object["provider"]
        or storage.get("bucket") != expected_audio_storage_object["bucket"]
        or storage.get("metadata_status") != expected_audio_storage_object["status"]
        or storage.get("metadata_registered") is not True
        or not _positive_int(storage.get("dispatch_count"))
        or storage.get("storage_object_id")
        != expected_audio_storage_object["storage_object_id"]
    ):
        raise EvidenceError(f"{filename} object-storage proof is invalid")
    http_range = payload.get("http_range")
    if (
        not isinstance(http_range, dict)
        or http_range.get("status") != 206
        or http_range.get("invalid_range_status") != 416
        or http_range.get("replacement_current_version_changed") is not True
        or http_range.get("registered_version_continuity_status") != 200
        or http_range.get("registered_version_body_match") is not True
        or not _positive_int(http_range.get("content_length"))
    ):
        raise EvidenceError(f"{filename} HTTP Range proof is invalid")
    markers = payload.get("rejected_fallback_markers")
    if (
        not isinstance(markers, list)
        or not markers
        or not all(_nonempty_text(item) for item in markers)
    ):
        raise EvidenceError(f"{filename} fallback rejection proof is invalid")


def _validate_dagster_daemons(value: object, *, label: str) -> None:
    if not isinstance(value, list) or not value:
        raise EvidenceError(f"{label} daemon proof is missing")
    required = [
        item
        for item in value
        if isinstance(item, dict) and item.get("required") is True
    ]
    if not required or any(item.get("healthy") is not True for item in required):
        raise EvidenceError(f"{label} required daemon proof is unhealthy")


def _validate_real_dagster(payload: dict[str, Any], *, source_commit: str) -> None:
    filename = "real-dagster-gate.json"
    required_fields = frozenset(
        {
            "schema_version",
            "status",
            "source_commit",
            "execution_environment",
            "started_at",
            "workspace",
            "daemon_health",
            "excluded_scope",
            "scenarios",
            "completed_at",
            "workspace_after_restart",
            "daemon_health_after_restart",
            "recovery",
        }
    )
    _require_exact_fields(payload, filename=filename, fields=required_fields)
    _require_common(
        payload,
        filename=filename,
        schema_version="auris.real-dagster-gate.v1",
        source_commit=source_commit,
    )
    if payload.get("execution_environment") != "compose":
        raise EvidenceError(f"{filename} execution_environment must be compose")
    for field in ("started_at", "completed_at"):
        _require_timestamp(payload.get(field), label=f"{filename} {field}")
    expected_workspace = {
        "location_name": "auris_flow_defs",
        "repository_name": "__repository__",
        "job_name": "auris_flow_generic_job",
    }
    for field in ("workspace", "workspace_after_restart"):
        workspace = payload.get(field)
        if (
            not isinstance(workspace, dict)
            or any(
                workspace.get(key) != value for key, value in expected_workspace.items()
            )
            or not _nonempty_text(workspace.get("dagster_version"))
        ):
            raise EvidenceError(f"{filename} {field} proof is invalid")
    _validate_dagster_daemons(payload.get("daemon_health"), label=filename)
    _validate_dagster_daemons(
        payload.get("daemon_health_after_restart"), label=filename
    )
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, dict) or set(scenarios) != {
        "success",
        "failure",
        "cancel",
    }:
        raise EvidenceError(f"{filename} scenario set is incomplete")
    for name, expected_status in (("success", "SUCCESS"), ("failure", "FAILURE")):
        scenario = scenarios.get(name)
        if (
            not isinstance(scenario, dict)
            or scenario.get("dagster_status") != expected_status
            or scenario.get("response_typename") != "LaunchRunSuccess"
            or scenario.get("selected_job_name") != "auris_flow_generic_job"
            or scenario.get("reconciled") is not True
            or not _nonempty_text(scenario.get("dagster_run_id"))
            or not _nonempty_text(scenario.get("completion_receipt_id"))
        ):
            raise EvidenceError(f"{filename} {name} scenario proof is invalid")
        _require_sha256(
            scenario.get("completion_body_sha256"),
            label=f"{filename} {name} completion body",
        )
    cancellation = scenarios.get("cancel")
    if (
        not isinstance(cancellation, dict)
        or cancellation.get("dagster_status") != "CANCELED"
        or cancellation.get("terminate_policy") != "SAFE_TERMINATE"
        or cancellation.get("completion_receipt_absent_after_cancel") is not True
        or cancellation.get("proof_scope") != "dagster-engine-only"
    ):
        raise EvidenceError(f"{filename} cancellation scenario proof is invalid")
    recovery = payload.get("recovery")
    if (
        not isinstance(recovery, dict)
        or recovery.get("canceled_completion_receipt_absent_after_restart") is not True
        or not isinstance(recovery.get("persisted_terminal_runs"), list)
        or len(recovery["persisted_terminal_runs"]) != 3
    ):
        raise EvidenceError(f"{filename} recovery proof is invalid")
    post_restart = recovery.get("post_restart_submission")
    if (
        not isinstance(post_restart, dict)
        or post_restart.get("dagster_status") != "SUCCESS"
    ):
        raise EvidenceError(f"{filename} post-restart proof is invalid")
    _require_sha256(
        post_restart.get("completion_body_sha256"),
        label=f"{filename} post-restart completion body",
    )


def _validate_outbox_events(value: object, *, label: str) -> None:
    if not isinstance(value, list) or len(value) < 3:
        raise EvidenceError(f"{label} outbox proof is incomplete")
    for event in value:
        if (
            not isinstance(event, dict)
            or event.get("status") != "processed"
            or event.get("delivery_state") != "confirmed"
            or not _positive_int(event.get("attempt_count"))
            or not _nonempty_text(event.get("dispatch_idempotency_key"))
        ):
            raise EvidenceError(f"{label} outbox proof is invalid")


def _validate_product_dagster(payload: dict[str, Any], *, source_commit: str) -> None:
    filename = "product-dagster-gate.json"
    _require_exact_fields(
        payload,
        filename=filename,
        fields=frozenset(
            {
                "schema_version",
                "status",
                "source_commit",
                "execution_environment",
                "adapter_mode",
                "verified_at",
                "scope",
                "scenarios",
            }
        ),
    )
    _require_common(
        payload,
        filename=filename,
        schema_version="auris.product-dagster-gate.v1",
        source_commit=source_commit,
    )
    if (
        payload.get("execution_environment") != "compose"
        or payload.get("adapter_mode") != "real"
    ):
        raise EvidenceError(f"{filename} does not prove the real Compose adapter")
    _require_timestamp(payload.get("verified_at"), label=filename)
    if payload.get("scope") != {"tenant_id": "aurora_auto", "project_id": "sales_qa"}:
        raise EvidenceError(f"{filename} scope proof is invalid")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, dict) or set(scenarios) != {"success", "cancellation"}:
        raise EvidenceError(f"{filename} scenario set is incomplete")
    success = scenarios.get("success")
    cancellation = scenarios.get("cancellation")
    if (
        not isinstance(success, dict)
        or success.get("status") != "success"
        or success.get("adapter_mode") != "real"
        or success.get("status_sync") != "SUCCESS"
        or success.get("signed_completion") is not True
        or success.get("outbox_confirmed") is not True
        or not _positive_int(success.get("status_version"))
    ):
        raise EvidenceError(f"{filename} success scenario proof is invalid")
    if (
        not isinstance(cancellation, dict)
        or cancellation.get("status") != "cancelled"
        or cancellation.get("adapter_mode") != "real"
        or cancellation.get("terminate_policy") != "SAFE_TERMINATE"
        or cancellation.get("engine_status") not in {"CANCELED", "CANCELLED"}
        or cancellation.get("outbox_confirmed") is not True
        or not _positive_int(cancellation.get("status_version"))
    ):
        raise EvidenceError(f"{filename} cancellation scenario proof is invalid")
    _validate_outbox_events(success.get("outbox_events"), label=f"{filename} success")
    _validate_outbox_events(
        cancellation.get("outbox_events"), label=f"{filename} cancellation"
    )


def _sbom_component_license(component: dict[str, Any], *, filename: str) -> str | None:
    licenses = component.get("licenses")
    if licenses is None:
        return None
    if (
        not isinstance(licenses, list)
        or len(licenses) != 1
        or not isinstance(licenses[0], dict)
        or set(licenses[0]) != {"expression"}
        or not _nonempty_text(licenses[0].get("expression"))
    ):
        raise EvidenceError(f"{filename} component license proof is invalid")
    return str(licenses[0]["expression"])


def _validate_review_exception(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != REVIEW_EXCEPTION_FIELDS:
        raise EvidenceError("dependency reviewed exception proof is invalid")
    if not all(_nonempty_text(value[field]) for field in REVIEW_EXCEPTION_FIELDS):
        raise EvidenceError("dependency reviewed exception fields are incomplete")
    normalized = {field: str(value[field]).strip() for field in REVIEW_EXCEPTION_FIELDS}
    if len(normalized["reason"]) < 20:
        raise EvidenceError("dependency reviewed exception reason is too short")
    try:
        reviewed_on = date.fromisoformat(normalized["reviewed_on"])
        expires_on = date.fromisoformat(normalized["expires_on"])
    except ValueError as exc:
        raise EvidenceError("dependency reviewed exception dates are invalid") from exc
    today = date.today()
    if reviewed_on > today or expires_on < today or expires_on < reviewed_on:
        raise EvidenceError("dependency reviewed exception is not currently valid")
    return normalized


def _load_committed_review_exceptions(
    raw: bytes,
) -> dict[tuple[str, str, str], dict[str, str]]:
    filename = "config/release/license-review-exceptions.json"
    payload = _load_json_bytes(raw, label=filename)
    if set(payload) != {"schema_version", "exceptions"}:
        raise EvidenceError("committed license review exception fields are invalid")
    if payload.get("schema_version") != "auris.license-review-exceptions.v1":
        raise EvidenceError("committed license review exception schema is invalid")
    entries = payload.get("exceptions")
    if not isinstance(entries, list):
        raise EvidenceError("committed license review exceptions must be an array")

    result: dict[tuple[str, str, str], dict[str, str]] = {}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != COMMITTED_REVIEW_EXCEPTION_FIELDS
        ):
            raise EvidenceError("committed license review exception entry is invalid")
        ecosystem = entry.get("ecosystem")
        name = entry.get("name")
        version = entry.get("version")
        if (
            not isinstance(ecosystem, str)
            or ecosystem.strip().lower()
            not in {"backend-python", "dagster-python", "npm"}
            or not _nonempty_text(name)
            or not _nonempty_text(version)
        ):
            raise EvidenceError(
                "committed license review exception identity is invalid"
            )
        normalized_ecosystem = ecosystem.strip().lower()
        key = (
            normalized_ecosystem,
            _canonical_package_name(str(name).strip()),
            str(version).strip(),
        )
        if key in result:
            raise EvidenceError("duplicate committed license review exception")
        proof = _validate_review_exception(
            {field: entry[field] for field in REVIEW_EXCEPTION_FIELDS}
        )
        result[key] = proof
    return result


def _load_committed_artifact_conclusions(
    raw: bytes,
    *,
    source_input_bytes: dict[str, bytes],
) -> dict[tuple[str, str, str, str], dict[str, str]]:
    filename = "config/release/exact-artifact-license-conclusions.json"
    payload = _load_json_bytes(raw, label=filename)
    if set(payload) != {"schema_version", "conclusions"}:
        raise EvidenceError(
            "committed exact-artifact license conclusion fields are invalid"
        )
    if payload.get("schema_version") != "auris.exact-artifact-license-conclusions.v2":
        raise EvidenceError(
            "committed exact-artifact license conclusion schema is invalid"
        )
    entries = payload.get("conclusions")
    if not isinstance(entries, list):
        raise EvidenceError(
            "committed exact-artifact license conclusions must be an array"
        )

    result: dict[tuple[str, str, str, str], dict[str, str]] = {}
    package_license_bindings: dict[tuple[str, str, str], tuple[str, str]] = {}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != COMMITTED_ARTIFACT_CONCLUSION_FIELDS
        ):
            raise EvidenceError("committed artifact conclusion entry is invalid")
        if not all(
            isinstance(entry[field], str) and str(entry[field]).strip()
            for field in COMMITTED_ARTIFACT_CONCLUSION_FIELDS
        ):
            raise EvidenceError(
                "committed exact-artifact license conclusion fields are incomplete"
            )
        ecosystem = str(entry["ecosystem"]).strip().lower()
        name = _canonical_package_name(str(entry["name"]).strip())
        version = str(entry["version"]).strip()
        artifact_sha256 = str(entry["artifact_sha256"]).strip()
        declared_license = " ".join(str(entry["declared_license"]).strip().split())
        concluded_license = str(entry["concluded_license"]).strip()
        license_text_path = _normalize_license_text_path(entry["license_text_path"])
        license_text_sha256 = str(entry["license_text_sha256"]).strip()
        if ecosystem not in {"backend-python", "dagster-python"}:
            raise EvidenceError(
                "committed exact-artifact license conclusion ecosystem is invalid"
            )
        if str(entry["name"]).strip() != name:
            raise EvidenceError(
                "committed exact-artifact license conclusion name is not canonical"
            )
        _require_sha256(
            artifact_sha256,
            label="committed exact-artifact license conclusion",
            prefixed=True,
        )
        _require_sha256(
            license_text_sha256,
            label="committed exact-artifact license text",
            prefixed=True,
        )
        try:
            license_text = source_input_bytes[license_text_path]
        except KeyError as exc:
            raise EvidenceError(
                "committed exact-artifact license text is absent from source inputs"
            ) from exc
        if f"sha256:{_sha256_bytes(license_text)}" != license_text_sha256:
            raise EvidenceError(
                "committed exact-artifact license text SHA-256 does not match"
            )
        identifiers = _spdx_license_identifiers(concluded_license)
        if not identifiers.issubset(APPROVED_LICENSE_IDENTIFIERS):
            raise EvidenceError(
                "committed exact-artifact concluded license is outside the allowlist"
            )
        key = (ecosystem, name, version, artifact_sha256)
        if key in result:
            raise EvidenceError("duplicate committed exact-artifact license conclusion")
        package_key = (ecosystem, name, version)
        license_binding = (license_text_path, license_text_sha256)
        existing_binding = package_license_bindings.setdefault(
            package_key,
            license_binding,
        )
        if existing_binding != license_binding:
            raise EvidenceError(
                "all committed exact-artifact conclusions for a package must "
                "bind the same license text"
            )
        result[key] = {
            "artifact_sha256": artifact_sha256,
            "concluded_license": concluded_license,
            "declared_license": declared_license,
            "ecosystem": ecosystem,
            "license_text_path": license_text_path,
            "license_text_sha256": license_text_sha256,
            "name": name,
            "version": version,
        }
    return result


def _build_supply_expectations(
    source_input_bytes: dict[str, bytes],
) -> SupplyExpectations:
    backend_all, backend_runtime, backend_hashes = _parse_uv_lock(
        source_input_bytes["backend/uv.lock"],
        filename="backend/uv.lock",
    )
    dagster_all, dagster_runtime, dagster_hashes = _parse_uv_lock(
        source_input_bytes["production/dagster/uv.lock"],
        filename="production/dagster/uv.lock",
    )
    npm_identities, npm_lock_entry_count = _parse_npm_lock(
        source_input_bytes["prototype/auris-flow-ui/package-lock.json"],
        filename="prototype/auris-flow-ui/package-lock.json",
    )
    return SupplyExpectations(
        component_identities={
            "backend-python": backend_all,
            "dagster-python": dagster_all,
            "npm": npm_identities,
        },
        python_runtime_identities={
            "backend-python": backend_runtime,
            "dagster-python": dagster_runtime,
        },
        python_hashes={
            "backend-python": backend_hashes,
            "dagster-python": dagster_hashes,
        },
        npm_lock_entry_count=npm_lock_entry_count,
    )


def _validate_supply_artifacts(
    artifact_bytes: dict[str, bytes],
    *,
    component_counts: dict[str, Any],
    source_input_bytes: dict[str, bytes],
) -> SupplyExpectations:
    expectations = _build_supply_expectations(source_input_bytes)
    expected_counts = {
        ecosystem: len(identities)
        for ecosystem, identities in expectations.component_identities.items()
    }
    expected_counts["total"] = sum(expected_counts.values())
    if component_counts != expected_counts:
        raise EvidenceError(
            "evidence-manifest.json component_counts do not match the committed locks"
        )
    committed_exceptions = _load_committed_review_exceptions(
        source_input_bytes["config/release/license-review-exceptions.json"]
    )
    committed_conclusions = _load_committed_artifact_conclusions(
        source_input_bytes["config/release/exact-artifact-license-conclusions.json"],
        source_input_bytes=source_input_bytes,
    )
    consumed_exceptions: set[tuple[str, str, str]] = set()
    consumed_conclusions: set[tuple[str, str, str, str]] = set()
    component_licenses: dict[tuple[str, str, str], str | None] = {}
    for filename, sbom_ecosystem in (
        ("backend-python.cdx.json", "backend-python"),
        ("dagster-python.cdx.json", "dagster-python"),
        ("npm.cdx.json", "npm"),
    ):
        try:
            payload = _load_json_bytes(artifact_bytes[filename], label=filename)
        except KeyError as exc:
            raise EvidenceError(f"{filename} SBOM is invalid") from exc
        components = payload.get("components") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("bomFormat") != "CycloneDX"
            or payload.get("specVersion") != "1.5"
            or not isinstance(components, list)
            or len(components) != component_counts[sbom_ecosystem]
        ):
            raise EvidenceError(f"{filename} SBOM component proof is invalid")
        for component in components:
            if not isinstance(component, dict):
                raise EvidenceError(f"{filename} SBOM component entry is invalid")
            name = component.get("name")
            version = component.get("version")
            if not _nonempty_text(name) or not _nonempty_text(version):
                raise EvidenceError(f"{filename} SBOM component identity is invalid")
            key = (sbom_ecosystem, _canonical_package_name(str(name)), str(version))
            if key in component_licenses:
                raise EvidenceError(f"{filename} SBOM component identity is duplicated")
            component_licenses[key] = _sbom_component_license(
                component, filename=filename
            )
        observed_identities = frozenset(
            (name, version)
            for ecosystem, name, version in component_licenses
            if ecosystem == sbom_ecosystem
        )
        if observed_identities != expectations.component_identities[sbom_ecosystem]:
            lock_label = (
                "uv.lock closure"
                if sbom_ecosystem in {"backend-python", "dagster-python"}
                else "package-lock closure"
            )
            raise EvidenceError(f"{filename} does not match its committed {lock_label}")

    try:
        inventory = _load_json_bytes(
            artifact_bytes["dependency-licenses.json"],
            label="dependency-licenses.json",
        )
    except KeyError as exc:
        raise EvidenceError("dependency-licenses.json is invalid") from exc
    if not isinstance(inventory, dict) or set(inventory) != {
        "schema_version",
        "dependencies",
        "policy",
    }:
        raise EvidenceError("dependency license inventory fields are invalid")
    dependencies = inventory.get("dependencies")
    if (
        inventory.get("schema_version") != "auris.dependency-license-inventory.v3"
        or inventory.get("policy") != LICENSE_POLICY
        or not isinstance(dependencies, list)
        or len(dependencies) != component_counts["total"]
    ):
        raise EvidenceError(
            "dependency license inventory is incomplete or has policy drift"
        )

    seen_dependencies: set[tuple[str, str, str]] = set()
    dependency_order: list[tuple[str, str, str]] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise EvidenceError("dependency license entry is invalid")
        ecosystem = dependency.get("ecosystem")
        name = dependency.get("name")
        version = dependency.get("version")
        if (
            ecosystem not in {"backend-python", "dagster-python", "npm"}
            or not _nonempty_text(name)
            or not _nonempty_text(version)
        ):
            raise EvidenceError("dependency license identity is invalid")
        key = (str(ecosystem), _canonical_package_name(str(name)), str(version))
        if key in seen_dependencies or key not in component_licenses:
            raise EvidenceError(
                "dependency license identity is duplicated or absent from SBOM"
            )
        seen_dependencies.add(key)
        dependency_order.append(key)
        declared_license = dependency.get("declared_license")
        concluded_license = dependency.get("concluded_license")
        if declared_license is not None and not _nonempty_text(declared_license):
            raise EvidenceError("dependency declared license is invalid")
        if concluded_license is not None and not _nonempty_text(concluded_license):
            raise EvidenceError("dependency license conclusion is invalid")
        effective_license = (
            concluded_license if concluded_license is not None else declared_license
        )
        if effective_license != component_licenses[key]:
            raise EvidenceError("dependency license conclusion does not match its SBOM")
        obligations = dependency.get("obligations")
        if not isinstance(obligations, list) or not all(
            _nonempty_text(item) for item in obligations
        ):
            raise EvidenceError("dependency license obligations are missing")

        status = dependency.get("license_status")
        base_fields = {
            "ecosystem",
            "name",
            "version",
            "declared_license",
            "concluded_license",
            "license_status",
            "obligations",
        }
        if status == "approved-compatible":
            if (
                set(dependency) != base_fields
                or not isinstance(declared_license, str)
                or not isinstance(concluded_license, str)
                or declared_license != concluded_license
            ):
                raise EvidenceError("approved dependency license proof is invalid")
            identifiers = _spdx_license_identifiers(concluded_license)
            if not identifiers.issubset(APPROVED_LICENSE_IDENTIFIERS):
                raise EvidenceError(
                    "approved dependency license is outside the allowlist"
                )
            if frozenset(obligations) != _expected_license_obligations(identifiers):
                raise EvidenceError(
                    "approved dependency license obligations are invalid"
                )
        elif status == "approved-exact-artifact-conclusion":
            if (
                set(dependency) != base_fields | {"conclusion_evidence"}
                or not isinstance(declared_license, str)
                or not isinstance(concluded_license, str)
                or key[0] not in {"backend-python", "dagster-python"}
            ):
                raise EvidenceError(
                    "exact-artifact dependency license proof is invalid"
                )
            identifiers = _spdx_license_identifiers(concluded_license)
            if not identifiers.issubset(APPROVED_LICENSE_IDENTIFIERS):
                raise EvidenceError(
                    "exact-artifact concluded license is outside the allowlist"
                )
            try:
                declared_identifiers = _spdx_license_identifiers(declared_license)
            except EvidenceError:
                declared_identifiers = frozenset()
            if declared_identifiers and declared_identifiers.issubset(
                APPROVED_LICENSE_IDENTIFIERS
            ):
                raise EvidenceError(
                    "exact-artifact conclusion is unnecessary for an approved "
                    "declared license"
                )
            if frozenset(obligations) != _expected_license_obligations(identifiers):
                raise EvidenceError(
                    "exact-artifact dependency license obligations are invalid"
                )

            locked_hashes = expectations.python_hashes[key[0]].get((key[1], key[2]))
            if locked_hashes is None:
                raise EvidenceError(
                    "exact-artifact dependency is absent from its committed lock"
                )
            exact_entries = {
                conclusion_key: conclusion
                for conclusion_key, conclusion in committed_conclusions.items()
                if conclusion_key[:3] == key
            }
            if not exact_entries:
                raise EvidenceError(
                    "dependency has no exact committed artifact conclusion: "
                    f"{key[0]}:{key[1]}@{key[2]}"
                )
            configured_hashes = frozenset(
                conclusion_key[3] for conclusion_key in exact_entries
            )
            if configured_hashes != locked_hashes:
                raise EvidenceError(
                    "exact committed artifact conclusion must cover every "
                    f"locked artifact: {key[0]}:{key[1]}@{key[2]}"
                )
            if {
                conclusion["declared_license"] for conclusion in exact_entries.values()
            } != {declared_license}:
                raise EvidenceError(
                    "exact committed artifact conclusion declared license "
                    "does not match the dependency inventory"
                )
            if {
                conclusion["concluded_license"] for conclusion in exact_entries.values()
            } != {concluded_license}:
                raise EvidenceError(
                    "exact committed artifact concluded license does not match "
                    "the dependency inventory"
                )
            configured_license_texts = {
                (
                    conclusion["license_text_path"],
                    conclusion["license_text_sha256"],
                )
                for conclusion in exact_entries.values()
            }
            if len(configured_license_texts) != 1:
                raise EvidenceError(
                    "exact committed artifact conclusions must bind one license "
                    "text per package"
                )
            configured_license_text_path, configured_license_text_sha256 = (
                configured_license_texts.pop()
            )

            conclusion_evidence = dependency.get("conclusion_evidence")
            if (
                not isinstance(conclusion_evidence, dict)
                or set(conclusion_evidence) != ARTIFACT_CONCLUSION_EVIDENCE_FIELDS
                or conclusion_evidence.get("kind") != "committed-exact-artifact-map"
            ):
                raise EvidenceError("exact-artifact conclusion evidence is invalid")
            evidence_hashes = conclusion_evidence.get("artifact_sha256s")
            if (
                not isinstance(evidence_hashes, list)
                or evidence_hashes != sorted(locked_hashes)
                or len(set(evidence_hashes)) != len(evidence_hashes)
            ):
                raise EvidenceError(
                    "exact-artifact conclusion evidence hashes are invalid"
                )
            if (
                conclusion_evidence.get("license_text_path")
                != configured_license_text_path
                or conclusion_evidence.get("license_text_sha256")
                != configured_license_text_sha256
            ):
                raise EvidenceError(
                    "exact-artifact conclusion evidence license text proof does "
                    "not match the committed policy"
                )
            consumed_conclusions.update(exact_entries)
        elif status == "reviewed-exception":
            if set(dependency) != base_fields | {"review_exception"}:
                raise EvidenceError("reviewed dependency license proof is invalid")
            if concluded_license is not None:
                raise EvidenceError(
                    "reviewed dependency must not assert a concluded license"
                )
            if frozenset(obligations) != REVIEW_EXCEPTION_OBLIGATIONS:
                raise EvidenceError(
                    "reviewed dependency license obligations are invalid"
                )
            if isinstance(declared_license, str):
                try:
                    identifiers = _spdx_license_identifiers(declared_license)
                except EvidenceError:
                    identifiers = frozenset()
                if identifiers and identifiers.issubset(APPROVED_LICENSE_IDENTIFIERS):
                    raise EvidenceError(
                        "reviewed exception is unnecessary for an approved license"
                    )
            committed_proof = committed_exceptions.get(key)
            if committed_proof is None:
                raise EvidenceError(
                    "dependency has no exact committed review exception: "
                    f"{key[0]}:{key[1]}@{key[2]}"
                )
            evidence_proof = _validate_review_exception(
                dependency.get("review_exception")
            )
            if evidence_proof != committed_proof:
                raise EvidenceError(
                    "dependency review proof differs from its exact committed "
                    "review exception"
                )
            consumed_exceptions.add(key)
        else:
            raise EvidenceError(
                "dependency license policy contains an unapproved result"
            )

    if seen_dependencies != set(component_licenses):
        raise EvidenceError(
            "dependency license inventory does not cover every SBOM component"
        )
    if dependency_order != sorted(dependency_order):
        raise EvidenceError(
            "dependency license inventory ordering is not deterministic"
        )
    unused_exceptions = set(committed_exceptions) - consumed_exceptions
    if unused_exceptions:
        ecosystem, name, version = sorted(unused_exceptions)[0]
        raise EvidenceError(
            f"unused committed license review exception: {ecosystem}:{name}@{version}"
        )
    unused_conclusions = set(committed_conclusions) - consumed_conclusions
    if unused_conclusions:
        ecosystem, name, version, artifact_sha256 = sorted(unused_conclusions)[0]
        raise EvidenceError(
            "unused committed exact-artifact license conclusion: "
            f"{ecosystem}:{name}@{version}#{artifact_sha256}"
        )
    return expectations


def _validate_core_evidence(
    evidence_dir: Path,
    *,
    source_commit: str,
    repository_root: Path,
    check_repository_binding: bool,
    expected_release_tag: str | None,
    release_bundle_root: Path | None,
) -> tuple[dict[str, bytes], SupplyExpectations]:
    verified_bytes: dict[str, bytes] = {}
    clean_clone, raw = _load_json_object(evidence_dir / "clean-clone.json")
    verified_bytes["clean-clone.json"] = raw
    _validate_clean_clone(clean_clone, source_commit=source_commit)

    frontend_bundle, raw = _load_json_object(evidence_dir / "frontend-bundle.json")
    verified_bytes["frontend-bundle.json"] = raw
    _validate_frontend_bundle(
        frontend_bundle,
        source_commit=source_commit,
        repository_root=repository_root,
        check_repository_binding=check_repository_binding,
    )

    visual, raw = _load_json_object(evidence_dir / "visual-regression.json")
    verified_bytes["visual-regression.json"] = raw
    _validate_visual(visual, source_commit=source_commit)

    real_stack, raw = _load_json_object(evidence_dir / "real-stack-gate.json")
    verified_bytes["real-stack-gate.json"] = raw
    _validate_real_stack(real_stack, source_commit=source_commit)

    real_dagster, raw = _load_json_object(evidence_dir / "real-dagster-gate.json")
    verified_bytes["real-dagster-gate.json"] = raw
    _validate_real_dagster(real_dagster, source_commit=source_commit)

    product_dagster, raw = _load_json_object(evidence_dir / "product-dagster-gate.json")
    verified_bytes["product-dagster-gate.json"] = raw
    _validate_product_dagster(product_dagster, source_commit=source_commit)

    production_path, raw = _load_json_object(evidence_dir / "production-path-gate.json")
    verified_bytes["production-path-gate.json"] = raw
    _validate_production_path(
        production_path,
        source_commit=source_commit,
        repository_root=repository_root,
    )

    backup_restore, raw = _load_json_object(
        evidence_dir / "backup-restore-gate.json"
    )
    verified_bytes["backup-restore-gate.json"] = raw
    _validate_backup_restore(
        backup_restore,
        source_commit=source_commit,
        repository_root=repository_root,
    )
    _signature_bundle, raw = _load_json_object(
        evidence_dir / "backup-restore-gate.sigstore.json"
    )
    verified_bytes["backup-restore-gate.sigstore.json"] = raw
    if (expected_release_tag is None) != (release_bundle_root is None):
        raise EvidenceError(
            "formal backup/restore validation requires both release tag and bundle"
        )
    if expected_release_tag is not None and release_bundle_root is not None:
        binding_errors = validate_backup_restore_release_bindings(
            backup_restore,
            release_bundle_root=release_bundle_root,
            expected_commit=source_commit,
            expected_release_tag=expected_release_tag,
        )
        if binding_errors:
            raise EvidenceError(
                "backup/restore release binding is invalid: "
                + "; ".join(binding_errors)
            )
        try:
            verify_signed_release_bundle(release_bundle_root)
            verify_backup_restore_sigstore_attestation(
                evidence_path=evidence_dir / "backup-restore-gate.json",
                signature_bundle=(
                    evidence_dir / "backup-restore-gate.sigstore.json"
                ),
                release_tag=expected_release_tag,
            )
        except FormalEvidenceError as exc:
            raise EvidenceError(str(exc)) from exc

    supply_manifest, raw = _load_json_object(evidence_dir / "evidence-manifest.json")
    verified_bytes["evidence-manifest.json"] = raw
    supply_bytes, source_input_bytes = _validate_supply_manifest(
        evidence_dir,
        supply_manifest,
        source_commit=source_commit,
        repository_root=repository_root,
    )
    supply_expectations = _validate_supply_artifacts(
        supply_bytes,
        component_counts=supply_manifest["component_counts"],
        source_input_bytes=source_input_bytes,
    )
    verified_bytes.update(supply_bytes)
    return verified_bytes, supply_expectations


def _parse_hashed_requirements(
    raw: bytes,
    *,
    filename: str,
    lock_hashes: dict[PackageIdentity, frozenset[str]],
) -> tuple[frozenset[PackageIdentity], frozenset[PackageIdentity]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError(f"{filename} is not UTF-8") from exc
    declared: set[PackageIdentity] = set()
    active: set[PackageIdentity] = set()
    current_identity: PackageIdentity | None = None
    current_marker: str | None = None
    current_hashes: set[str] = set()

    def finish_requirement() -> None:
        nonlocal current_identity, current_marker, current_hashes
        if current_identity is None:
            return
        if current_identity in declared:
            raise EvidenceError(f"{filename} contains a duplicate requirement")
        expected_hashes = lock_hashes.get(current_identity)
        if expected_hashes is None:
            raise EvidenceError(
                f"{filename} runtime requirements contain a package absent from uv.lock"
            )
        if frozenset(current_hashes) != expected_hashes:
            raise EvidenceError(
                f"{filename} runtime requirements hashes do not match uv.lock"
            )
        declared.add(current_identity)
        if _marker_applies(current_marker, extras={""}):
            active.add(current_identity)
        current_identity = None
        current_marker = None
        current_hashes = set()

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continued = stripped.endswith("\\")
        content = stripped[:-1].rstrip() if continued else stripped
        if raw_line[:1].isspace():
            if current_identity is None:
                raise EvidenceError(f"{filename} has an orphan requirement option")
            hash_match = REQUIREMENT_HASH_PATTERN.fullmatch(content)
            if hash_match is None:
                raise EvidenceError(f"{filename} contains an unsupported option")
            current_hashes.add(f"sha256:{hash_match.group('digest')}")
            continue

        finish_requirement()
        requirement_match = REQUIREMENT_PATTERN.fullmatch(content)
        if requirement_match is None or not continued:
            raise EvidenceError(f"{filename} contains an unpinned requirement")
        current_identity = (
            _canonical_package_name(requirement_match.group("name")),
            requirement_match.group("version"),
        )
        marker = requirement_match.group("marker")
        current_marker = marker.strip() if marker is not None else None
    finish_requirement()
    if not declared or not active:
        raise EvidenceError(f"{filename} runtime requirements are empty")
    return frozenset(declared), frozenset(active)


def _validate_python_audit(
    payload: dict[str, Any],
    *,
    filename: str,
    expected_identities: frozenset[PackageIdentity],
) -> None:
    if set(payload) != {"dependencies", "fixes"} or payload.get("fixes") != []:
        raise EvidenceError(f"{filename} pip-audit schema is invalid")
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, list):
        raise EvidenceError(f"{filename} dependency list is invalid")
    observed: set[PackageIdentity] = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict) or set(dependency) != {
            "name",
            "version",
            "vulns",
        }:
            raise EvidenceError(f"{filename} vulnerability result is invalid")
        name = dependency.get("name")
        version = dependency.get("version")
        vulnerabilities = dependency.get("vulns")
        if (
            not _nonempty_text(name)
            or not _nonempty_text(version)
            or not isinstance(vulnerabilities, list)
        ):
            raise EvidenceError(f"{filename} vulnerability result is invalid")
        identity = (_canonical_package_name(str(name)), str(version))
        if identity in observed:
            raise EvidenceError(f"{filename} audit coverage is duplicated")
        observed.add(identity)
        if vulnerabilities:
            raise EvidenceError(f"{filename} contains unresolved vulnerabilities")
    if observed != expected_identities:
        raise EvidenceError(f"{filename} audit coverage does not match runtime uv.lock")


def _validate_npm_audit(
    payload: dict[str, Any],
    *,
    expected_dependency_count: int,
) -> None:
    filename = "npm-audit.json"
    if set(payload) != {"auditReportVersion", "metadata", "vulnerabilities"}:
        raise EvidenceError(f"{filename} schema is invalid")
    if payload.get("auditReportVersion") != 2:
        raise EvidenceError(f"{filename} audit report version is invalid")
    metadata = payload.get("metadata")
    vulnerability_entries = payload.get("vulnerabilities")
    if (
        not isinstance(metadata, dict)
        or set(metadata) != {"dependencies", "vulnerabilities"}
        or not isinstance(vulnerability_entries, dict)
    ):
        raise EvidenceError(f"{filename} metadata is invalid")
    dependency_counts = metadata.get("dependencies")
    expected_dependency_fields = {
        "prod",
        "dev",
        "optional",
        "peer",
        "peerOptional",
        "total",
    }
    if (
        not isinstance(dependency_counts, dict)
        or set(dependency_counts) != expected_dependency_fields
        or not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in dependency_counts.values()
        )
        or dependency_counts["total"] != expected_dependency_count
    ):
        raise EvidenceError(
            f"{filename} npm audit coverage does not match package-lock"
        )

    vulnerability_counts = metadata.get("vulnerabilities")
    severity_fields = {"info", "low", "moderate", "high", "critical", "total"}
    if (
        not isinstance(vulnerability_counts, dict)
        or set(vulnerability_counts) != severity_fields
        or not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in vulnerability_counts.values()
        )
        or vulnerability_counts["total"]
        != sum(
            vulnerability_counts[severity]
            for severity in ("info", "low", "moderate", "high", "critical")
        )
    ):
        raise EvidenceError(f"{filename} vulnerability summary is invalid")
    for severity in ("high", "critical"):
        if vulnerability_counts[severity] != 0:
            raise EvidenceError(
                f"{filename} contains unresolved {severity} vulnerabilities"
            )
    for package_name, vulnerability in vulnerability_entries.items():
        if not _nonempty_text(package_name) or not isinstance(vulnerability, dict):
            raise EvidenceError(f"{filename} vulnerability entry is invalid")
        if vulnerability.get("severity") in {"high", "critical"}:
            raise EvidenceError(f"{filename} contains an unresolved high-risk entry")


def _validate_audit_reports(
    evidence: dict[str, tuple[dict[str, Any], bytes]],
    *,
    verified_bytes: dict[str, bytes],
    expectations: SupplyExpectations,
) -> None:
    for ecosystem, requirements_filename, audit_filename in (
        (
            "backend-python",
            "backend-runtime-requirements.txt",
            "backend-python-audit.json",
        ),
        (
            "dagster-python",
            "dagster-runtime-requirements.txt",
            "dagster-python-audit.json",
        ),
    ):
        _, active_requirements = _parse_hashed_requirements(
            verified_bytes[requirements_filename],
            filename=requirements_filename,
            lock_hashes=expectations.python_hashes[ecosystem],
        )
        if active_requirements != expectations.python_runtime_identities[ecosystem]:
            raise EvidenceError(
                f"{requirements_filename} runtime requirements do not match uv.lock"
            )
        payload, _ = evidence[audit_filename]
        _validate_python_audit(
            payload,
            filename=audit_filename,
            expected_identities=active_requirements,
        )

    npm_payload, _ = evidence["npm-audit.json"]
    _validate_npm_audit(
        npm_payload,
        expected_dependency_count=expectations.npm_lock_entry_count,
    )


def _verify_repository_binding(source_commit: str, *, repository_root: Path) -> None:
    try:
        head = (
            subprocess.run(
                ("git", "rev-parse", "--verify", "HEAD^{commit}"),
                cwd=repository_root,
                check=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .lower()
        )
        status = subprocess.run(
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EvidenceError("unable to verify the release repository binding") from exc
    if head != source_commit:
        raise EvidenceError("release evidence source_commit is not repository HEAD")
    if status:
        raise EvidenceError("release evidence finalization requires a clean Git tree")


def finalize_release_evidence(
    evidence_dir: Path,
    *,
    source_commit: str,
    check_repository_binding: bool = True,
    repository_root: Path = ROOT,
    require_audits: bool = False,
    expected_release_tag: str | None = None,
    release_bundle_root: Path | None = None,
) -> dict[str, Any]:
    normalized_commit = _normalize_commit(source_commit)
    repository_root = Path(os.path.abspath(repository_root))
    evidence_dir = Path(os.path.abspath(evidence_dir))
    if release_bundle_root is not None:
        release_bundle_root = Path(os.path.abspath(release_bundle_root))
    if not evidence_dir.is_dir() or evidence_dir.is_symlink():
        raise EvidenceError("release evidence directory must be a regular directory")
    if check_repository_binding:
        _verify_repository_binding(normalized_commit, repository_root=repository_root)

    entries = list(evidence_dir.iterdir())
    names = {entry.name for entry in entries}
    missing = sorted(CORE_EVIDENCE - names)
    if missing:
        raise EvidenceError(
            "required release evidence is missing: " + ", ".join(missing)
        )
    unrecognized = sorted(names - ALLOWED_EVIDENCE)
    if unrecognized:
        raise EvidenceError(
            "unrecognized release evidence artifact: " + ", ".join(unrecognized)
        )
    if require_audits:
        missing_audits = sorted(OPTIONAL_RELEASE_EVIDENCE - names)
        if missing_audits:
            raise EvidenceError(
                "official release audit evidence is missing: "
                + ", ".join(missing_audits)
            )
    present_audits = OPTIONAL_RELEASE_EVIDENCE & names
    if present_audits and present_audits != OPTIONAL_RELEASE_EVIDENCE:
        missing_audits = sorted(OPTIONAL_RELEASE_EVIDENCE - present_audits)
        raise EvidenceError(
            "release audit evidence is partial: " + ", ".join(missing_audits)
        )

    verified_bytes, supply_expectations = _validate_core_evidence(
        evidence_dir,
        source_commit=normalized_commit,
        repository_root=repository_root,
        check_repository_binding=check_repository_binding,
        expected_release_tag=expected_release_tag,
        release_bundle_root=release_bundle_root,
    )
    audit_documents: dict[str, tuple[dict[str, Any], bytes]] = {}
    for filename in sorted(OPTIONAL_RELEASE_EVIDENCE & names):
        path = evidence_dir / filename
        if filename.endswith(".json"):
            audit_documents[filename] = _load_json_object(path)
            verified_bytes[filename] = audit_documents[filename][1]
        else:
            verified_bytes[filename] = _read_regular_bytes(
                path, size_limit=MAX_TEXT_BYTES
            )
    if audit_documents:
        _validate_audit_reports(
            audit_documents,
            verified_bytes=verified_bytes,
            expectations=supply_expectations,
        )

    expected_loaded = names - {FINAL_MANIFEST}
    if set(verified_bytes) != expected_loaded:
        raise EvidenceError("release evidence was not completely validated")

    artifacts = [
        {
            "path": filename,
            "sha256": _sha256_bytes(verified_bytes[filename]),
            "size": len(verified_bytes[filename]),
        }
        for filename in sorted(verified_bytes)
    ]
    payload: dict[str, Any] = {
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "completed_at": datetime.now(UTC).isoformat(),
        "schema_version": "auris.release-gate-manifest.v1",
        "source_commit": normalized_commit,
        "status": "ok",
    }
    output_path = evidence_dir / FINAL_MANIFEST
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=evidence_dir,
            prefix=f".{FINAL_MANIFEST}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(payload, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, output_path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate and hash all commit-bound Auris Flow release evidence."
    )
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--source-commit")
    parser.add_argument(
        "--require-audits",
        action="store_true",
        help="require and semantically validate official pip-audit and npm audit reports",
    )
    parser.add_argument("--expected-release-tag")
    parser.add_argument("--release-bundle-root", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.require_audits and (
            args.expected_release_tag is None
            or args.release_bundle_root is None
        ):
            raise EvidenceError(
                "--require-audits requires --expected-release-tag and "
                "--release-bundle-root for formal recovery attestation"
            )
        source_commit = args.source_commit
        if source_commit is None:
            source_commit = subprocess.run(
                ("git", "rev-parse", "--verify", "HEAD^{commit}"),
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        payload = finalize_release_evidence(
            args.evidence_dir,
            source_commit=source_commit,
            require_audits=args.require_audits,
            expected_release_tag=args.expected_release_tag,
            release_bundle_root=args.release_bundle_root,
        )
    except (EvidenceError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Release evidence failed closed: {exc}", file=sys.stderr)
        return 1
    print(
        "Release evidence finalized: "
        f"{payload['artifact_count']} artifacts, source_commit={payload['source_commit']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
