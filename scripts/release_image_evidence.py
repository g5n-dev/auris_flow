#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import stat
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any


IMAGE_EVIDENCE_SCHEMA = "auris.release-image-evidence.v1"
VERIFICATION_SCHEMA = "auris.github-attestation-verification.v2"
SLSA_PREDICATE = "https://slsa.dev/provenance/v1"
CYCLONEDX_PREDICATE = "https://cyclonedx.org/bom"
COMMIT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
IMAGE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")
RELEASE_TAG_PATTERN = re.compile(
    r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-rc\.[1-9]\d*)?"
)
REPOSITORY_PATTERN = re.compile(
    r"ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*/"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
)
MAX_EVIDENCE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096


class ImageEvidenceError(RuntimeError):
    """Raised when release image attestation evidence is incomplete or ambiguous."""


def _base_evidence_names(image_id: str) -> dict[str, str]:
    return {
        "cyclonedx_sbom": f"{image_id}.cdx.json",
        "provenance_attestation": f"{image_id}.provenance.sigstore.json",
        "provenance_verification": f"{image_id}.provenance.verification.json",
        "sbom_attestation": f"{image_id}.sbom.sigstore.json",
        "sbom_verification": f"{image_id}.sbom.verification.json",
    }


def _assembly_evidence_names(image_id: str) -> dict[str, str]:
    return {
        "assembly_provenance_verification": (
            f"{image_id}.assembly.provenance.verification.json"
        ),
        "assembly_sbom_verification": f"{image_id}.assembly.sbom.verification.json",
        "assembly_registry_provenance_verification": (
            f"{image_id}.assembly.registry.provenance.verification.json"
        ),
        "assembly_registry_sbom_verification": (
            f"{image_id}.assembly.registry.sbom.verification.json"
        ),
    }


def _validate_image_id(value: str) -> str:
    if IMAGE_ID_PATTERN.fullmatch(value) is None:
        raise ImageEvidenceError("image id must use lowercase kebab-case")
    return value


def _validate_repository(value: str) -> str:
    if REPOSITORY_PATTERN.fullmatch(value) is None:
        raise ImageEvidenceError(
            "image repository must be a lowercase, tag-free ghcr.io owner/repository path"
        )
    return value


def _validate_release_tag(value: str) -> str:
    if RELEASE_TAG_PATTERN.fullmatch(value) is None:
        raise ImageEvidenceError("release tag must be SemVer or an rc.N prerelease")
    return value


def _validate_source_commit(value: str) -> str:
    if COMMIT_PATTERN.fullmatch(value) is None:
        raise ImageEvidenceError(
            "source commit must be a complete lowercase Git object id"
        )
    return value


def _validate_image_digest(value: str) -> str:
    if DIGEST_PATTERN.fullmatch(value) is None:
        raise ImageEvidenceError(
            "image digest must be a complete lowercase sha256 digest"
        )
    return value


