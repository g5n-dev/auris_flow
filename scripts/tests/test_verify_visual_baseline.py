from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from verify_visual_baseline import (  # noqa: E402
    BaselineValidationError,
    _verify_repository_binding,
    assert_update_target_safe,
    create_artifact_package,
    materialize_locked_baseline,
    playwright_lock_version,
    promote_oci_baseline,
    release_runtime_contract,
    resolve_visual_execution_policy,
    runner_contract_sha256,
    validate_baseline,
    validate_visual_baseline_lock,
    verify_visual_artifact_signature,
    verify_visual_oci_provenance,
    write_visual_baseline_lock,
    write_visual_evidence,
    write_manifest,
)


SCREENSHOT_COUNT = 76


def _release_runtime_descriptor() -> dict[str, object]:
    contract = release_runtime_contract()
    return {
        "runtime_kind": "pinned-playwright-container",
        "platform": contract["platform"],
        "runner_image": contract["runner_image"],
        "runner_contract_sha256": runner_contract_sha256(),
        "playwright_version": playwright_lock_version(),
        "browser_name": "chromium",
        "browser_version": "141.0.7390.37",
        "node_version": "v22.22.1",
        "os_release": "Ubuntu 24.04.3 LTS",
        "reproducibility_scope": contract["reproducibility_scope"],
    }


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)
    )


def _write_png(path: Path, *, width: int = 1440, height: int = 900) -> None:
    scanline = b"\x00" + (b"\xff\xff\xff" * width)
    payload = b"".join(scanline for _ in range(height))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
        )
        + _png_chunk(b"IDAT", zlib.compress(payload, level=9))
        + _png_chunk(b"IEND", b"")
    )


