from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType


SCRIPT_UNDER_TEST = Path(__file__).resolve().parents[1] / "finalize_release_evidence.py"
SOURCE_COMMIT = "1" * 40
SHA256 = "a" * 64
TIMESTAMP = "2026-07-18T08:00:00+00:00"
SOURCE_INPUTS = (
    "backend/uv.lock",
    "config/release/license-review-exceptions.json",
    "production/dagster/uv.lock",
    "prototype/auris-flow-ui/package-lock.json",
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "finalize_release_evidence", SCRIPT_UNDER_TEST
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _outbox_events(prefix: str) -> list[dict[str, object]]:
    return [
        {
            "attempt_count": 1,
            "delivery_state": "confirmed",
            "dispatch_idempotency_key": f"{prefix}-{index}",
            "status": "processed",
        }
        for index in range(3)
    ]


class FinalReleaseEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()
        self.original_production_path_validator = (
            self.module.validate_production_path_evidence
        )
        self.production_path_validation_calls: list[
            tuple[dict[str, object], Path, str]
        ] = []

        def accept_production_path_fixture(
            evidence: object, *, root: Path, expected_commit: str
        ) -> list[str]:
            assert isinstance(evidence, dict)
            self.production_path_validation_calls.append(
                (evidence, root, expected_commit)
            )
            return []

        self.module.validate_production_path_evidence = accept_production_path_fixture
        self.temp = tempfile.TemporaryDirectory(prefix="auris_release_evidence_")
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.evidence = self.root / "evidence"
        self.repository.mkdir()
        self.evidence.mkdir()
        self._write_valid_fixture()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_valid_fixture(self) -> None:
        for path in self.evidence.iterdir():
            if path.is_symlink() or path.is_file():
                path.unlink()
        backend_lock = self.repository / "backend/uv.lock"
        backend_lock.parent.mkdir(parents=True, exist_ok=True)
        backend_lock.write_text(
            (
                "version = 1\n"
                "revision = 3\n"
                'requires-python = "==3.12.*"\n\n'
                "[[package]]\n"
                'name = "auris-flow-bff"\n'
                'version = "0.1.0"\n'
                'source = { virtual = "." }\n'
                'dependencies = [{ name = "backend" }]\n\n'
                "[[package]]\n"
                'name = "backend"\n'
                'version = "1"\n'
                'source = { registry = "https://pypi.org/simple" }\n'
                f'wheels = [{{ hash = "sha256:{SHA256}" }}]\n'
            ),
            encoding="utf-8",
        )
        dagster_lock = self.repository / "production/dagster/uv.lock"
        dagster_lock.parent.mkdir(parents=True, exist_ok=True)
        dagster_lock.write_text(
            (
                "version = 1\n"
                "revision = 3\n"
                'requires-python = "==3.12.*"\n\n'
                "[[package]]\n"
                'name = "auris-flow-dagster"\n'
                'version = "1.0.0"\n'
                'source = { editable = "." }\n'
                'dependencies = [{ name = "dagster" }]\n\n'
                "[[package]]\n"
                'name = "dagster"\n'
                'version = "1"\n'
                'source = { registry = "https://pypi.org/simple" }\n'
                f'wheels = [{{ hash = "sha256:{SHA256}" }}]\n'
            ),
            encoding="utf-8",
        )
        _write_json(
            self.repository / "config/release/license-review-exceptions.json",
            {
                "exceptions": [],
                "schema_version": "auris.license-review-exceptions.v1",
            },
        )
        _write_json(
            self.repository / "prototype/auris-flow-ui/package-lock.json",
            {
                "lockfileVersion": 3,
                "name": "auris-flow-ui",
                "packages": {
                    "": {"name": "auris-flow-ui", "version": "0.1.0"},
                    "node_modules/frontend": {
                        "license": "BSD-3-Clause",
                        "version": "1",
                    },
                },
                "requires": True,
                "version": "0.1.0",
            },
        )

        supply_artifacts = {
            "backend-python.cdx.json": {
                "bomFormat": "CycloneDX",
                "components": [
                    {
                        "licenses": [{"expression": "MIT"}],
                        "name": "backend",
                        "version": "1",
                    }
                ],
                "specVersion": "1.5",
            },
            "dagster-python.cdx.json": {
                "bomFormat": "CycloneDX",
                "components": [
                    {
                        "licenses": [{"expression": "Apache-2.0"}],
                        "name": "dagster",
                        "version": "1",
                    }
                ],
                "specVersion": "1.5",
            },
            "dependency-licenses.json": {
                "dependencies": [
                    {
                        "ecosystem": ecosystem,
                        "license": license_name,
                        "license_status": "approved-compatible",
                        "name": name,
                        "obligations": obligations,
                        "version": "1",
                    }
                    for ecosystem, name, license_name, obligations in (
                        (
                            "backend-python",
                            "backend",
                            "MIT",
                            ["retain-upstream-license-and-copyright-notices"],
                        ),
                        (
                            "dagster-python",
                            "dagster",
                            "Apache-2.0",
                            [
                                "preserve-apache-notice-and-state-changes",
                                "retain-upstream-license-and-copyright-notices",
                            ],
                        ),
                        (
                            "npm",
                            "frontend",
                            "BSD-3-Clause",
                            ["retain-upstream-license-and-copyright-notices"],
                        ),
                    )
                ],
                "policy": self.module.LICENSE_POLICY,
                "schema_version": "auris.dependency-license-inventory.v1",
            },
            "npm.cdx.json": {
                "bomFormat": "CycloneDX",
                "components": [
                    {
                        "licenses": [{"expression": "BSD-3-Clause"}],
                        "name": "frontend",
                        "version": "1",
                    }
                ],
                "specVersion": "1.5",
            },
        }
        for filename, payload in supply_artifacts.items():
            _write_json(self.evidence / filename, payload)
        _write_json(
            self.evidence / "evidence-manifest.json",
            {
                "artifacts": [
                    {
                        "path": filename,
                        "sha256": hashlib.sha256(
                            (self.evidence / filename).read_bytes()
                        ).hexdigest(),
                    }
                    for filename in sorted(supply_artifacts)
                ],
                "component_counts": {
                    "backend-python": 1,
                    "dagster-python": 1,
                    "npm": 1,
                    "total": 3,
                },
                "generator": {
                    "name": "auris-supply-chain-evidence",
                    "version": "2",
                },
                "schema_version": "auris.release-evidence-manifest.v1",
                "source_commit": SOURCE_COMMIT,
                "source_inputs": [
                    {
                        "path": relative_path,
                        "sha256": hashlib.sha256(
                            (self.repository / relative_path).read_bytes()
                        ).hexdigest(),
                    }
                    for relative_path in SOURCE_INPUTS
                ],
            },
        )
        _write_json(
            self.evidence / "clean-clone.json",
            {
                "completed_at": TIMESTAMP,
                "git_object_isolation": "clone-no-local-without-alternates",
                "readiness_scope": "release",
                "reproducibility_scope": "functional-locked-source",
                "schema_version": "auris.clean-clone-evidence.v1",
                "source_commit": SOURCE_COMMIT,
                "status": "ok",
                "toolchain": {
                    "node": "v24.4.1",
                    "npm": "11.4.2",
                    "python_request": "3.12",
                    "uv": "0.10.0",
                },
                "verified_steps": [
                    "locked-dependency-install",
                    "database-migrations",
                    "backend-tests-and-smoke",
                    "dagster-tests",
                    "frontend-build-and-bundle-policy",
                    "release-readiness",
                    "secret-history-scan",
                    "final-clean-tree",
                ],
            },
        )
        _write_json(
            self.evidence / "visual-regression.json",
            {
                "baseline_oci_digest": f"sha256:{SHA256}",
                "baseline_oci_ref": (
                    f"ghcr.io/auris-flow/auris-flow/visual-baseline@sha256:{SHA256}"
                ),
                "baseline_sha256": SHA256,
                "baseline_source_commit": "2" * 40,
                "job_workflow_sha": "2" * 40,
                "kind": "auris-flow-visual-regression-evidence",
                "manifest_sha256": SHA256,
                "passed": 76,
                "runner_contract_sha256": SHA256,
                "scenario_count": 76,
                "schema_version": 1,
                "signature_identity": (
                    "https://github.com/auris-flow/auris-flow/.github/workflows/"
                    "visual-baseline-build.yml@refs/heads/main"
                ),
                "signature_issuer": "https://token.actions.githubusercontent.com",
                "source_commit": SOURCE_COMMIT,
                "status": "ok",
            },
        )
        _write_json(
            self.evidence / "real-stack-gate.json",
            {
                "database": {
                    "artifact_ref": "artifact-1",
                    "backend": "mysql",
                    "registered_storage_object_count": 1,
                    "run_record_count": 1,
                },
                "execution_environment": "compose-dependencies",
                "http_range": {
                    "content_length": 32,
                    "invalid_range_status": 416,
                    "same_size_version_mismatch_status": 412,
                    "status": 206,
                },
                "object_storage": {
                    "dispatch_count": 1,
                    "metadata_registered": True,
                    "mode": "real",
                    "provider": "minio",
                    "storage_object_id": "sto_1",
                },
                "qdrant": {
                    "dispatch_count": 1,
                    "mode": "real_qdrant",
                    "recall_count": 1,
                },
                "rejected_fallback_markers": ["sqlite", "memory", "mock"],
                "run_id": "run-1",
                "schema_version": "auris.real-stack-gate.v1",
                "source_artifacts": {
                    "outbox_sha256": SHA256,
                    "ui_bff_sha256": SHA256,
                },
                "source_commit": SOURCE_COMMIT,
                "status": "ok",
                "validated_at": TIMESTAMP,
            },
        )
        workspace = {
            "dagster_version": "1.11.0",
            "job_name": "auris_flow_generic_job",
            "location_name": "auris_flow_defs",
            "repository_name": "__repository__",
        }
        daemons = [{"daemon_type": "SENSOR", "healthy": True, "required": True}]
        success_scenario = {
            "completion_body_sha256": SHA256,
            "completion_receipt_id": "dagster:success",
            "dagster_run_id": "dagster-success",
            "dagster_status": "SUCCESS",
            "reconciled": True,
            "response_typename": "LaunchRunSuccess",
            "selected_job_name": "auris_flow_generic_job",
        }
        failure_scenario = {
            **success_scenario,
            "completion_receipt_id": "dagster:failure",
            "dagster_run_id": "dagster-failure",
            "dagster_status": "FAILURE",
        }
        _write_json(
            self.evidence / "real-dagster-gate.json",
            {
                "completed_at": TIMESTAMP,
                "daemon_health": daemons,
                "daemon_health_after_restart": daemons,
                "excluded_scope": ["product-bff-cancel-and-reconcile-routes"],
                "execution_environment": "compose",
                "recovery": {
                    "canceled_completion_receipt_absent_after_restart": True,
                    "persisted_terminal_runs": ["success", "failure", "cancel"],
                    "post_restart_submission": {
                        "completion_body_sha256": SHA256,
                        "dagster_status": "SUCCESS",
                    },
                },
                "scenarios": {
                    "cancel": {
                        "completion_receipt_absent_after_cancel": True,
                        "dagster_status": "CANCELED",
                        "proof_scope": "dagster-engine-only",
                        "terminate_policy": "SAFE_TERMINATE",
                    },
                    "failure": failure_scenario,
                    "success": success_scenario,
                },
                "schema_version": "auris.real-dagster-gate.v1",
                "source_commit": SOURCE_COMMIT,
                "started_at": TIMESTAMP,
                "status": "ok",
                "workspace": workspace,
                "workspace_after_restart": workspace,
            },
        )
        _write_json(
            self.evidence / "product-dagster-gate.json",
            {
                "adapter_mode": "real",
                "execution_environment": "compose",
                "scenarios": {
                    "cancellation": {
                        "adapter_mode": "real",
                        "engine_status": "CANCELED",
                        "outbox_confirmed": True,
                        "outbox_events": _outbox_events("cancel"),
                        "status": "cancelled",
                        "status_version": 3,
                        "terminate_policy": "SAFE_TERMINATE",
                    },
                    "success": {
                        "adapter_mode": "real",
                        "outbox_confirmed": True,
                        "outbox_events": _outbox_events("success"),
                        "signed_completion": True,
                        "status": "success",
                        "status_sync": "SUCCESS",
                        "status_version": 2,
                    },
                },
                "schema_version": "auris.product-dagster-gate.v1",
                "scope": {"project_id": "sales_qa", "tenant_id": "aurora_auto"},
                "source_commit": SOURCE_COMMIT,
                "status": "ok",
                "verified_at": TIMESTAMP,
            },
        )
        _write_json(
            self.evidence / "production-path-gate.json",
            {
                "execution_environment": "production-compose",
                "producer": "scripts/verify_production_path_runtime.py",
                "schema_version": "auris.production-path-gate.v1",
                "source_commit": SOURCE_COMMIT,
                "status": "ok",
            },
        )

    def _finalize(self, **kwargs: object) -> dict[str, object]:
        return self.module.finalize_release_evidence(
            self.evidence,
            source_commit=SOURCE_COMMIT,
            check_repository_binding=False,
            repository_root=self.repository,
            **kwargs,
        )

    def _write_audits(self) -> None:
        (self.evidence / "backend-runtime-requirements.txt").write_text(
            f"backend==1 \\\n    --hash=sha256:{SHA256}\n", encoding="utf-8"
        )
        (self.evidence / "dagster-runtime-requirements.txt").write_text(
            f"dagster==1 \\\n    --hash=sha256:{SHA256}\n", encoding="utf-8"
        )
        for filename, package in (
            ("backend-python-audit.json", "backend"),
            ("dagster-python-audit.json", "dagster"),
        ):
            _write_json(
                self.evidence / filename,
                {
                    "dependencies": [{"name": package, "version": "1", "vulns": []}],
                    "fixes": [],
                },
            )
        _write_json(
            self.evidence / "npm-audit.json",
            {
                "auditReportVersion": 2,
                "metadata": {
                    "dependencies": {
                        "dev": 0,
                        "optional": 0,
                        "peer": 0,
                        "peerOptional": 0,
                        "prod": 1,
                        "total": 1,
                    },
                    "vulnerabilities": {
                        "critical": 0,
                        "high": 0,
                        "info": 0,
                        "low": 0,
                        "moderate": 0,
                        "total": 0,
                    },
                },
                "vulnerabilities": {},
            },
        )

    def _refresh_supply_hashes(self, *filenames: str) -> None:
        path = self.evidence / "evidence-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        selected = set(filenames)
        for artifact in manifest["artifacts"]:
            if artifact["path"] in selected:
                artifact["sha256"] = hashlib.sha256(
                    (self.evidence / artifact["path"]).read_bytes()
                ).hexdigest()
        _write_json(path, manifest)

    def _refresh_source_hashes(self, *relative_paths: str) -> None:
        path = self.evidence / "evidence-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        selected = set(relative_paths)
        for source_input in manifest["source_inputs"]:
            if source_input["path"] in selected:
                source_input["sha256"] = hashlib.sha256(
                    (self.repository / source_input["path"]).read_bytes()
                ).hexdigest()
        _write_json(path, manifest)

    @staticmethod
    def _review_exception_fields() -> dict[str, str]:
        return {
            "expires_on": "2027-07-18",
            "reason": "The exact package and locked version were reviewed separately.",
            "review_reference": "LIC-2026-BACKEND-1",
            "reviewed_by": "release-security",
            "reviewed_on": "2026-07-18",
        }

    def _forge_backend_reviewed_exception(self) -> None:
        inventory_path = self.evidence / "dependency-licenses.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        inventory["dependencies"][0].update(
            {
                "license": "GPL-2.0-only",
                "license_status": "reviewed-exception",
                "obligations": sorted(self.module.REVIEW_EXCEPTION_OBLIGATIONS),
                "review_exception": self._review_exception_fields(),
            }
        )
        _write_json(inventory_path, inventory)

        sbom_path = self.evidence / "backend-python.cdx.json"
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        sbom["components"][0]["licenses"] = [{"expression": "GPL-2.0-only"}]
        _write_json(sbom_path, sbom)
        self._refresh_supply_hashes(
            "backend-python.cdx.json", "dependency-licenses.json"
        )

    def test_final_manifest_hashes_only_commit_bound_verified_evidence(self) -> None:
        result = self._finalize()

        self.assertEqual("auris.release-gate-manifest.v1", result["schema_version"])
        self.assertEqual("ok", result["status"])
        self.assertEqual(SOURCE_COMMIT, result["source_commit"])
        artifacts = result["artifacts"]
        assert isinstance(artifacts, list)
        paths = [item["path"] for item in artifacts]
        self.assertEqual(sorted(paths), paths)
        self.assertNotIn("release-gate-manifest.json", paths)
        self.assertIn("product-dagster-gate.json", paths)
        self.assertIn("production-path-gate.json", paths)
        self.assertEqual(len(paths), result["artifact_count"])

    def test_visual_evidence_binds_workflow_commit_to_baseline_source(self) -> None:
        path = self.evidence / "visual-regression.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["job_workflow_sha"] = "9" * 40
        _write_json(path, payload)

        with self.assertRaisesRegex(self.module.EvidenceError, "job_workflow_sha"):
            self._finalize()

    def test_visual_evidence_rejects_matching_foreign_repository_identity(
        self,
    ) -> None:
        path = self.evidence / "visual-regression.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["baseline_oci_ref"] = (
            f"ghcr.io/attacker/evil/visual-baseline@sha256:{SHA256}"
        )
        payload["signature_identity"] = (
            "https://github.com/attacker/evil/.github/workflows/"
            "visual-baseline-build.yml@refs/heads/main"
        )
        _write_json(path, payload)

        with self.assertRaisesRegex(
            self.module.EvidenceError, "official Auris Flow repository"
        ):
            self._finalize()

    def test_clean_clone_requires_explicit_release_readiness_scope(self) -> None:
        path = self.evidence / "clean-clone.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["readiness_scope"] = "base"
        payload["verified_steps"] = [
            "base-readiness" if step == "release-readiness" else step
            for step in payload["verified_steps"]
        ]
        _write_json(path, payload)

        with self.assertRaisesRegex(
            self.module.EvidenceError, "readiness_scope must be release"
        ):
            self._finalize()

        self._write_valid_fixture()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["verified_steps"] = [
            "base-readiness" if step == "release-readiness" else step
            for step in payload["verified_steps"]
        ]
        _write_json(path, payload)
        with self.assertRaisesRegex(
            self.module.EvidenceError, "verified_steps are incomplete"
        ):
            self._finalize()

        self._write_valid_fixture()
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["readiness_scope"]
        _write_json(path, payload)
        with self.assertRaisesRegex(self.module.EvidenceError, "fields are invalid"):
            self._finalize()

    def test_production_path_is_mandatory_hashed_and_strictly_delegated(self) -> None:
        result = self._finalize()

        artifacts = result["artifacts"]
        assert isinstance(artifacts, list)
        self.assertIn(
            "production-path-gate.json",
            [item["path"] for item in artifacts],
        )
        self.assertEqual(1, len(self.production_path_validation_calls))
        payload, root, expected_commit = self.production_path_validation_calls[0]
        self.assertEqual("auris.production-path-gate.v1", payload["schema_version"])
        self.assertEqual(self.repository, root)
        self.assertEqual(SOURCE_COMMIT, expected_commit)

    def test_rejects_missing_or_forged_production_path_evidence(self) -> None:
        (self.evidence / "production-path-gate.json").unlink()
        with self.assertRaisesRegex(
            self.module.EvidenceError, "production-path-gate.json"
        ):
            self._finalize()

        self._write_valid_fixture()
        self.module.validate_production_path_evidence = (
            lambda evidence, *, root, expected_commit: [
                "raw runtime proof binding is invalid"
            ]
        )
        with self.assertRaisesRegex(
            self.module.EvidenceError, "raw runtime proof binding is invalid"
        ):
            self._finalize()

        self._write_valid_fixture()
        self.module.validate_production_path_evidence = (
            self.original_production_path_validator
        )
        with self.assertRaisesRegex(self.module.EvidenceError, "production path"):
            self._finalize()

    def test_rejects_source_drift_local_dagster_and_supply_tampering(self) -> None:
        cases = (
            (
                "source drift",
                "clean-clone.json",
                {"source_commit": "2" * 40},
                "source_commit",
            ),
            (
                "non-compose Dagster",
                "real-dagster-gate.json",
                {"execution_environment": "local-process"},
                "execution_environment",
            ),
            (
                "untrusted visual signer",
                "visual-regression.json",
                {
                    "signature_identity": (
                        "https://github.com/attacker/repo/.github/workflows/"
                        "untrusted.yml@refs/heads/main"
                    )
                },
                "signature identity",
            ),
            (
                "visual signer repository mismatch",
                "visual-regression.json",
                {
                    "signature_identity": (
                        "https://github.com/example/other/.github/workflows/"
                        "visual-baseline-build.yml@refs/heads/main"
                    )
                },
                "repository differ",
            ),
        )
        for label, filename, updates, message in cases:
            with self.subTest(label=label):
                self._write_valid_fixture()
                path = self.evidence / filename
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload.update(updates)
                _write_json(path, payload)
                with self.assertRaisesRegex(self.module.EvidenceError, message):
                    self._finalize()

        self._write_valid_fixture()
        (self.evidence / "dependency-licenses.json").write_text(
            '{"dependencies":["tampered"]}\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(self.module.EvidenceError, "sha256"):
            self._finalize()

    def test_rejects_minimal_self_report_and_source_input_drift(self) -> None:
        path = self.evidence / "clean-clone.json"
        _write_json(
            path,
            {
                "schema_version": "auris.clean-clone-evidence.v1",
                "source_commit": SOURCE_COMMIT,
                "status": "ok",
            },
        )
        with self.assertRaisesRegex(self.module.EvidenceError, "fields are invalid"):
            self._finalize()

        self._write_valid_fixture()
        (self.repository / SOURCE_INPUTS[0]).write_text("drift\n", encoding="utf-8")
        with self.assertRaisesRegex(self.module.EvidenceError, "source input sha256"):
            self._finalize()

    def test_rejects_absolute_paths_symlinks_and_unrecognized_artifacts(self) -> None:
        path = self.evidence / "real-stack-gate.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["diagnostic"] = "/srv/auris/private/result.json"
        _write_json(path, payload)
        with self.assertRaisesRegex(self.module.EvidenceError, "absolute path"):
            self._finalize()

        self._write_valid_fixture()
        (self.evidence / "unexpected.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(self.module.EvidenceError, "unrecognized"):
            self._finalize()

        self._write_valid_fixture()
        target = self.root / "clean-clone-target.json"
        (self.evidence / "clean-clone.json").replace(target)
        (self.evidence / "clean-clone.json").symlink_to(target)
        with self.assertRaisesRegex(self.module.EvidenceError, "regular file"):
            self._finalize()

    def test_official_release_requires_complete_clean_audits(self) -> None:
        with self.assertRaisesRegex(
            self.module.EvidenceError, "audit evidence is missing"
        ):
            self._finalize(require_audits=True)

        self._write_audits()
        result = self._finalize(require_audits=True)
        self.assertEqual("ok", result["status"])

        audit = self.evidence / "backend-python-audit.json"
        _write_json(
            audit,
            {
                "dependencies": [
                    {"name": "unsafe", "version": "1", "vulns": [{"id": "CVE"}]}
                ],
                "fixes": [],
            },
        )
        with self.assertRaisesRegex(
            self.module.EvidenceError, "unresolved vulnerabilities"
        ):
            self._finalize(require_audits=True)

    def test_rejects_partial_audit_evidence_even_before_official_finalization(
        self,
    ) -> None:
        _write_json(self.evidence / "npm-audit.json", {"metadata": {}})
        with self.assertRaisesRegex(
            self.module.EvidenceError, "audit evidence is partial"
        ):
            self._finalize()

    def test_rejects_forged_approved_status_for_unapproved_license(self) -> None:
        inventory_path = self.evidence / "dependency-licenses.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        inventory["dependencies"][0]["license"] = "GPL-2.0-only"
        _write_json(inventory_path, inventory)

        sbom_path = self.evidence / "backend-python.cdx.json"
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        sbom["components"][0]["licenses"] = [{"expression": "GPL-2.0-only"}]
        _write_json(sbom_path, sbom)
        self._refresh_supply_hashes(
            "backend-python.cdx.json", "dependency-licenses.json"
        )

        with self.assertRaisesRegex(self.module.EvidenceError, "outside the allowlist"):
            self._finalize()

    def test_rejects_forged_reviewed_exception_not_bound_to_committed_policy(
        self,
    ) -> None:
        self._forge_backend_reviewed_exception()

        with self.assertRaisesRegex(
            self.module.EvidenceError, "exact committed review exception"
        ):
            self._finalize()

    def test_accepts_only_the_exact_committed_review_exception(self) -> None:
        review_fields = self._review_exception_fields()
        exception_path = (
            self.repository / "config/release/license-review-exceptions.json"
        )
        _write_json(
            exception_path,
            {
                "exceptions": [
                    {
                        "ecosystem": "backend-python",
                        "name": "backend",
                        "version": "1",
                        **review_fields,
                    }
                ],
                "schema_version": "auris.license-review-exceptions.v1",
            },
        )
        self._refresh_source_hashes("config/release/license-review-exceptions.json")
        self._forge_backend_reviewed_exception()

        result = self._finalize()

        self.assertEqual("ok", result["status"])

    def test_rejects_review_proof_that_differs_from_the_committed_exception(
        self,
    ) -> None:
        review_fields = self._review_exception_fields()
        exception_path = (
            self.repository / "config/release/license-review-exceptions.json"
        )
        _write_json(
            exception_path,
            {
                "exceptions": [
                    {
                        "ecosystem": "backend-python",
                        "name": "backend",
                        "version": "1",
                        **review_fields,
                    }
                ],
                "schema_version": "auris.license-review-exceptions.v1",
            },
        )
        self._refresh_source_hashes("config/release/license-review-exceptions.json")
        self._forge_backend_reviewed_exception()
        inventory_path = self.evidence / "dependency-licenses.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        inventory["dependencies"][0]["review_exception"]["review_reference"] = (
            "LIC-FORGED"
        )
        _write_json(inventory_path, inventory)
        self._refresh_supply_hashes("dependency-licenses.json")

        with self.assertRaisesRegex(
            self.module.EvidenceError, "differs from its exact committed"
        ):
            self._finalize()

    def test_rejects_duplicate_and_unused_committed_review_exceptions(self) -> None:
        exception_path = (
            self.repository / "config/release/license-review-exceptions.json"
        )
        exact_exception = {
            "ecosystem": "backend-python",
            "name": "backend",
            "version": "1",
            **self._review_exception_fields(),
        }
        cases = (
            ("duplicate", [exact_exception, exact_exception], "duplicate"),
            ("unused", [exact_exception], "unused"),
        )
        for label, exceptions, message in cases:
            with self.subTest(label=label):
                self._write_valid_fixture()
                _write_json(
                    exception_path,
                    {
                        "exceptions": exceptions,
                        "schema_version": "auris.license-review-exceptions.v1",
                    },
                )
                self._refresh_source_hashes(
                    "config/release/license-review-exceptions.json"
                )
                with self.assertRaisesRegex(self.module.EvidenceError, message):
                    self._finalize()

    def test_rejects_same_count_dependency_graph_substituted_for_the_locks(
        self,
    ) -> None:
        sbom_path = self.evidence / "backend-python.cdx.json"
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        sbom["components"][0]["name"] = "counterfeit"
        _write_json(sbom_path, sbom)

        inventory_path = self.evidence / "dependency-licenses.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        inventory["dependencies"][0]["name"] = "counterfeit"
        _write_json(inventory_path, inventory)
        self._refresh_supply_hashes(
            "backend-python.cdx.json", "dependency-licenses.json"
        )

        with self.assertRaisesRegex(self.module.EvidenceError, "uv.lock closure"):
            self._finalize()

        self._write_valid_fixture()
        sbom_path = self.evidence / "npm.cdx.json"
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        sbom["components"][0]["name"] = "counterfeit"
        _write_json(sbom_path, sbom)
        inventory_path = self.evidence / "dependency-licenses.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        inventory["dependencies"][2]["name"] = "counterfeit"
        _write_json(inventory_path, inventory)
        self._refresh_supply_hashes("npm.cdx.json", "dependency-licenses.json")
        with self.assertRaisesRegex(self.module.EvidenceError, "package-lock closure"):
            self._finalize()

    def test_rejects_empty_or_wrong_python_audit_coverage(self) -> None:
        cases = (
            ("empty", []),
            (
                "substituted",
                [{"name": "counterfeit", "version": "1", "vulns": []}],
            ),
        )
        for label, dependencies in cases:
            with self.subTest(label=label):
                self._write_valid_fixture()
                self._write_audits()
                _write_json(
                    self.evidence / "backend-python-audit.json",
                    {"dependencies": dependencies, "fixes": []},
                )
                with self.assertRaisesRegex(
                    self.module.EvidenceError, "audit coverage"
                ):
                    self._finalize(require_audits=True)

    def test_rejects_runtime_requirements_or_npm_audit_not_bound_to_locks(
        self,
    ) -> None:
        self._write_audits()
        (self.evidence / "backend-runtime-requirements.txt").write_text(
            f"counterfeit==1 \\\n    --hash=sha256:{SHA256}\n",
            encoding="utf-8",
        )
        _write_json(
            self.evidence / "backend-python-audit.json",
            {
                "dependencies": [{"name": "counterfeit", "version": "1", "vulns": []}],
                "fixes": [],
            },
        )
        with self.assertRaisesRegex(self.module.EvidenceError, "runtime requirements"):
            self._finalize(require_audits=True)

        self._write_valid_fixture()
        self._write_audits()
        (self.evidence / "backend-runtime-requirements.txt").write_text(
            f"backend==1 \\\n    --hash=sha256:{'b' * 64}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            self.module.EvidenceError, "hashes do not match uv.lock"
        ):
            self._finalize(require_audits=True)

        self._write_valid_fixture()
        self._write_audits()
        audit_path = self.evidence / "npm-audit.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        audit["metadata"]["dependencies"]["total"] = 0
        _write_json(audit_path, audit)
        with self.assertRaisesRegex(self.module.EvidenceError, "npm audit coverage"):
            self._finalize(require_audits=True)


if __name__ == "__main__":
    unittest.main()