def _require_regular_file(path: Path, *, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ImageEvidenceError(f"{label} is missing: {path.name}") from exc
    if not stat.S_ISREG(mode):
        raise ImageEvidenceError(
            f"{label} must be a regular non-symlink file: {path.name}"
        )
    size = path.stat().st_size
    if size <= 0 or size > MAX_EVIDENCE_BYTES:
        raise ImageEvidenceError(
            f"{label} must be non-empty and at most {MAX_EVIDENCE_BYTES} bytes: {path.name}"
        )


def _load_json(path: Path, *, label: str) -> Any:
    _require_regular_file(path, label=label)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageEvidenceError(
            f"{label} must be valid UTF-8 JSON: {path.name}"
        ) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_json_atomic(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(_json_bytes(document))
        handle.flush()
    temporary.chmod(0o644)
    temporary.replace(path)


def _validate_cyclonedx(document: Any) -> None:
    if not isinstance(document, dict):
        raise ImageEvidenceError("CycloneDX SBOM must be a JSON object")
    if document.get("bomFormat") != "CycloneDX":
        raise ImageEvidenceError("image SBOM must declare CycloneDX bomFormat")
    spec_version = document.get("specVersion")
    if (
        not isinstance(spec_version, str)
        or re.fullmatch(r"1\.[4-9]", spec_version) is None
    ):
        raise ImageEvidenceError(
            "image SBOM must use a supported CycloneDX 1.x version"
        )
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ImageEvidenceError("image SBOM must declare a positive integer version")
    if not isinstance(document.get("components"), list):
        raise ImageEvidenceError("image SBOM must contain a components array")


def _validate_attestation_bundle(document: Any, *, label: str) -> None:
    if not isinstance(document, dict) or not document:
        raise ImageEvidenceError(f"{label} must be a non-empty Sigstore bundle object")


def _validate_sanitized_verification(
    document: Any,
    *,
    repository: str,
    image_digest: str,
    predicate_type: str,
    label: str,
) -> None:
    required = {
        "schema_version",
        "predicate_type",
        "subject",
        "verified_results",
    }
    if not isinstance(document, dict) or set(document) != required:
        raise ImageEvidenceError(f"{label} has missing or unexpected fields")
    if document.get("schema_version") != VERIFICATION_SCHEMA:
        raise ImageEvidenceError(f"{label} schema is not supported")
    if document.get("predicate_type") != predicate_type:
        raise ImageEvidenceError(
            f"{label} predicate type does not match its evidence role"
        )
    expected_subject = {
        "name": repository,
        "digest": {"sha256": image_digest.removeprefix("sha256:")},
    }
    if document.get("subject") != expected_subject:
        raise ImageEvidenceError(
            f"{label} subject does not match the exact image digest"
        )
    results = document.get("verified_results")
    if not isinstance(results, list) or not results:
        raise ImageEvidenceError(f"{label} must retain at least one verified result")
    for index, result in enumerate(results):
        if not isinstance(result, dict) or set(result) != {
            "signature",
            "verified_timestamps",
        }:
            raise ImageEvidenceError(f"{label} verified result {index} is invalid")
        signature = result.get("signature")
        if not isinstance(signature, dict) or not signature.get("certificate"):
            raise ImageEvidenceError(
                f"{label} verified result {index} must retain the signing certificate"
            )
        timestamps = result.get("verified_timestamps")
        if not isinstance(timestamps, list) or not timestamps:
            raise ImageEvidenceError(
                f"{label} verified result {index} must retain a verified timestamp"
            )


def sanitize_verification_document(
    document: Any,
    *,
    expected_repository: str,
    expected_image_digest: str,
    expected_predicate_type: str,
    expected_cyclonedx_sbom: Any | None = None,
) -> dict[str, Any]:
    repository = _validate_repository(expected_repository)
    image_digest = _validate_image_digest(expected_image_digest)
    if expected_predicate_type not in {SLSA_PREDICATE, CYCLONEDX_PREDICATE}:
        raise ImageEvidenceError("attestation predicate type is not approved")
    if not isinstance(document, list) or not document:
        raise ImageEvidenceError(
            "gh attestation verification must return at least one verified result"
        )
    if expected_predicate_type == CYCLONEDX_PREDICATE:
        _validate_cyclonedx(expected_cyclonedx_sbom)
    elif expected_cyclonedx_sbom is not None:
        raise ImageEvidenceError(
            "CycloneDX input is only valid for an SBOM attestation"
        )
    expected_subject = {
        "name": repository,
        "digest": {"sha256": image_digest.removeprefix("sha256:")},
    }
    verified_results: list[dict[str, Any]] = []
    for index, result in enumerate(document):
        if not isinstance(result, dict):
            raise ImageEvidenceError(
                f"gh attestation verification result {index} must be an object"
            )
        verification = result.get("verificationResult")
        if not isinstance(verification, dict):
            raise ImageEvidenceError(
                f"gh attestation verification result {index} is missing"
            )
        statement = verification.get("statement")
        if not isinstance(statement, dict):
            raise ImageEvidenceError(
                f"gh attestation verification statement {index} is missing"
            )
        if statement.get("predicateType") != expected_predicate_type:
            raise ImageEvidenceError(
                f"gh attestation predicate {index} does not match the expected type"
            )
        if (
            expected_predicate_type == CYCLONEDX_PREDICATE
            and statement.get("predicate") != expected_cyclonedx_sbom
        ):
            raise ImageEvidenceError(
                "signed SBOM predicate does not match the downloadable CycloneDX document"
            )
        if statement.get("subject") != [expected_subject]:
            raise ImageEvidenceError(
                f"gh attestation subject {index} does not match the exact image digest"
            )
        verified_results.append(
            {
                "signature": copy.deepcopy(verification.get("signature")),
                "verified_timestamps": copy.deepcopy(
                    verification.get("verifiedTimestamps")
                ),
            }
        )
    sanitized = {
        "schema_version": VERIFICATION_SCHEMA,
        "predicate_type": expected_predicate_type,
        "subject": expected_subject,
        "verified_results": verified_results,
    }
    _validate_sanitized_verification(
        sanitized,
        repository=repository,
        image_digest=image_digest,
        predicate_type=expected_predicate_type,
        label="sanitized gh attestation verification",
    )
    return sanitized


def _expected_record_identity(
    *,
    image_id: str,
    repository: str,
    release_tag: str,
    source_commit: str,
    image_digest: str,
) -> dict[str, str]:
    validated_id = _validate_image_id(image_id)
    validated_repository = _validate_repository(repository)
    validated_tag = _validate_release_tag(release_tag)
    validated_commit = _validate_source_commit(source_commit)
    validated_digest = _validate_image_digest(image_digest)
    tagged_reference = f"{validated_repository}:{validated_tag}"
    return {
        "schema_version": IMAGE_EVIDENCE_SCHEMA,
        "image_id": validated_id,
        "repository": validated_repository,
        "tagged_reference": tagged_reference,
        "image": f"{tagged_reference}@{validated_digest}",
        "image_digest": validated_digest,
        "release_tag": validated_tag,
        "source_commit": validated_commit,
    }


def _binding_for(path: Path) -> dict[str, str]:
    _require_regular_file(path, label="bound image evidence")
    return {"path": path.name, "sha256": _sha256_file(path)}


def _validate_evidence_semantics(
    *, evidence_dir: Path, image_id: str, repository: str, image_digest: str
) -> None:
    _validate_cyclonedx(
        _load_json(evidence_dir / f"{image_id}.cdx.json", label="CycloneDX image SBOM")
    )
    _validate_attestation_bundle(
        _load_json(
            evidence_dir / f"{image_id}.provenance.sigstore.json",
            label="provenance attestation bundle",
        ),
        label="provenance attestation bundle",
    )
    _validate_attestation_bundle(
        _load_json(
            evidence_dir / f"{image_id}.sbom.sigstore.json",
            label="SBOM attestation bundle",
        ),
        label="SBOM attestation bundle",
    )
    verification_roles = {
        f"{image_id}.provenance.verification.json": SLSA_PREDICATE,
        f"{image_id}.sbom.verification.json": CYCLONEDX_PREDICATE,
    }
    for filename, predicate_type in verification_roles.items():
        _validate_sanitized_verification(
            _load_json(
                evidence_dir / filename, label="attestation verification evidence"
            ),
            repository=repository,
            image_digest=image_digest,
            predicate_type=predicate_type,
            label=filename,
        )


def create_image_record(
    *,
    evidence_dir: Path,
    image_id: str,
    repository: str,
    tagged_reference: str,
    image_digest: str,
    release_tag: str,
    source_commit: str,
    output_path: Path,
) -> dict[str, Any]:
    identity = _expected_record_identity(
        image_id=image_id,
        repository=repository,
        release_tag=release_tag,
        source_commit=source_commit,
        image_digest=image_digest,
    )
    if tagged_reference != identity["tagged_reference"]:
        raise ImageEvidenceError(
            "tagged image reference does not match repository and release tag"
        )
    if output_path.parent.resolve() != evidence_dir.resolve() or output_path.name != (
        f"{identity['image_id']}.image.json"
    ):
        raise ImageEvidenceError(
            "image record output must use the fixed evidence filename"
        )
    _validate_evidence_semantics(
        evidence_dir=evidence_dir,
        image_id=identity["image_id"],
        repository=identity["repository"],
        image_digest=identity["image_digest"],
    )
    evidence = {
        key: _binding_for(evidence_dir / filename)
        for key, filename in _base_evidence_names(identity["image_id"]).items()
    }
    record: dict[str, Any] = {**identity, "evidence": evidence}
    _write_json_atomic(output_path, record)
    return verify_image_record(
        evidence_dir=evidence_dir,
        record_path=output_path,
        expected_image_id=identity["image_id"],
        expected_repository=identity["repository"],
        expected_release_tag=identity["release_tag"],
        expected_source_commit=identity["source_commit"],
        require_assembly_verifications=False,
    )


def verify_image_record(
    *,
    evidence_dir: Path,
    record_path: Path,
    expected_image_id: str,
    expected_repository: str,
    expected_release_tag: str,
    expected_source_commit: str,
    require_assembly_verifications: bool,
) -> dict[str, Any]:
    image_id = _validate_image_id(expected_image_id)
    repository = _validate_repository(expected_repository)
    release_tag = _validate_release_tag(expected_release_tag)
    source_commit = _validate_source_commit(expected_source_commit)
    if record_path.parent.resolve() != evidence_dir.resolve() or record_path.name != (
        f"{image_id}.image.json"
    ):
        raise ImageEvidenceError("image record must use the fixed evidence filename")
    document = _load_json(record_path, label="release image evidence record")
    required_record_fields = {
        "schema_version",
        "image_id",
        "repository",
        "tagged_reference",
        "image",
        "image_digest",
        "release_tag",
        "source_commit",
        "evidence",
    }
    if not isinstance(document, dict) or set(document) != required_record_fields:
        raise ImageEvidenceError(
            "release image evidence record has missing or unexpected fields"
        )
    digest = _validate_image_digest(str(document.get("image_digest", "")))
    expected_identity = _expected_record_identity(
        image_id=image_id,
        repository=repository,
        release_tag=release_tag,
        source_commit=source_commit,
        image_digest=digest,
    )
    for key, expected_value in expected_identity.items():
        if document.get(key) != expected_value:
            raise ImageEvidenceError(
                f"release image evidence {key} does not match expectation"
            )
    evidence = document.get("evidence")
    filenames = _base_evidence_names(image_id)
    if require_assembly_verifications:
        filenames = {**filenames, **_assembly_evidence_names(image_id)}
    if not isinstance(evidence, dict) or set(evidence) != set(filenames):
        raise ImageEvidenceError(
            "release image evidence has missing or unexpected file bindings"
        )
    for role, filename in filenames.items():
        binding = evidence.get(role)
        if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
            raise ImageEvidenceError(
                f"release image evidence binding is invalid: {role}"
            )
        if binding.get("path") != filename:
            raise ImageEvidenceError(f"release image evidence path is invalid: {role}")
        expected_sha256 = binding.get("sha256")
        if (
            not isinstance(expected_sha256, str)
            or SHA256_PATTERN.fullmatch(expected_sha256) is None
        ):
            raise ImageEvidenceError(
                f"release image evidence SHA-256 is invalid: {role}"
            )
        evidence_path = evidence_dir / filename
        _require_regular_file(evidence_path, label=f"bound image evidence {role}")
        if _sha256_file(evidence_path) != expected_sha256:
            raise ImageEvidenceError(f"release image evidence SHA-256 mismatch: {role}")
    _validate_evidence_semantics(
        evidence_dir=evidence_dir,
        image_id=image_id,
        repository=repository,
        image_digest=digest,
    )
    if require_assembly_verifications:
        assembly_roles = {
            f"{image_id}.assembly.provenance.verification.json": SLSA_PREDICATE,
            f"{image_id}.assembly.sbom.verification.json": CYCLONEDX_PREDICATE,
            f"{image_id}.assembly.registry.provenance.verification.json": (
                SLSA_PREDICATE
            ),
            f"{image_id}.assembly.registry.sbom.verification.json": (
                CYCLONEDX_PREDICATE
            ),
        }
        for filename, predicate_type in assembly_roles.items():
            _validate_sanitized_verification(
                _load_json(
                    evidence_dir / filename,
                    label="assembly attestation verification evidence",
                ),
                repository=repository,
                image_digest=digest,
                predicate_type=predicate_type,
                label=filename,
            )
    return document


def add_assembly_verifications(
    *,
    evidence_dir: Path,
    record_path: Path,
    expected_image_id: str,
    expected_repository: str,
    expected_release_tag: str,
    expected_source_commit: str,
) -> dict[str, Any]:
    document = verify_image_record(
        evidence_dir=evidence_dir,
        record_path=record_path,
        expected_image_id=expected_image_id,
        expected_repository=expected_repository,
        expected_release_tag=expected_release_tag,
        expected_source_commit=expected_source_commit,
        require_assembly_verifications=False,
    )
    updated = copy.deepcopy(document)
    evidence = updated["evidence"]
    for role, filename in _assembly_evidence_names(expected_image_id).items():
        evidence[role] = _binding_for(evidence_dir / filename)
    _write_json_atomic(record_path, updated)
    return verify_image_record(
        evidence_dir=evidence_dir,
        record_path=record_path,
        expected_image_id=expected_image_id,
        expected_repository=expected_repository,
        expected_release_tag=expected_release_tag,
        expected_source_commit=expected_source_commit,
        require_assembly_verifications=True,
    )


def _require_archive_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ImageEvidenceError(
            f"signed evidence archive is missing: {path.name}"
        ) from exc
    if not stat.S_ISREG(mode):
        raise ImageEvidenceError(
            "signed evidence archive must be a regular non-symlink file"
        )
    size = path.stat().st_size
    if size <= 0 or size > MAX_ARCHIVE_BYTES:
        raise ImageEvidenceError(
            f"signed evidence archive must be non-empty and at most {MAX_ARCHIVE_BYTES} bytes"
        )


def _validated_archive_parts(name: str, *, expected_root: str) -> tuple[str, ...]:
    if not name or "\\" in name or "\x00" in name:
        raise ImageEvidenceError("signed evidence archive contains an unsafe path")
    path = PurePosixPath(name)
    parts = path.parts
    if (
        path.is_absolute()
        or not parts
        or parts[0] != expected_root
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ImageEvidenceError("signed evidence archive contains an unsafe path")
    return parts


def _safe_extract_image_evidence(
    *,
    archive_path: Path,
    destination: Path,
    expected_root: str,
    required_names: set[str],
) -> None:
    _require_archive_file(archive_path)
    extracted_names: set[str] = set()
    seen_paths: set[tuple[str, ...]] = set()
    expanded_bytes = 0
    member_count = 0
    try:
        with tarfile.open(archive_path, mode="r|gz") as archive:
            for member in archive:
                member_count += 1
                if member_count > MAX_ARCHIVE_MEMBERS:
                    raise ImageEvidenceError(
                        "signed evidence archive member count is invalid"
                    )
                parts = _validated_archive_parts(
                    member.name, expected_root=expected_root
                )
                if parts in seen_paths:
                    raise ImageEvidenceError(
                        "signed evidence archive contains a duplicate normalized path"
                    )
                seen_paths.add(parts)
                if member.isdir():
                    continue
                if not member.isreg():
                    raise ImageEvidenceError(
                        "signed evidence archive contains a link or special file"
                    )
                if member.size < 0 or member.size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise ImageEvidenceError(
                        "signed evidence archive member size is invalid"
                    )
                expanded_bytes += member.size
                if expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
                    raise ImageEvidenceError(
                        "signed evidence archive expands beyond its limit"
                    )
                if (
                    len(parts) != 3
                    or parts[1] != "images"
                    or parts[2] not in required_names
                ):
                    continue
                filename = parts[2]
                if filename in extracted_names:
                    raise ImageEvidenceError(
                        "signed evidence archive contains duplicate image evidence"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise ImageEvidenceError(
                        "signed image evidence could not be extracted"
                    )
                target = destination / filename
                with source, target.open("xb") as output:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ImageEvidenceError(
                                "signed image evidence ended before its declared size"
                            )
                        output.write(chunk)
                        remaining -= len(chunk)
                    if source.read(1):
                        raise ImageEvidenceError(
                            "signed image evidence exceeds its declared size"
                        )
                extracted_names.add(filename)
            if member_count == 0:
                raise ImageEvidenceError(
                    "signed evidence archive member count is invalid"
                )
    except (OSError, tarfile.TarError) as exc:
        raise ImageEvidenceError(
            "signed evidence archive is not a valid gzip tar"
        ) from exc
    missing = required_names - extracted_names
    if missing:
        raise ImageEvidenceError(
            "signed evidence archive is missing image evidence: "
            + ", ".join(sorted(missing))
        )


def verify_image_evidence_archive(
    *,
    archive_path: Path,
    release_tag: str,
    source_commit: str,
    image_repositories: Mapping[str, str],
) -> dict[str, str]:
    validated_tag = _validate_release_tag(release_tag)
    validated_commit = _validate_source_commit(source_commit)
    if not image_repositories:
        raise ImageEvidenceError("at least one image repository must be verified")
    repositories: dict[str, str] = {}
    required_names: set[str] = set()
    for image_id, repository in image_repositories.items():
        validated_id = _validate_image_id(image_id)
        if validated_id in repositories:
            raise ImageEvidenceError(
                "duplicate image id in archive verification policy"
            )
        repositories[validated_id] = _validate_repository(repository)
        required_names.update(_base_evidence_names(validated_id).values())
        required_names.update(_assembly_evidence_names(validated_id).values())
        required_names.add(f"{validated_id}.image.json")
    archive_root = f"auris-flow-{validated_tag}-evidence"
    with tempfile.TemporaryDirectory(
        prefix="auris-release-image-evidence-"
    ) as temporary:
        evidence_dir = Path(temporary) / "images"
        evidence_dir.mkdir(mode=0o700)
        _safe_extract_image_evidence(
            archive_path=archive_path,
            destination=evidence_dir,
            expected_root=archive_root,
            required_names=required_names,
        )
        verified: dict[str, str] = {}
        for image_id, repository in sorted(repositories.items()):
            record = verify_image_record(
                evidence_dir=evidence_dir,
                record_path=evidence_dir / f"{image_id}.image.json",
                expected_image_id=image_id,
                expected_repository=repository,
                expected_release_tag=validated_tag,
                expected_source_commit=validated_commit,
                require_assembly_verifications=True,
            )
            verified[image_id] = str(record["image"])
    return verified


def _parse_image_repository(value: str) -> tuple[str, str]:
    image_id, separator, repository = value.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError("image must use IMAGE_ID=GHCR_REPOSITORY")
    try:
        return _validate_image_id(image_id), _validate_repository(repository)
    except ImageEvidenceError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _add_common_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--source-commit", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and fail-closed verify release image attestation evidence."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    sanitize = commands.add_parser(
        "sanitize-verification",
        help="Validate gh attestation verify JSON and retain only trusted verification fields.",
    )
    sanitize.add_argument("--input", type=Path, required=True)
    sanitize.add_argument("--output", type=Path, required=True)
    sanitize.add_argument("--repository", required=True)
    sanitize.add_argument("--image-digest", required=True)
    sanitize.add_argument("--predicate-type", required=True)
    sanitize.add_argument("--sbom-path", type=Path)

    create = commands.add_parser(
        "create", help="Create a build-stage image evidence record."
    )
    _add_common_identity_arguments(create)
    create.add_argument("--tagged-reference", required=True)
    create.add_argument("--image-digest", required=True)

    verify = commands.add_parser(
        "verify", help="Verify a release image evidence record."
    )
    _add_common_identity_arguments(verify)
    verify.add_argument("--require-assembly-verifications", action="store_true")

    add_assembly = commands.add_parser(
        "add-assembly-verifications",
        help="Bind assembly-stage verification outputs into the image evidence record.",
    )
    _add_common_identity_arguments(add_assembly)

    verify_archive = commands.add_parser(
        "verify-archive",
        help="Safely extract and verify image records from a signed evidence archive.",
    )
    verify_archive.add_argument("--archive", type=Path, required=True)
    verify_archive.add_argument("--release-tag", required=True)
    verify_archive.add_argument("--source-commit", required=True)
    verify_archive.add_argument(
        "--image", type=_parse_image_repository, action="append", required=True
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "sanitize-verification":
            raw = _load_json(arguments.input, label="raw gh attestation verification")
            expected_sbom = (
                _load_json(
                    arguments.sbom_path, label="downloadable CycloneDX image SBOM"
                )
                if arguments.sbom_path is not None
                else None
            )
            sanitized = sanitize_verification_document(
                raw,
                expected_repository=arguments.repository,
                expected_image_digest=arguments.image_digest,
                expected_predicate_type=arguments.predicate_type,
                expected_cyclonedx_sbom=expected_sbom,
            )
            _write_json_atomic(arguments.output, sanitized)
        elif arguments.command == "create":
            create_image_record(
                evidence_dir=arguments.evidence_dir,
                image_id=arguments.image_id,
                repository=arguments.repository,
                tagged_reference=arguments.tagged_reference,
                image_digest=arguments.image_digest,
                release_tag=arguments.release_tag,
                source_commit=arguments.source_commit,
                output_path=arguments.record,
            )
        elif arguments.command == "verify":
            verify_image_record(
                evidence_dir=arguments.evidence_dir,
                record_path=arguments.record,
                expected_image_id=arguments.image_id,
                expected_repository=arguments.repository,
                expected_release_tag=arguments.release_tag,
                expected_source_commit=arguments.source_commit,
                require_assembly_verifications=arguments.require_assembly_verifications,
            )
        elif arguments.command == "add-assembly-verifications":
            add_assembly_verifications(
                evidence_dir=arguments.evidence_dir,
                record_path=arguments.record,
                expected_image_id=arguments.image_id,
                expected_repository=arguments.repository,
                expected_release_tag=arguments.release_tag,
                expected_source_commit=arguments.source_commit,
            )
        elif arguments.command == "verify-archive":
            image_repositories: dict[str, str] = {}
            for image_id, repository in arguments.image:
                if image_id in image_repositories:
                    raise ImageEvidenceError(
                        "duplicate image id in archive verification policy"
                    )
                image_repositories[image_id] = repository
            verify_image_evidence_archive(
                archive_path=arguments.archive,
                release_tag=arguments.release_tag,
                source_commit=arguments.source_commit,
                image_repositories=image_repositories,
            )
        else:  # pragma: no cover - argparse enforces the command choices.
            raise ImageEvidenceError("unsupported command")
    except ImageEvidenceError as exc:
        print(f"image evidence error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
