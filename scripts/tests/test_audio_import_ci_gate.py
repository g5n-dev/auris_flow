from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
RELEASE_GATE = ROOT / "scripts" / "verify_release.sh"
DEFAULT_GATE = ROOT / "scripts" / "verify_all.sh"
STACK_GATE = ROOT / "scripts" / "verify_audio_import_stack.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "audio-import-real-stack.yml"
FINALIZER = ROOT / "scripts" / "finalize_release_evidence.py"
TOPOLOGY_GATE = ROOT / "scripts" / "verify_audio_import_gate_compose.py"


def _load_finalizer():
    spec = importlib.util.spec_from_file_location(
        "audio_import_ci_finalizer",
        FINALIZER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_topology_gate():
    spec = importlib.util.spec_from_file_location(
        "audio_import_topology_gate",
        TOPOLOGY_GATE,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AudioImportCiGateTests(unittest.TestCase):
    def test_release_executes_audio_import_real_stack_gate_exactly_once(self) -> None:
        source = RELEASE_GATE.read_text(encoding="utf-8")
        executable = [
            line.strip()
            for line in source.splitlines()
            if line.strip() == "bash scripts/verify_audio_import_stack.sh"
        ]

        self.assertEqual(["bash scripts/verify_audio_import_stack.sh"], executable)
        self.assertIn(
            'if [ "${AURIS_SKIP_AUDIO_IMPORT_REAL_STACK_GATE:-0}" = "1" ]; then',
            source,
        )
        self.assertIn(
            "AURIS_SKIP_AUDIO_IMPORT_REAL_STACK_GATE=1 is not allowed",
            source,
        )
        self.assertLess(
            source.index("bash scripts/verify_audio_import_stack.sh"),
            source.index("scripts/generate_supply_chain_evidence.py"),
        )

    def test_default_verify_all_does_not_start_the_heavy_audio_import_stack(
        self,
    ) -> None:
        source = DEFAULT_GATE.read_text(encoding="utf-8")

        self.assertNotIn("verify_audio_import_stack.sh", source)

    def test_dedicated_workflow_is_path_scoped_nightly_and_release_mandatory(
        self,
    ) -> None:
        document = yaml.load(
            WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
        )
        self.assertIsInstance(document, dict)
        triggers = document["on"]
        self.assertIsInstance(triggers, dict)

        pull_request = triggers["pull_request"]
        self.assertIsInstance(pull_request, dict)
        paths = pull_request["paths"]
        self.assertIsInstance(paths, list)
        for required_path in (
            "production/dagster/**",
            "backend/app/services/audio_import_*.py",
            "backend/app/services/audio_playback_service.py",
            "backend/app/services/audio_evidence_review_service.py",
            "backend/app/services/audio_intelligence_service.py",
            "backend/app/services/audio_session_orchestration_service.py",
            "backend/app/services/human_review_service.py",
            "backend/app/workers/outbox_worker.py",
            "backend/app/services/adapters.py",
            "backend/app/services/platform_connection_service.py",
            "prototype/auris-flow-ui/src/features/data/DataModule.tsx",
            "prototype/auris-flow-ui/src/features/data/audioImport*.ts",
            "prototype/auris-flow-ui/src/features/data/components/AudioImport*.tsx",
            "prototype/auris-flow-ui/src/features/listening/**",
            "prototype/auris-flow-ui/src/styles/features/data/audio-import.css",
            "scripts/verify_audio_import_*.sh",
        ):
            self.assertIn(required_path, paths)

        schedules = triggers["schedule"]
        self.assertIsInstance(schedules, list)
        self.assertEqual(1, len(schedules))
        self.assertRegex(str(schedules[0]["cron"]), r"^\d+ \d+ \* \* \*$")
        self.assertEqual(["published"], triggers["release"]["types"])
        self.assertIn("workflow_dispatch", triggers)

        jobs = document["jobs"]
        self.assertEqual(["audio-import-real-stack"], list(jobs))
        job = jobs["audio-import-real-stack"]
        self.assertNotIn("continue-on-error", job)
        self.assertEqual("ubuntu-24.04", job["runs-on"])
        self.assertTrue(int(job["timeout-minutes"]) >= 30)
        steps = job["steps"]
        run_steps = [
            step
            for step in steps
            if isinstance(step, dict)
            and "bash scripts/verify_audio_import_stack.sh" in step.get("run", "")
        ]
        self.assertEqual(1, len(run_steps))
        self.assertEqual(
            "0",
            run_steps[0]["env"]["AURIS_SKIP_AUDIO_IMPORT_REAL_STACK_GATE"],
        )
        self.assertTrue(
            any(
                isinstance(step, dict)
                and str(step.get("uses", "")).startswith("actions/upload-artifact@")
                and step.get("if") == "always()"
                for step in steps
            )
        )

    def test_browser_gate_requires_post_import_vertical_chain(self) -> None:
        source = STACK_GATE.read_text(encoding="utf-8")
        browser = (ROOT / "prototype/auris-flow-ui/e2e/platform-bff.mjs").read_text(
            encoding="utf-8"
        )

        self.assertIn("auris.audio-import-browser-e2e.v2", source)
        self.assertIn("runImportedAudioIntelligenceReviewClosedLoop", browser)
        for required_marker in (
            "intelligenceRunId",
            "evidencePackId",
            "reviewTaskId",
            "reviewDecisionId",
            "traceRootMatched",
            "noSeedSwitch",
        ):
            self.assertIn(required_marker, browser)

    def test_final_release_evidence_requires_both_audio_import_proofs(self) -> None:
        finalizer = _load_finalizer()

        self.assertIn("audio-import-real-stack-gate.json", finalizer.CORE_EVIDENCE)
        self.assertIn("audio-import-browser-e2e.json", finalizer.CORE_EVIDENCE)
        self.assertTrue(callable(finalizer._validate_audio_import_real_stack))
        self.assertTrue(callable(finalizer._validate_audio_import_browser))

    def test_stack_gate_binds_browser_evidence_to_the_same_source_tree(self) -> None:
        source = STACK_GATE.read_text(encoding="utf-8")

        self.assertIn('artifact["source_commit"] = expected_commit', source)
        self.assertIn('artifact["source_tree_dirty"] = expected_dirty', source)
        self.assertIn(
            'browser_artifact.get("source_commit") != expected_commit',
            source,
        )
        self.assertIn(
            'browser_artifact.get("source_tree_dirty") is not expected_dirty',
            source,
        )

    def test_worker_and_bff_must_share_the_frozen_object_storage_scope(self) -> None:
        topology = _load_topology_gate()
        bff_environment = dict(topology.SHARED_STORAGE_ENVIRONMENT)
        worker_environment = dict(topology.SHARED_STORAGE_ENVIRONMENT)

        topology._validate_shared_storage_scope(
            bff_environment,
            worker_environment,
        )

        worker_environment["OBJECT_STORAGE_BUCKET"] = "auris-flow-local"
        with self.assertRaisesRegex(
            topology.GateTopologyError,
            "outbox worker real audio-import storage scope is incomplete",
        ):
            topology._validate_shared_storage_scope(
                bff_environment,
                worker_environment,
            )


if __name__ == "__main__":
    unittest.main()
