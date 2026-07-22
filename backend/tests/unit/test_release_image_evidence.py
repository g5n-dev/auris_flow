from __future__ import annotations

import importlib.util
import io
import json
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
IMAGE_ID = "bff"
REPOSITORY = "ghcr.io/auris-flow/auris-flow-bff"
RELEASE_TAG = "v1.0.0-rc.1"
SOURCE_COMMIT = "1" * 40
IMAGE_DIGEST = "sha256:" + "a" * 64
SLSA_PREDICATE = "https://slsa.dev/provenance/v1"
CYCLONEDX_PREDICATE = "https://cyclonedx.org/bom"


def _load_evidence_module() -> ModuleType:
    path = ROOT / "scripts" / "release_image_evidence.py"
    spec = importlib.util.spec_from_file_location("auris_release_image_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verification_document(predicate_type: str) -> dict[str, object]:
    return {
        "schema_version": "auris.github-attestation-verification.v2",
        "predicate_type": predicate_type,
        "subject": {
            "name": REPOSITORY,
            "digest": {"sha256": IMAGE_DIGEST.removeprefix("sha256:")},
        },
        "verified_results": [
            {
                "signature": {"certificate": {"subjectAlternativeName": "trusted-workflow"}},
                "verified_timestamps": [
                    {"type": "transparency-log", "timestamp": "2026-07-21T00:00:00Z"}
                ],
            }
        ],
    }


def _write_base_evidence(evidence_dir: Path) -> None:
    evidence_dir.mkdir()
    (evidence_dir / f"{IMAGE_ID}.cdx.json").write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "version": 1,
                "components": [],
            }
        ),
        encoding="utf-8",
    )
    for filename in (
        f"{IMAGE_ID}.provenance.sigstore.json",
        f"{IMAGE_ID}.sbom.sigstore.json",
    ):
        (evidence_dir / filename).write_text(
            '{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}\n',
            encoding="utf-8",
        )
    (evidence_dir / f"{IMAGE_ID}.provenance.verification.json").write_text(
        json.dumps(_verification_document(SLSA_PREDICATE)),
        encoding="utf-8",
    )
    (evidence_dir / f"{IMAGE_ID}.sbom.verification.json").write_text(
        json.dumps(_verification_document(CYCLONEDX_PREDICATE)),
        encoding="utf-8",
    )


def _create_record(module: ModuleType, evidence_dir: Path, record_path: Path) -> dict[str, object]:
    return module.create_image_record(
        evidence_dir=evidence_dir,
        image_id=IMAGE_ID,
        repository=REPOSITORY,
        tagged_reference=f"{REPOSITORY}:{RELEASE_TAG}",
        image_digest=IMAGE_DIGEST,
        release_tag=RELEASE_TAG,
        source_commit=SOURCE_COMMIT,
        output_path=record_path,
    )


def _verify_record(
    module: ModuleType,
    evidence_dir: Path,
    record_path: Path,
    *,
    require_assembly: bool = False,
) -> dict[str, object]:
    return module.verify_image_record(
        evidence_dir=evidence_dir,
        record_path=record_path,
        expected_image_id=IMAGE_ID,
        expected_repository=REPOSITORY,
        expected_release_tag=RELEASE_TAG,
        expected_source_commit=SOURCE_COMMIT,
        require_assembly_verifications=require_assembly,
    )


def test_image_record_binds_exact_digest_and_every_build_evidence_file(tmp_path: Path) -> None:
    module = _load_evidence_module()
    evidence_dir = tmp_path / "evidence"
    record_path = evidence_dir / f"{IMAGE_ID}.image.json"
    _write_base_evidence(evidence_dir)

    record = _create_record(module, evidence_dir, record_path)
    verified = _verify_record(module, evidence_dir, record_path)

    assert verified == record
    assert record["schema_version"] == "auris.release-image-evidence.v1"
    assert record["image"] == f"{REPOSITORY}:{RELEASE_TAG}@{IMAGE_DIGEST}"
    assert record["image_digest"] == IMAGE_DIGEST
    assert set(record["evidence"]) == {
        "cyclonedx_sbom",
        "provenance_attestation",
        "provenance_verification",
        "sbom_attestation",
        "sbom_verification",
    }
    for binding in record["evidence"].values():
        assert set(binding) == {"path", "sha256"}
        assert len(binding["sha256"]) == 64


def test_image_record_rejects_tampered_or_symlinked_bound_evidence(tmp_path: Path) -> None:
    module = _load_evidence_module()
    evidence_dir = tmp_path / "evidence"
    record_path = evidence_dir / f"{IMAGE_ID}.image.json"
    _write_base_evidence(evidence_dir)
    _create_record(module, evidence_dir, record_path)

    bundle = evidence_dir / f"{IMAGE_ID}.provenance.sigstore.json"
    bundle.write_text("{}\n", encoding="utf-8")
    with pytest.raises(module.ImageEvidenceError, match="SHA-256"):
        _verify_record(module, evidence_dir, record_path)

    bundle.unlink()
    bundle.symlink_to(evidence_dir / f"{IMAGE_ID}.sbom.sigstore.json")
    with pytest.raises(module.ImageEvidenceError, match="regular non-symlink"):
        _verify_record(module, evidence_dir, record_path)