def _create_baseline(root: Path) -> Path:
    baseline = root / "visual-regression"
    screenshots = baseline / "screenshots"
    screenshots.mkdir(parents=True)
    geometry: dict[str, object] = {}
    for index in range(SCREENSHOT_COUNT):
        name = f"shot-{index:03d}.png"
        _write_png(screenshots / name)
        geometry[name] = {
            "shell": {"x": 0, "y": 0, "width": 1440, "height": 900},
            "sidebar": {"x": 0, "y": 0, "width": 88, "height": 900},
            "topbar": {"x": 88, "y": 0, "width": 1352, "height": 54},
            "workbench": {"x": 88, "y": 0, "width": 1352, "height": 900},
            "tabs": {"x": 102, "y": 155, "width": 1324, "height": 42},
            "selectedTab": {"x": 107, "y": 160, "width": 116, "height": 32},
        }
    (baseline / "geometry.json").write_text(
        json.dumps(geometry, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (baseline / "seed-overlay.json").write_text(
        json.dumps(
            {
                "scene_profile_versions": [
                    {
                        "scene_profile_version_id": "scenev_test",
                        "manifest": {"task_type_refs": ["task_test"]},
                        "expected_manifest_sha256": "a" * 64,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return baseline


class VisualBaselineManifestTests(unittest.TestCase):
    def test_write_manifest_is_deterministic_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            baseline = _create_baseline(Path(temp_dir))

            write_manifest(
                baseline,
                reference_platform="test-os-test-arch",
                playwright_version="1.61.1",
            )
            first = (baseline / "manifest.json").read_bytes()
            write_manifest(
                baseline,
                reference_platform="test-os-test-arch",
                playwright_version="1.61.1",
            )

            self.assertEqual(first, (baseline / "manifest.json").read_bytes())
            self.assertEqual([], validate_baseline(baseline))

    def test_detects_tampered_png_and_invalid_magic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            baseline = _create_baseline(Path(temp_dir))
            write_manifest(
                baseline,
                reference_platform="test-os-test-arch",
                playwright_version="1.61.1",
            )
            screenshot = baseline / "screenshots/shot-000.png"
            content = screenshot.read_bytes()
            screenshot.write_bytes(b"BROKEN!!" + content[8:])

            failures = validate_baseline(baseline)

            self.assertTrue(
                any("PNG signature" in failure for failure in failures), failures
            )
            self.assertTrue(any("sha256 mismatch" in failure for failure in failures))

    def test_detects_wrong_png_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            baseline = _create_baseline(Path(temp_dir))
            _write_png(baseline / "screenshots/shot-075.png", width=1439)
            write_manifest(
                baseline,
                reference_platform="test-os-test-arch",
                playwright_version="1.61.1",
                validate_dimensions=False,
            )

            failures = validate_baseline(baseline)

            self.assertTrue(any("must be 1440x900" in failure for failure in failures))

    def test_detects_geometry_and_screenshot_inventory_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            baseline = _create_baseline(Path(temp_dir))
            write_manifest(
                baseline,
                reference_platform="test-os-test-arch",
                playwright_version="1.61.1",
            )
            geometry_path = baseline / "geometry.json"
            geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
            geometry["unexpected.png"] = geometry.pop("shot-075.png")
            geometry_path.write_text(
                json.dumps(geometry, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            failures = validate_baseline(baseline)

            self.assertTrue(
                any(
                    "geometry/screenshot inventory mismatch" in failure
                    for failure in failures
                ),
                failures,
            )
            self.assertTrue(any("sha256 mismatch" in failure for failure in failures))

    def test_rejects_manifest_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            baseline = _create_baseline(Path(temp_dir))
            write_manifest(
                baseline,
                reference_platform="test-os-test-arch",
                playwright_version="1.61.1",
            )
            manifest_path = baseline / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][0]["path"] = "../../outside.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            failures = validate_baseline(baseline)

            self.assertTrue(
                any("path escapes baseline" in failure for failure in failures),
                failures,
            )

    def test_rejects_playwright_version_that_drifted_from_package_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            baseline = _create_baseline(Path(temp_dir))
            write_manifest(
                baseline,
                reference_platform="test-os-test-arch",
                playwright_version=playwright_lock_version(),
            )
            manifest_path = baseline / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["reference_environment"]["playwright_version"] = "0.0.0"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )

            failures = validate_baseline(baseline)

            self.assertTrue(
                any("package-lock" in failure for failure in failures), failures
            )

    def test_release_baseline_requires_exact_pinned_container_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            baseline = _create_baseline(Path(temp_dir))
            runtime = _release_runtime_descriptor()
            write_manifest(baseline, runtime_descriptor=runtime)

            self.assertEqual(
                [],
                validate_baseline(
                    baseline,
                    runtime_descriptor=runtime,
                    require_release_runtime=True,
                ),
            )

            drifted_runtime = {**runtime, "browser_version": "999.0.0.0"}
            failures = validate_baseline(
                baseline,
                runtime_descriptor=drifted_runtime,
                require_release_runtime=True,
            )
            self.assertTrue(
                any("runtime descriptor" in failure for failure in failures), failures
            )

    def test_release_baseline_rejects_host_diagnostics_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            baseline = _create_baseline(Path(temp_dir))
            write_manifest(
                baseline,
                reference_platform="test-os-test-arch",
                playwright_version=playwright_lock_version(),
            )

            failures = validate_baseline(baseline, require_release_runtime=True)

            self.assertTrue(
                any("pinned Playwright container" in failure for failure in failures),
                failures,
            )


class ImmutableVisualArtifactTests(unittest.TestCase):
    source_commit = "1" * 40
    artifact_ref = "ghcr.io/auris-flow/auris-flow/visual-baseline@sha256:" + ("2" * 64)
    signature_identity = (
        "https://github.com/auris-flow/auris-flow/.github/workflows/"
        "visual-baseline-build.yml@refs/heads/main"
    )
    signature_issuer = "https://token.actions.githubusercontent.com"

    def _release_candidate(self, root: Path) -> tuple[Path, Path]:
        baseline = _create_baseline(root)
        runtime = _release_runtime_descriptor()
        write_manifest(
            baseline,
            runtime_descriptor=runtime,
            source_commit=self.source_commit,
        )
        package_path = root / "visual-baseline.tar"
        create_artifact_package(baseline, package_path)
        return baseline, package_path

    def _approved_lock(self, root: Path, baseline: Path, package_path: Path) -> Path:
        lock_path = root / "visual-baseline.lock.json"
        write_visual_baseline_lock(
            lock_path,
            baseline_dir=baseline,
            package_path=package_path,
            artifact_ref=self.artifact_ref,
            approval_reference="fixture-approval-123",
            signature_identity=self.signature_identity,
            signature_issuer=self.signature_issuer,
        )
        return lock_path

    def test_pending_lock_is_fail_closed_before_any_registry_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock_path = root / "visual-baseline.lock.json"
            lock_path.write_text(
                json.dumps(
                    {
                        "artifact": None,
                        "kind": "auris-flow-visual-baseline-lock",
                        "reason": "No approved linux/amd64 artifact yet.",
                        "schema_version": 1,
                        "status": "PENDING",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            failures = validate_visual_baseline_lock(lock_path, require_approved=True)
            self.assertTrue(any("PENDING" in failure for failure in failures), failures)

            with patch("verify_visual_baseline.subprocess.run") as run:
                with self.assertRaises(BaselineValidationError):
                    materialize_locked_baseline(
                        lock_path,
                        root / "materialized",
                        check_repository_binding=False,
                    )
                run.assert_not_called()

    def test_lock_rejects_mutable_or_non_ghcr_artifact_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline, package_path = self._release_candidate(root)
            for invalid_ref in (
                "ghcr.io/auris-flow/auris-flow/visual-baseline:latest",
                "docker.io/auris-flow/visual-baseline@sha256:" + ("2" * 64),
                "ghcr.io/auris-flow/auris-flow/visual-baseline@sha256:short",
                "ghcr.io/auris-flow/visual-baseline@sha256:" + ("2" * 64),
            ):
                with self.subTest(invalid_ref=invalid_ref):
                    with self.assertRaises(BaselineValidationError):
                        write_visual_baseline_lock(
                            root / "invalid.lock.json",
                            baseline_dir=baseline,
                            package_path=package_path,
                            artifact_ref=invalid_ref,
                            approval_reference="fixture-approval-123",
                            signature_identity=self.signature_identity,
                            signature_issuer=self.signature_issuer,
                        )

    def test_cosign_verification_is_bound_to_the_exact_build_workflow(self) -> None:
        completed = subprocess.CompletedProcess(["cosign", "verify"], 0, "verified", "")
        with patch(
            "verify_visual_baseline.subprocess.run", return_value=completed
        ) as run:
            verify_visual_artifact_signature(
                self.artifact_ref,
                signature_identity=self.signature_identity,
                signature_issuer=self.signature_issuer,
            )
        command = run.call_args.args[0]
        self.assertIn("--certificate-identity", command)
        self.assertIn(self.signature_identity, command)
        self.assertIn("--certificate-oidc-issuer", command)
        self.assertIn(self.signature_issuer, command)

        failed = subprocess.CompletedProcess(["cosign", "verify"], 1, "", "bad")
        with (
            patch("verify_visual_baseline.subprocess.run", return_value=failed),
            self.assertRaisesRegex(BaselineValidationError, "untrusted workflow"),
        ):
            verify_visual_artifact_signature(
                self.artifact_ref,
                signature_identity=self.signature_identity,
                signature_issuer=self.signature_issuer,
            )

    def test_approved_lock_rejects_untrusted_signature_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline, package_path = self._release_candidate(root)
            lock_path = self._approved_lock(root, baseline, package_path)
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            payload["artifact"]["signature_identity"] = (
                "https://github.com/example/example/.github/workflows/"
                "release-images.yml@refs/heads/main"
            )
            lock_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            failures = validate_visual_baseline_lock(lock_path, require_approved=True)

            self.assertTrue(
                any("signature_identity" in failure for failure in failures), failures
            )

    def test_approved_lock_rejects_workflow_sha_not_bound_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline, package_path = self._release_candidate(root)
            lock_path = self._approved_lock(root, baseline, package_path)
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            payload["artifact"]["job_workflow_sha"] = "9" * 40
            lock_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            failures = validate_visual_baseline_lock(lock_path, require_approved=True)

            self.assertTrue(
                any("job_workflow_sha" in failure for failure in failures), failures
            )

    def test_oci_provenance_requires_exact_workflow_and_source_annotations(
        self,
    ) -> None:
        valid = json.dumps(
            {
                "annotations": {
                    "org.opencontainers.image.revision": self.source_commit,
                    "io.auris.visual.job-workflow-sha": self.source_commit,
                }
            }
        )
        with patch(
            "verify_visual_baseline.subprocess.run",
            return_value=subprocess.CompletedProcess(["oras"], 0, valid, ""),
        ):
            verify_visual_oci_provenance(
                self.artifact_ref, source_commit=self.source_commit
            )

        tampered = json.dumps(
            {
                "annotations": {
                    "org.opencontainers.image.revision": self.source_commit,
                    "io.auris.visual.job-workflow-sha": "9" * 40,
                }
            }
        )
        with (
            patch(
                "verify_visual_baseline.subprocess.run",
                return_value=subprocess.CompletedProcess(["oras"], 0, tampered, ""),
            ),
            self.assertRaisesRegex(BaselineValidationError, "job-workflow-sha"),
        ):
            verify_visual_oci_provenance(
                self.artifact_ref, source_commit=self.source_commit
            )

    def test_lock_rejects_signer_from_a_different_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline, package_path = self._release_candidate(root)
            lock_path = self._approved_lock(root, baseline, package_path)
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            payload["artifact"]["signature_identity"] = (
                "https://github.com/attacker/auris-flow/.github/workflows/"
                "visual-baseline-build.yml@refs/heads/main"
            )
            lock_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            failures = validate_visual_baseline_lock(lock_path, require_approved=True)

            self.assertTrue(
                any("same GitHub repository" in failure for failure in failures),
                failures,
            )

    def test_lock_writer_rejects_matching_foreign_repository_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline, package_path = self._release_candidate(root)
            foreign_ref = "ghcr.io/attacker/evil/visual-baseline@sha256:" + ("2" * 64)
            foreign_identity = (
                "https://github.com/attacker/evil/.github/workflows/"
                "visual-baseline-build.yml@refs/heads/main"
            )

            with self.assertRaisesRegex(
                BaselineValidationError, "official Auris Flow repository"
            ):
                write_visual_baseline_lock(
                    root / "foreign.lock.json",
                    baseline_dir=baseline,
                    package_path=package_path,
                    artifact_ref=foreign_ref,
                    approval_reference="fixture-approval-123",
                    signature_identity=foreign_identity,
                    signature_issuer=self.signature_issuer,
                )

    def test_signature_verifier_rejects_foreign_repository_before_cosign(
        self,
    ) -> None:
        foreign_ref = "ghcr.io/attacker/evil/visual-baseline@sha256:" + ("2" * 64)
        foreign_identity = (
            "https://github.com/attacker/evil/.github/workflows/"
            "visual-baseline-build.yml@refs/heads/main"
        )

        with (
            patch("verify_visual_baseline.subprocess.run") as run,
            self.assertRaisesRegex(
                BaselineValidationError, "official Auris Flow repository"
            ),
        ):
            verify_visual_artifact_signature(
                foreign_ref,
                signature_identity=foreign_identity,
                signature_issuer=self.signature_issuer,
            )
        run.assert_not_called()

    def test_lock_writer_does_not_follow_an_output_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline, package_path = self._release_candidate(root)
            target = root / "outside.json"
            target.write_text("untouched\n", encoding="utf-8")
            lock_path = root / "visual-baseline.lock.json"
            lock_path.symlink_to(target)

            with self.assertRaisesRegex(BaselineValidationError, "regular file"):
                write_visual_baseline_lock(
                    lock_path,
                    baseline_dir=baseline,
                    package_path=package_path,
                    artifact_ref=self.artifact_ref,
                    approval_reference="fixture-approval-123",
                    signature_identity=self.signature_identity,
                    signature_issuer=self.signature_issuer,
                )

            self.assertEqual("untouched\n", target.read_text(encoding="utf-8"))

    def test_materializes_exact_package_and_verifies_manifest_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline, package_path = self._release_candidate(root)
            lock_path = self._approved_lock(root, baseline, package_path)
            canonical_seed = baseline / "seed-overlay.json"

            def fake_oras(
                args: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                output_dir = Path(args[args.index("--output") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(package_path, output_dir / "visual-baseline.tar")
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch("verify_visual_baseline.subprocess.run", side_effect=fake_oras):
                materialized = materialize_locked_baseline(
                    lock_path,
                    root / "materialized",
                    canonical_seed_path=canonical_seed,
                    check_repository_binding=False,
                )

            self.assertEqual(
                [], validate_baseline(materialized, require_release_runtime=True)
            )
            self.assertEqual(
                (baseline / "manifest.json").read_bytes(),
                (materialized / "manifest.json").read_bytes(),
            )

    def test_strict_materialization_rejects_signature_before_registry_pull(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline, package_path = self._release_candidate(root)
            lock_path = self._approved_lock(root, baseline, package_path)
            failed = subprocess.CompletedProcess(
                ["cosign", "verify"], 1, "", "untrusted"
            )

            with (
                patch(
                    "verify_visual_baseline.subprocess.run", return_value=failed
                ) as run,
                self.assertRaisesRegex(BaselineValidationError, "untrusted workflow"),
            ):
                materialize_locked_baseline(
                    lock_path,
                    root / "materialized",
                    canonical_seed_path=baseline / "seed-overlay.json",
                    check_repository_binding=False,
                    verify_signature=True,
                )

            self.assertEqual("cosign", run.call_args.args[0][0])
            self.assertFalse((root / "materialized").exists())

    def test_rejects_downloaded_package_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline, package_path = self._release_candidate(root)
            lock_path = self._approved_lock(root, baseline, package_path)
            tampered = root / "tampered.tar"
            tampered.write_bytes(package_path.read_bytes() + b"tampered")

            def fake_oras(
                args: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                output_dir = Path(args[args.index("--output") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(tampered, output_dir / "visual-baseline.tar")
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch("verify_visual_baseline.subprocess.run", side_effect=fake_oras):
                with self.assertRaisesRegex(BaselineValidationError, "package sha256"):
                    materialize_locked_baseline(
                        lock_path,
                        root / "materialized",
                        canonical_seed_path=baseline / "seed-overlay.json",
                        check_repository_binding=False,
                    )

    def test_rejects_tar_symlink_and_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline, package_path = self._release_candidate(root)
            lock_path = self._approved_lock(root, baseline, package_path)
            hostile = root / "hostile.tar"
            with tarfile.open(hostile, "w") as archive:
                traversal = tarfile.TarInfo("../../outside")
                traversal.size = 0
                archive.addfile(traversal)
                symlink = tarfile.TarInfo("screenshots/link.png")
                symlink.type = tarfile.SYMTYPE
                symlink.linkname = "/etc/passwd"
                archive.addfile(symlink)

            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            import hashlib

            lock["artifact"]["package_sha256"] = hashlib.sha256(
                hostile.read_bytes()
            ).hexdigest()
            lock_path.write_text(json.dumps(lock) + "\n", encoding="utf-8")

            def fake_oras(
                args: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                output_dir = Path(args[args.index("--output") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(hostile, output_dir / "visual-baseline.tar")
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch("verify_visual_baseline.subprocess.run", side_effect=fake_oras):
                with self.assertRaises(BaselineValidationError):
                    materialize_locked_baseline(
                        lock_path,
                        root / "materialized",
                        canonical_seed_path=baseline / "seed-overlay.json",
                        check_repository_binding=False,
                    )
            self.assertFalse((root / "outside").exists())

    def test_evidence_is_written_only_for_an_approved_verified_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline, package_path = self._release_candidate(root)
            lock_path = self._approved_lock(root, baseline, package_path)
            evidence_path = root / "visual-regression.json"
            verified = subprocess.CompletedProcess(
                ["cosign", "verify"], 0, "verified", ""
            )

            def verified_provenance(
                args: list[str] | tuple[str, ...], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                if args[0] == "cosign":
                    return verified
                manifest = {
                    "annotations": {
                        "org.opencontainers.image.revision": self.source_commit,
                        "io.auris.visual.job-workflow-sha": self.source_commit,
                    }
                }
                return subprocess.CompletedProcess(args, 0, json.dumps(manifest), "")

            with patch(
                "verify_visual_baseline.subprocess.run",
                side_effect=verified_provenance,
            ):
                evidence = write_visual_evidence(
                    lock_path,
                    baseline,
                    evidence_path,
                    canonical_seed_path=baseline / "seed-overlay.json",
                    check_repository_binding=False,
                )

            self.assertEqual("ok", evidence["status"])
            self.assertEqual(76, evidence["scenario_count"])
            self.assertEqual(76, evidence["passed"])
            self.assertEqual("sha256:" + ("2" * 64), evidence["baseline_oci_digest"])
            self.assertEqual(self.source_commit, evidence["source_commit"])
            self.assertEqual(self.source_commit, evidence["baseline_source_commit"])
            self.assertEqual(self.signature_identity, evidence["signature_identity"])
            self.assertEqual(self.signature_issuer, evidence["signature_issuer"])
            self.assertEqual(self.source_commit, evidence["job_workflow_sha"])

            release_commit = "3" * 40
            with (
                patch(
                    "verify_visual_baseline._verify_repository_binding",
                    return_value=release_commit,
                ),
                patch(
                    "verify_visual_baseline.subprocess.run",
                    side_effect=verified_provenance,
                ),
            ):
                release_evidence = write_visual_evidence(
                    lock_path,
                    baseline,
                    evidence_path,
                    canonical_seed_path=baseline / "seed-overlay.json",
                    check_repository_binding=True,
                )
            self.assertEqual(release_commit, release_evidence["source_commit"])
            self.assertEqual(
                self.source_commit, release_evidence["baseline_source_commit"]
            )

            pending_lock = root / "pending.lock.json"
            pending_lock.write_text(
                json.dumps(
                    {
                        "artifact": None,
                        "kind": "auris-flow-visual-baseline-lock",
                        "reason": "pending",
                        "schema_version": 1,
                        "status": "PENDING",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            evidence_path.write_text('{"status":"ok"}\n', encoding="utf-8")
            with self.assertRaises(BaselineValidationError):
                write_visual_evidence(
                    pending_lock,
                    baseline,
                    evidence_path,
                    canonical_seed_path=baseline / "seed-overlay.json",
                    check_repository_binding=False,
                )
            self.assertFalse(evidence_path.exists())

            evidence_path.write_text('{"status":"ok"}\n', encoding="utf-8")
            failed = subprocess.CompletedProcess(
                ["cosign", "verify"], 1, "", "untrusted"
            )
            with (
                patch("verify_visual_baseline.subprocess.run", return_value=failed),
                self.assertRaisesRegex(BaselineValidationError, "untrusted workflow"),
            ):
                write_visual_evidence(
                    lock_path,
                    baseline,
                    evidence_path,
                    canonical_seed_path=baseline / "seed-overlay.json",
                    check_repository_binding=False,
                )
            self.assertFalse(evidence_path.exists())

    def test_promotion_repulls_the_digest_and_writes_only_an_approved_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline, package_path = self._release_candidate(root)
            lock_path = root / "visual-baseline.lock.json"

            def fake_tools(
                args: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                if args[0] == "cosign":
                    self.assertEqual("verify", args[1])
                    self.assertIn(self.signature_identity, args)
                    self.assertIn(self.signature_issuer, args)
                    return subprocess.CompletedProcess(args, 0, "verified", "")
                if tuple(args[1:3]) == ("manifest", "fetch"):
                    manifest = {
                        "annotations": {
                            "org.opencontainers.image.revision": self.source_commit,
                            "io.auris.visual.job-workflow-sha": self.source_commit,
                        }
                    }
                    return subprocess.CompletedProcess(
                        args, 0, json.dumps(manifest), ""
                    )
                output_dir = Path(args[args.index("--output") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(package_path, output_dir / "visual-baseline.tar")
                return subprocess.CompletedProcess(args, 0, "", "")

            with (
                patch(
                    "verify_visual_baseline._verify_repository_binding",
                    return_value=self.source_commit,
                ),
                patch("verify_visual_baseline.subprocess.run", side_effect=fake_tools),
            ):
                promoted = promote_oci_baseline(
                    artifact_ref=self.artifact_ref,
                    source_commit=self.source_commit,
                    approval_reference="fixture-approval-123",
                    signature_identity=self.signature_identity,
                    signature_issuer=self.signature_issuer,
                    lock_path=lock_path,
                    canonical_seed_path=baseline / "seed-overlay.json",
                )

            self.assertEqual("APPROVED", promoted["status"])
            self.assertEqual(self.artifact_ref, promoted["artifact"]["reference"])
            self.assertEqual(
                self.signature_identity,
                promoted["artifact"]["signature_identity"],
            )
            self.assertEqual(
                promoted, json.loads(lock_path.read_text(encoding="utf-8"))
            )

    def test_promotion_rejects_untrusted_signature_before_downloading(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock_path = root / "visual-baseline.lock.json"
            failed = subprocess.CompletedProcess(
                ["cosign", "verify"], 1, "", "untrusted"
            )
            with (
                patch(
                    "verify_visual_baseline._verify_repository_binding",
                    return_value=self.source_commit,
                ),
                patch(
                    "verify_visual_baseline.subprocess.run", return_value=failed
                ) as run,
                self.assertRaisesRegex(BaselineValidationError, "untrusted workflow"),
            ):
                promote_oci_baseline(
                    artifact_ref=self.artifact_ref,
                    source_commit=self.source_commit,
                    approval_reference="fixture-approval-123",
                    signature_identity=self.signature_identity,
                    signature_issuer=self.signature_issuer,
                    lock_path=lock_path,
                )

            self.assertEqual(1, run.call_count)
            self.assertEqual("cosign", run.call_args.args[0][0])
            self.assertFalse(lock_path.exists())

    def test_repository_binding_allows_later_non_visual_commit_but_not_visual_drift(
        self,
    ) -> None:
        release_commit = "3" * 40
        clean_results = (
            subprocess.CompletedProcess(["git", "merge-base"], 0, "", ""),
            subprocess.CompletedProcess(["git", "diff"], 0, "", ""),
        )
        with (
            patch(
                "verify_visual_baseline._current_source_commit",
                return_value=release_commit,
            ),
            patch(
                "verify_visual_baseline.subprocess.run", side_effect=clean_results
            ) as run,
        ):
            self.assertEqual(
                release_commit, _verify_repository_binding(self.source_commit)
            )
        diff_command = run.call_args_list[1].args[0]
        self.assertIn("prototype/auris-flow-ui/src", diff_command)
        self.assertIn("backend/app", diff_command)
        self.assertNotIn("README.md", diff_command)

        drift_results = (
            subprocess.CompletedProcess(["git", "merge-base"], 0, "", ""),
            subprocess.CompletedProcess(
                ["git", "diff"],
                0,
                "prototype/auris-flow-ui/src/App.tsx\n",
                "",
            ),
        )
        with (
            patch(
                "verify_visual_baseline._current_source_commit",
                return_value=release_commit,
            ),
            patch("verify_visual_baseline.subprocess.run", side_effect=drift_results),
            self.assertRaisesRegex(BaselineValidationError, "visual inputs changed"),
        ):
            _verify_repository_binding(self.source_commit)


class VisualExecutionPolicyTests(unittest.TestCase):
    def test_frozen_verification_rejects_every_override_and_host_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            default_goal = root / "tracked-baselines"
            diagnostics = root / "e2e/artifacts"
            frozen = default_goal / "visual-regression"
            frozen.mkdir(parents=True)
            diagnostics.mkdir(parents=True)

            for overrides in (
                {"goal_dir": diagnostics / "candidate"},
                {"seed_overlay": diagnostics / "seed.json"},
                {"runtime": "host"},
            ):
                with self.subTest(overrides=overrides):
                    with self.assertRaises(BaselineValidationError):
                        resolve_visual_execution_policy(
                            release_check=False,
                            update=False,
                            default_goal_dir=default_goal,
                            frozen_root=default_goal,
                            diagnostics_root=diagnostics,
                            **overrides,
                        )

    def test_release_mode_rejects_update_even_in_diagnostics_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            default_goal = root / "tracked-baselines"
            diagnostics = root / "e2e/artifacts"
            default_goal.mkdir()
            diagnostics.mkdir(parents=True)

            with self.assertRaises(
                BaselineValidationError, msg="release update must fail"
            ):
                resolve_visual_execution_policy(
                    release_check=True,
                    update=True,
                    goal_dir=diagnostics / "candidate",
                    default_goal_dir=default_goal,
                    frozen_root=default_goal,
                    diagnostics_root=diagnostics,
                )

    def test_candidate_update_requires_independent_diagnostics_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            default_goal = root / "tracked-baselines"
            diagnostics = root / "e2e/artifacts"
            default_goal.mkdir()
            diagnostics.mkdir(parents=True)

            for invalid_goal in (None, default_goal, root / "outside-diagnostics"):
                with self.subTest(invalid_goal=invalid_goal):
                    with self.assertRaises(BaselineValidationError):
                        resolve_visual_execution_policy(
                            release_check=False,
                            update=True,
                            goal_dir=invalid_goal,
                            default_goal_dir=default_goal,
                            frozen_root=default_goal,
                            diagnostics_root=diagnostics,
                        )

            policy = resolve_visual_execution_policy(
                release_check=False,
                update=True,
                goal_dir=diagnostics / "candidate",
                runtime="host",
                default_goal_dir=default_goal,
                frozen_root=default_goal,
                diagnostics_root=diagnostics,
            )
            self.assertEqual("host", policy.runtime)
            self.assertEqual(
                (diagnostics / "candidate/visual-regression").resolve(),
                policy.visual_dir,
            )

    def test_shell_delegates_fail_closed_policy_to_the_tested_validator(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "visual_regression.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("verify_visual_baseline.py check-execution-policy", source)
        self.assertIn("--release-check", source)
        self.assertIn("--diagnostics-root", source)
        self.assertIn("--runtime", source)


class VisualShellPolicyIntegrationTests(unittest.TestCase):
    def test_shell_rejects_release_and_frozen_override_bypasses_before_docker(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[2]
        script = root / "scripts/visual_regression.sh"
        diagnostics_goal = (
            root / "prototype/auris-flow-ui/e2e/artifacts/policy-negative-candidate"
        )
        cases = (
            {
                "AURIS_RELEASE_CHECK": "1",
                "AURIS_VISUAL_GOAL_DIR": str(diagnostics_goal),
            },
            {
                "AURIS_RELEASE_CHECK": "1",
                "AURIS_UPDATE_VISUAL_BASELINE": "1",
                "AURIS_VISUAL_GOAL_DIR": str(diagnostics_goal),
            },
            {"AURIS_VISUAL_RUNTIME": "host"},
            {"AURIS_VISUAL_SEED_OVERLAY": str(root / "README.md")},
            {"AURIS_VISUAL_GOAL_DIR": str(diagnostics_goal)},
            {"AURIS_UPDATE_VISUAL_BASELINE": "1"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                environment = os.environ.copy()
                for name in (
                    "AURIS_RELEASE_CHECK",
                    "AURIS_UPDATE_VISUAL_BASELINE",
                    "AURIS_VISUAL_GOAL_DIR",
                    "AURIS_VISUAL_SEED_OVERLAY",
                    "AURIS_VISUAL_RUNTIME",
                ):
                    environment.pop(name, None)
                environment.update(overrides)
                environment["PYTHON"] = sys.executable
                result = subprocess.run(
                    ["bash", str(script)],
                    cwd=root,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn("visual baseline:", result.stderr)

    def test_release_files_pin_and_require_the_container_runtime(self) -> None:
        root = Path(__file__).resolve().parents[2]
        contract = release_runtime_contract()
        dockerfile = (root / "production/visual/Dockerfile").read_text(encoding="utf-8")
        shell = (root / "scripts/visual_regression.sh").read_text(encoding="utf-8")
        vite_config = (root / "prototype/auris-flow-ui/vite.config.ts").read_text(
            encoding="utf-8"
        )
        workflow = (root / ".github/workflows/verify.yml").read_text(encoding="utf-8")
        readiness = (root / "scripts/check_platform_readiness.py").read_text(
            encoding="utf-8"
        )

        self.assertIn(f"FROM {contract['runner_image']}", dockerfile)
        self.assertNotIn(":latest", dockerfile)
        self.assertIn("--platform linux/amd64", shell)
        self.assertIn("materialize-locked", shell)
        self.assertIn("--verify-signature", shell)
        self.assertIn("production/visual/visual-baseline.lock.json", shell)
        self.assertIn("production/visual/seed-overlay.json", shell)
        self.assertIn("write-evidence", shell)
        self.assertIn("build/release-evidence/visual-regression.json", shell)
        self.assertIn("--require-release-runtime", shell)
        self.assertIn("--runtime-descriptor", shell)
        self.assertNotIn("FROZEN_BASELINE_ROOT", shell)
        self.assertIn(
            "--env AURIS_VISUAL_ARTIFACT_DIR=/artifacts/test-results", shell
        )
        self.assertIn(
            '--volume "${CONTAINER_ARTIFACT_DIR}:/artifacts:rw"', shell
        )
        self.assertNotRegex(
            shell,
            r"--env AURIS_VISUAL_ARTIFACT_DIR=/artifacts\s+\\",
        )
        self.assertIn('ALLOW_DOCKER_HOST_PREVIEW="1"', shell)
        self.assertIn(
            'AURIS_ALLOW_DOCKER_HOST_PREVIEW="${ALLOW_DOCKER_HOST_PREVIEW}"',
            shell,
        )
        self.assertIn(
            'process.env.AURIS_ALLOW_DOCKER_HOST_PREVIEW === "1"', vite_config
        )
        self.assertIn('["host.docker.internal"]', vite_config)
        self.assertIn("allowedHosts: previewAllowedHosts", vite_config)
        self.assertNotIn("allowedHosts: true", vite_config)
        self.assertIn('AURIS_VISUAL_RUNTIME: "container"', workflow)
        self.assertIn(
            "oras-project/setup-oras@8d34698a59f5ffe24821f0b48ab62a3de8b64b20",
            workflow,
        )
        self.assertIn('version: "1.2.3"', workflow)
        self.assertIn("validate_visual_baseline_lock", readiness)
        self.assertIn("production/visual/Dockerfile", readiness)

    def test_generated_visual_outputs_are_ignored_and_not_required_as_source(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[2]
        gitignore = (root / ".gitignore").read_text(encoding="utf-8")
        readiness = (root / "scripts/check_platform_readiness.py").read_text(
            encoding="utf-8"
        )

        for ignored in (
            "prototype/auris-flow-ui/test-baselines/",
            "prototype/auris-flow-ui/e2e/screenshots/",
            "prototype/auris-flow-ui/test-results/",
            "prototype/auris-flow-ui/playwright-report/",
        ):
            self.assertIn(ignored, gitignore)
        self.assertNotIn(
            '"prototype/auris-flow-ui/test-baselines/visual-regression/geometry.json"',
            readiness,
        )
        self.assertNotIn(
            '"prototype/auris-flow-ui/test-baselines/visual-regression/screenshots/',
            readiness,
        )
        self.assertIn('"production/visual/visual-baseline.lock.json"', readiness)
        self.assertIn('"production/visual/seed-overlay.json"', readiness)

    def test_repository_lock_and_two_stage_promotion_gate_are_valid(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[2]
        lock_path = root / "production/visual/visual-baseline.lock.json"
        promotion_script = root / "scripts/promote_visual_baseline.sh"
        promotion_workflow = root / ".github/workflows/visual-baseline-promotion.yml"
        build_workflow = root / ".github/workflows/visual-baseline-build.yml"

        # The repository lock may be PENDING before promotion or a contract-bound
        # APPROVED lock afterward. Both must keep the regular test suite green;
        # release readiness separately requires APPROVED and fails closed.
        failures = validate_visual_baseline_lock(lock_path)
        self.assertEqual([], failures)
        promotion = promotion_script.read_text(encoding="utf-8")
        self.assertIn("promote-oci", promotion)
        self.assertIn("--signature-identity", promotion)
        self.assertIn("--signature-issuer", promotion)
        workflow = promotion_workflow.read_text(encoding="utf-8")
        self.assertIn("visual-baseline-production", workflow)
        self.assertIn("permissions:\n  contents: read\n  packages: read", workflow)
        self.assertIn("Merely naming this", workflow)
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", workflow)
        self.assertIn('"${SOURCE_COMMIT}" != "${default_tip}"', workflow)
        self.assertIn("PROMOTION_WORKFLOW_SHA: ${{ github.workflow_sha }}", workflow)
        self.assertNotIn("git merge-base --is-ancestor", workflow)
        for pinned_action in (
            "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
            "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
            "oras-project/setup-oras@8d34698a59f5ffe24821f0b48ab62a3de8b64b20",
            "docker/login-action@74a5d142397b4f367a81961eba4e8cd7edddf772",
            "sigstore/cosign-installer@d58896d6a1865668819e1d91763c7751a165e159",
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        ):
            self.assertIn(pinned_action, workflow)
        self.assertNotRegex(workflow, r"uses:\s+[^\s]+@v\d")
        self.assertIn('cosign-release: "v2.5.3"', workflow)
        for forbidden in (
            "contents: write",
            "packages: write",
            "id-token: write",
            "pull-requests: write",
            "git push",
        ):
            self.assertNotIn(forbidden, workflow)

        build = build_workflow.read_text(encoding="utf-8")
        self.assertIn("environment: visual-baseline-build", build)
        self.assertIn("runs-on: ubuntu-24.04", build)
        self.assertIn("id-token: write", build)
        self.assertIn("packages: write", build)
        self.assertIn("ref: ${{ inputs.source_commit }}", build)
        self.assertIn("source_commit must equal the reviewed default-branch tip", build)
        self.assertIn("WORKFLOW_SOURCE_COMMIT: ${{ github.workflow_sha }}", build)
        self.assertIn("io.auris.visual.job-workflow-sha", build)
        self.assertIn("job_workflow_sha", build)
        self.assertNotIn("git merge-base --is-ancestor", build)
        self.assertIn('AURIS_UPDATE_VISUAL_BASELINE: "1"', build)
        self.assertIn("AURIS_VISUAL_RUNTIME: container", build)
        self.assertIn("create-package", build)
        self.assertIn("oras push", build)
        self.assertIn("cosign sign --yes", build)
        self.assertIn("cosign verify", build)
        self.assertIn("GITHUB_WORKFLOW_REF", build)
        self.assertIn("artifact_ref=${artifact_ref}", build)
        self.assertNotIn("visual-baseline.lock.json", build)
        for pinned_action in (
            "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
            "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
            "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020",
            "oras-project/setup-oras@8d34698a59f5ffe24821f0b48ab62a3de8b64b20",
            "docker/login-action@74a5d142397b4f367a81961eba4e8cd7edddf772",
            "sigstore/cosign-installer@d58896d6a1865668819e1d91763c7751a165e159",
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        ):
            self.assertIn(pinned_action, build)
        self.assertNotRegex(build, r"uses:\s+[^\s]+@v\d")
        self.assertIn('cosign-release: "v2.5.3"', build)

        validator = (root / "scripts/verify_visual_baseline.py").read_text(
            encoding="utf-8"
        )
        for contract_input in (
            '".github/workflows/visual-baseline-build.yml"',
            '".github/workflows/visual-baseline-promotion.yml"',
            '"scripts/promote_visual_baseline.sh"',
            '"scripts/verify_visual_baseline.py"',
            '"scripts/visual_regression.sh"',
        ):
            self.assertIn(contract_input, validator)


class FrozenUpdateTargetTests(unittest.TestCase):
    def test_rejects_exact_or_descendant_frozen_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            frozen_root = root / "tracked-baselines"
            frozen_root.mkdir()

            with self.assertRaises(BaselineValidationError):
                assert_update_target_safe(
                    frozen_root / "visual-regression", frozen_root
                )
            with self.assertRaises(BaselineValidationError):
                assert_update_target_safe(
                    frozen_root / "diagnostics/visual-regression", frozen_root
                )

    def test_rejects_symlink_and_dotdot_alias_to_frozen_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            frozen_root = root / "tracked-baselines"
            frozen_target = frozen_root / "visual-regression"
            frozen_target.mkdir(parents=True)
            alias = root / "baseline-alias"
            alias.symlink_to(frozen_root, target_is_directory=True)

            with self.assertRaises(BaselineValidationError):
                assert_update_target_safe(
                    root / "scratch/../tracked-baselines/visual-regression",
                    frozen_root,
                )
            with self.assertRaises(BaselineValidationError):
                assert_update_target_safe(alias / "visual-regression", frozen_root)

    def test_allows_separate_diagnostics_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            frozen_root = root / "tracked-baselines"
            frozen_root.mkdir()

            assert_update_target_safe(
                root / "artifacts/visual-candidate/visual-regression",
                frozen_root,
            )

    def test_rejects_nested_symlink_from_diagnostics_to_frozen_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            frozen_root = root / "tracked-baselines"
            frozen_screenshots = frozen_root / "visual-regression/screenshots"
            frozen_screenshots.mkdir(parents=True)
            candidate = root / "artifacts/visual-candidate/visual-regression"
            candidate.mkdir(parents=True)
            (candidate / "screenshots").symlink_to(
                frozen_screenshots, target_is_directory=True
            )

            with self.assertRaises(BaselineValidationError):
                assert_update_target_safe(candidate, frozen_root)


if __name__ == "__main__":
    unittest.main()