def test_assembly_verifications_are_added_then_required_by_later_reads(tmp_path: Path) -> None:
    module = _load_evidence_module()
    evidence_dir = tmp_path / "evidence"
    record_path = evidence_dir / f"{IMAGE_ID}.image.json"
    _write_base_evidence(evidence_dir)
    _create_record(module, evidence_dir, record_path)
    with pytest.raises(module.ImageEvidenceError, match="file bindings"):
        _verify_record(module, evidence_dir, record_path, require_assembly=True)
    (evidence_dir / f"{IMAGE_ID}.assembly.provenance.verification.json").write_text(
        json.dumps(_verification_document(SLSA_PREDICATE)),
        encoding="utf-8",
    )
    (evidence_dir / f"{IMAGE_ID}.assembly.sbom.verification.json").write_text(
        json.dumps(_verification_document(CYCLONEDX_PREDICATE)),
        encoding="utf-8",
    )
    (evidence_dir / f"{IMAGE_ID}.assembly.registry.provenance.verification.json").write_text(
        json.dumps(_verification_document(SLSA_PREDICATE)),
        encoding="utf-8",
    )
    (evidence_dir / f"{IMAGE_ID}.assembly.registry.sbom.verification.json").write_text(
        json.dumps(_verification_document(CYCLONEDX_PREDICATE)),
        encoding="utf-8",
    )

    module.add_assembly_verifications(
        evidence_dir=evidence_dir,
        record_path=record_path,
        expected_image_id=IMAGE_ID,
        expected_repository=REPOSITORY,
        expected_release_tag=RELEASE_TAG,
        expected_source_commit=SOURCE_COMMIT,
    )
    record = _verify_record(module, evidence_dir, record_path, require_assembly=True)

    assert set(record["evidence"]) >= {
        "assembly_provenance_verification",
        "assembly_sbom_verification",
        "assembly_registry_provenance_verification",
        "assembly_registry_sbom_verification",
    }
    (evidence_dir / f"{IMAGE_ID}.assembly.sbom.verification.json").write_text(
        "{}\n", encoding="utf-8"
    )
    with pytest.raises(module.ImageEvidenceError, match="SHA-256"):
        _verify_record(module, evidence_dir, record_path, require_assembly=True)


def test_gh_verification_output_is_checked_and_sanitized(tmp_path: Path) -> None:
    module = _load_evidence_module()
    raw = [
        {
            "attestation": {"unneededBundleCopy": "do-not-retain"},
            "verificationResult": {
                "signature": {
                    "certificate": {
                        "subjectAlternativeName": (
                            "https://github.com/auris-flow/auris-flow/.github/workflows/"
                            "release-images.yml@refs/tags/v1.0.0-rc.1"
                        )
                    }
                },
                "verifiedTimestamps": [{"type": "transparency-log"}],
                "statement": {
                    "_type": "https://in-toto.io/Statement/v1",
                    "subject": [
                        {
                            "name": REPOSITORY,
                            "digest": {"sha256": IMAGE_DIGEST.removeprefix("sha256:")},
                        }
                    ],
                    "predicateType": SLSA_PREDICATE,
                    "predicate": {"untrustedEnvironment": {"secret": "must-not-be-copied"}},
                },
            },
        }
    ]

    sanitized = module.sanitize_verification_document(
        raw,
        expected_repository=REPOSITORY,
        expected_image_digest=IMAGE_DIGEST,
        expected_predicate_type=SLSA_PREDICATE,
    )

    assert sanitized == _verification_document(SLSA_PREDICATE) | {
        "verified_results": [
            {
                "signature": raw[0]["verificationResult"]["signature"],
                "verified_timestamps": raw[0]["verificationResult"]["verifiedTimestamps"],
            }
        ]
    }
    encoded = json.dumps(sanitized)
    assert "unneededBundleCopy" not in encoded
    assert "untrustedEnvironment" not in encoded
    assert "must-not-be-copied" not in encoded

    raw[0]["verificationResult"]["statement"]["subject"][0]["digest"]["sha256"] = "b" * 64
    with pytest.raises(module.ImageEvidenceError, match="subject"):
        module.sanitize_verification_document(
            raw,
            expected_repository=REPOSITORY,
            expected_image_digest=IMAGE_DIGEST,
            expected_predicate_type=SLSA_PREDICATE,
        )


def test_registry_verification_accepts_multiple_compliant_attestations_and_rejects_empty() -> None:
    module = _load_evidence_module()
    statement = {
        "subject": [
            {
                "name": REPOSITORY,
                "digest": {"sha256": IMAGE_DIGEST.removeprefix("sha256:")},
            }
        ],
        "predicateType": SLSA_PREDICATE,
        "predicate": {"buildDefinition": {"externalParameters": {}}},
    }
    raw = [
        {
            "attestation": {"bundle": index},
            "verificationResult": {
                "signature": {
                    "certificate": {"subjectAlternativeName": f"trusted-workflow-{index}"}
                },
                "verifiedTimestamps": [{"type": "transparency-log", "index": index}],
                "statement": statement,
            },
        }
        for index in (1, 2)
    ]

    sanitized = module.sanitize_verification_document(
        raw,
        expected_repository=REPOSITORY,
        expected_image_digest=IMAGE_DIGEST,
        expected_predicate_type=SLSA_PREDICATE,
    )

    assert len(sanitized["verified_results"]) == 2
    assert "buildDefinition" not in json.dumps(sanitized)
    assert "bundle" not in json.dumps(sanitized)
    with pytest.raises(module.ImageEvidenceError, match="at least one"):
        module.sanitize_verification_document(
            [],
            expected_repository=REPOSITORY,
            expected_image_digest=IMAGE_DIGEST,
            expected_predicate_type=SLSA_PREDICATE,
        )

    raw[1]["verificationResult"]["statement"] = {
        **statement,
        "predicateType": CYCLONEDX_PREDICATE,
    }
    with pytest.raises(module.ImageEvidenceError, match="predicate"):
        module.sanitize_verification_document(
            raw,
            expected_repository=REPOSITORY,
            expected_image_digest=IMAGE_DIGEST,
            expected_predicate_type=SLSA_PREDICATE,
        )


def test_cyclonedx_verification_must_sign_the_downloadable_sbom() -> None:
    module = _load_evidence_module()
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "components": [],
    }
    raw = [
        {
            "attestation": {},
            "verificationResult": {
                "signature": {"certificate": {"subjectAlternativeName": "trusted-workflow"}},
                "verifiedTimestamps": [{"type": "transparency-log"}],
                "statement": {
                    "subject": [
                        {
                            "name": REPOSITORY,
                            "digest": {"sha256": IMAGE_DIGEST.removeprefix("sha256:")},
                        }
                    ],
                    "predicateType": CYCLONEDX_PREDICATE,
                    "predicate": sbom,
                },
            },
        }
    ]

    module.sanitize_verification_document(
        raw,
        expected_repository=REPOSITORY,
        expected_image_digest=IMAGE_DIGEST,
        expected_predicate_type=CYCLONEDX_PREDICATE,
        expected_cyclonedx_sbom=sbom,
    )
    with pytest.raises(module.ImageEvidenceError, match="downloadable CycloneDX"):
        module.sanitize_verification_document(
            raw,
            expected_repository=REPOSITORY,
            expected_image_digest=IMAGE_DIGEST,
            expected_predicate_type=CYCLONEDX_PREDICATE,
            expected_cyclonedx_sbom={**sbom, "version": 2},
        )


def test_signed_evidence_archive_is_safely_extracted_and_semantically_verified(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    evidence_dir = tmp_path / "evidence"
    record_path = evidence_dir / f"{IMAGE_ID}.image.json"
    _write_base_evidence(evidence_dir)
    _create_record(module, evidence_dir, record_path)
    for filename, predicate_type in (
        (f"{IMAGE_ID}.assembly.provenance.verification.json", SLSA_PREDICATE),
        (f"{IMAGE_ID}.assembly.sbom.verification.json", CYCLONEDX_PREDICATE),
        (f"{IMAGE_ID}.assembly.registry.provenance.verification.json", SLSA_PREDICATE),
        (f"{IMAGE_ID}.assembly.registry.sbom.verification.json", CYCLONEDX_PREDICATE),
    ):
        (evidence_dir / filename).write_text(
            json.dumps(_verification_document(predicate_type)), encoding="utf-8"
        )
    module.add_assembly_verifications(
        evidence_dir=evidence_dir,
        record_path=record_path,
        expected_image_id=IMAGE_ID,
        expected_repository=REPOSITORY,
        expected_release_tag=RELEASE_TAG,
        expected_source_commit=SOURCE_COMMIT,
    )
    archive_path = tmp_path / "evidence.tar.gz"
    archive_root = f"auris-flow-{RELEASE_TAG}-evidence"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(evidence_dir, arcname=f"{archive_root}/images")

    verified = module.verify_image_evidence_archive(
        archive_path=archive_path,
        release_tag=RELEASE_TAG,
        source_commit=SOURCE_COMMIT,
        image_repositories={IMAGE_ID: REPOSITORY},
    )

    assert verified == {IMAGE_ID: f"{REPOSITORY}:{RELEASE_TAG}@{IMAGE_DIGEST}"}

    traversal_archive = tmp_path / "traversal.tar.gz"
    with tarfile.open(traversal_archive, "w:gz") as archive:
        payload = b"escaped\n"
        member = tarfile.TarInfo(f"{archive_root}/images/../escaped.txt")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    with pytest.raises(module.ImageEvidenceError, match="unsafe path"):
        module.verify_image_evidence_archive(
            archive_path=traversal_archive,
            release_tag=RELEASE_TAG,
            source_commit=SOURCE_COMMIT,
            image_repositories={IMAGE_ID: REPOSITORY},
        )
