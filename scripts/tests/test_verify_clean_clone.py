from __future__ import annotations

import os
import json
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


SCRIPT_UNDER_TEST = Path(__file__).resolve().parents[1] / "verify_clean_clone.sh"
RELEASE_SCRIPT_UNDER_TEST = Path(__file__).resolve().parents[1] / "verify_release.sh"
WORKFLOW_UNDER_TEST = (
    Path(__file__).resolve().parents[2] / ".github/workflows/verify.yml"
)
CODEQL_WORKFLOW_UNDER_TEST = (
    Path(__file__).resolve().parents[2] / ".github/workflows/codeql.yml"
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class CleanCloneGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="auris_clean_clone_test_")
        self.root = Path(self._temp.name)
        self.source = self.root / "source"
        self.fake_bin = self.root / "fake-bin"
        self.temp_parent = self.root / "temporary-clones"
        self.command_log = self.root / "commands.log"
        self.source.mkdir()
        self.fake_bin.mkdir()
        self.temp_parent.mkdir()

        required_files = {
            ".gitignore": "**/.venv/\nnode_modules/\ndist/\n*.pyc\n__pycache__/\n",
            "README.md": "clean-clone fixture\n",
            "backend/pyproject.toml": "[project]\nname='fixture-backend'\nversion='1.0.0'\n",
            "backend/uv.lock": "version = 1\n",
            "backend/app/__init__.py": "",
            "backend/scripts/verify_migrations.py": "# fixture\n",
            "backend/scripts/smoke_backend.py": "# fixture\n",
            "backend/migrations/versions/0001_fixture.py": "# fixture\n",
            "backend/tests/unit/test_unit.py": "",
            "backend/tests/contract/test_contract.py": "",
            "backend/tests/integration/test_integration.py": "",
            "production/dagster/pyproject.toml": "[project]\nname='fixture-dagster'\nversion='1.0.0'\n",
            "production/dagster/uv.lock": "version = 1\n",
            "production/dagster/src/fixture.py": "",
            "production/dagster/tests/test_fixture.py": "",
            "production/tests/test_compose.py": "",
            "prototype/auris-flow-ui/package.json": '{"scripts":{"build":"true","bundle:check":"true"}}\n',
            "prototype/auris-flow-ui/package-lock.json": '{"lockfileVersion":3}\n',
            "prototype/auris-flow-ui/src/main.ts": "",
            "doc/backend-spec/validate_backend_spec.py": "# fixture\n",
            "scripts/validate_public_audio_datasets.py": "# fixture\n",
            "scripts/check_platform_readiness.py": "# fixture\n",
            "scripts/scan_secrets.py": "# fixture\n",
            "scripts/verify_production_compose.py": "# fixture\n",
        }
        for relative_path, body in required_files.items():
            target = self.source / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")

        shutil.copy2(SCRIPT_UNDER_TEST, self.source / "scripts/verify_clean_clone.sh")
        (self.source / "scripts/verify_clean_clone.sh").chmod(0o755)
        self._write_fake_tools()
        self._git("init", "-q")
        self._git("config", "user.email", "clean-clone-test@example.invalid")
        self._git("config", "user.name", "Clean Clone Test")
        self._git("add", ".")
        self._git("commit", "-qm", "fixture")
        self.commit = self._git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.source,
            check=True,
            text=True,
            capture_output=True,
        )

    def _write_fake_tools(self) -> None:
        _write_executable(
            self.fake_bin / "uv",
            r"""
            #!/bin/sh
            set -eu
            if [ "${1:-}" = "--version" ]; then
              echo "uv 0.10.0 (fixture)"
              exit 0
            fi
            printf 'uv:%s\n' "$*" >> "${AURIS_CLEAN_CLONE_TEST_LOG:?}"
            project=""
            previous=""
            for argument in "$@"; do
              if [ "${previous}" = "--project" ]; then
                project="${argument}"
              fi
              previous="${argument}"
            done
            if [ "${1:-}" = "sync" ] && [ -n "${project}" ]; then
              mkdir -p "${project}/.venv/bin"
              cat > "${project}/.venv/bin/python" <<'PYTHON'
            #!/bin/sh
            set -eu
            printf 'python:%s\n' "$*" >> "${AURIS_CLEAN_CLONE_TEST_LOG:?}"
            if [ "${AURIS_CLEAN_CLONE_TEST_FAIL_RELEASE_READINESS:-0}" = "1" ] &&
               [ "${1:-}" = "scripts/check_platform_readiness.py" ] &&
               [ "${2:-}" = "--release" ]; then
              exit 9
            fi
            exit 0
            PYTHON
              chmod +x "${project}/.venv/bin/python"
            fi
            """,
        )
        _write_executable(
            self.fake_bin / "node",
            r"""
            #!/bin/sh
            if [ "${1:-}" = "--version" ]; then
              echo "v22.22.1"
              exit 0
            fi
            exit 0
            """,
        )
        _write_executable(
            self.fake_bin / "npm",
            r"""
            #!/bin/sh
            set -eu
            if [ "${1:-}" = "--version" ]; then
              echo "10.9.4"
              exit 0
            fi
            printf 'npm:%s\n' "$*" >> "${AURIS_CLEAN_CLONE_TEST_LOG:?}"
            if [ "${AURIS_CLEAN_CLONE_TEST_CREATE_UNTRACKED:-0}" = "1" ]; then
              touch unexpected-generated.txt
            fi
            exit 0
            """,
        )

    def _run_gate(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.fake_bin}{os.pathsep}{environment['PATH']}",
                "AURIS_CLEAN_CLONE_CACHE_DIR": str(self.root / "shared-cache"),
                "AURIS_CLEAN_CLONE_TEMP_PARENT": str(self.temp_parent),
                "AURIS_CLEAN_CLONE_TEST_LOG": str(self.command_log),
                "AURIS_CLEAN_CLONE_EVIDENCE": str(self.root / "clean-clone.json"),
            }
        )
        environment.update(extra_env)
        return subprocess.run(
            ["bash", "scripts/verify_clean_clone.sh"],
            cwd=self.source,
            env=environment,
            check=False,
            text=True,
            capture_output=True,
        )

    def test_rebuilds_and_verifies_the_exact_committed_head(self) -> None:
        result = self._run_gate()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(self.commit, result.stdout)
        self.assertIn("clean clone verification ok", result.stdout)
        self.assertEqual([], list(self.temp_parent.iterdir()))
        self.assertTrue((self.root / "shared-cache/uv").is_dir())
        self.assertTrue((self.root / "shared-cache/npm").is_dir())
        evidence = json.loads(
            (self.root / "clean-clone.json").read_text(encoding="utf-8")
        )
        self.assertEqual("auris.clean-clone-evidence.v1", evidence["schema_version"])
        self.assertEqual("ok", evidence["status"])
        self.assertEqual(self.commit, evidence["source_commit"])
        self.assertEqual("functional-locked-source", evidence["reproducibility_scope"])
        self.assertIn(
            "static-analysis-and-script-policy-tests", evidence["verified_steps"]
        )
        self.assertIn("frontend-architecture-tests", evidence["verified_steps"])

        command_log = self.command_log.read_text(encoding="utf-8")
        for required_invocation in (
            "uv:lock --check --project backend",
            "uv:sync --frozen --all-extras --project backend --python 3.12",
            "uv:lock --check --project production/dagster",
            "uv:sync --frozen --all-extras --project production/dagster --python 3.12",
            "python:-m ruff format --check backend scripts production/tests",
            "python:-m ruff check backend scripts production/tests",
            "python:-m mypy backend/app backend/scripts/verify_migrations.py",
            "python:-m unittest discover -s scripts/tests -p test_*.py",
            "python:backend/scripts/verify_migrations.py",
            "python:-m pytest backend/tests/unit backend/tests/contract backend/tests/integration",
            "python:backend/scripts/smoke_backend.py",
            "npm:ci --ignore-scripts --prefix prototype/auris-flow-ui",
            "npm:run architecture:test --prefix prototype/auris-flow-ui",
            "npm:run architecture:final --prefix prototype/auris-flow-ui",
            "npm:run build --prefix prototype/auris-flow-ui",
            "npm:run bundle:check --prefix prototype/auris-flow-ui",
        ):
            self.assertIn(required_invocation, command_log)

    def test_fails_before_clone_when_source_worktree_is_dirty(self) -> None:
        (self.source / "README.md").write_text("dirty\n", encoding="utf-8")

        result = self._run_gate()

        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("source worktree is not clean", result.stderr)
        self.assertEqual([], list(self.temp_parent.iterdir()))
        self.assertFalse(self.command_log.exists())

    def test_accepts_a_clean_detached_head_like_actions_checkout(self) -> None:
        self._git("checkout", "--detach", self.commit)

        result = self._run_gate()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"clean clone verification ok: {self.commit}", result.stdout)
        self.assertEqual([], list(self.temp_parent.iterdir()))

    def test_default_gate_checks_base_readiness_without_requiring_release_authority(
        self,
    ) -> None:
        result = self._run_gate(AURIS_CLEAN_CLONE_TEST_FAIL_RELEASE_READINESS="1")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        command_log = self.command_log.read_text(encoding="utf-8")
        self.assertIn("python:scripts/check_platform_readiness.py\n", command_log)
        self.assertNotIn(
            "python:scripts/check_platform_readiness.py --release", command_log
        )
        evidence = json.loads(
            (self.root / "clean-clone.json").read_text(encoding="utf-8")
        )
        self.assertEqual("base", evidence["readiness_scope"])

    def test_strict_gate_requires_release_readiness(self) -> None:
        result = self._run_gate(
            AURIS_RELEASE_CHECK="1",
            AURIS_CLEAN_CLONE_TEST_FAIL_RELEASE_READINESS="1",
        )

        self.assertEqual(9, result.returncode, result.stdout + result.stderr)
        command_log = self.command_log.read_text(encoding="utf-8")
        self.assertIn(
            "python:scripts/check_platform_readiness.py --release", command_log
        )

    def test_release_entrypoint_enables_strict_clean_clone_readiness(self) -> None:
        release_source = RELEASE_SCRIPT_UNDER_TEST.read_text(encoding="utf-8")

        self.assertIn(
            "AURIS_RELEASE_CHECK=1 bash scripts/verify_clean_clone.sh",
            release_source,
        )

    def test_fails_when_build_creates_non_ignored_source_output(self) -> None:
        result = self._run_gate(AURIS_CLEAN_CLONE_TEST_CREATE_UNTRACKED="1")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("clone became dirty", result.stderr)
        self.assertIn("unexpected-generated.txt", result.stderr)
        self.assertEqual([], list(self.temp_parent.iterdir()))

    def test_does_not_recurse_into_the_full_release_or_browser_gates(self) -> None:
        source = SCRIPT_UNDER_TEST.read_text(encoding="utf-8")

        for forbidden in (
            "verify_all.sh",
            "verify_fast.sh",
            "verify_release.sh",
            "verify_real_stack.sh",
            "visual_regression.sh",
            "audit_ui.sh",
        ):
            self.assertNotIn(forbidden, source)


class CleanCloneWorkflowTests(unittest.TestCase):
    def test_push_verification_covers_the_current_default_release_branch(self) -> None:
        workflows = (
            WORKFLOW_UNDER_TEST.read_text(encoding="utf-8"),
            CODEQL_WORKFLOW_UNDER_TEST.read_text(encoding="utf-8"),
        )

        for workflow in workflows:
            self.assertIn('      - "codex/open-source-v1-candidate"', workflow)

    def test_ci_runs_the_clean_clone_gate_with_the_pinned_toolchain(self) -> None:
        workflow = WORKFLOW_UNDER_TEST.read_text(encoding="utf-8")

        self.assertIn("clean-clone-reproducibility:", workflow)
        self.assertIn("    needs: clean-clone-reproducibility", workflow)
        self.assertIn('node-version: "22"', workflow)
        self.assertIn('python-version: "3.12"', workflow)
        self.assertIn('python -m pip install "uv==0.10.0"', workflow)
        self.assertIn("python scripts/tests/test_verify_clean_clone.py", workflow)
        self.assertIn("bash scripts/verify_clean_clone.sh", workflow)
        self.assertIn("AURIS_CLEAN_CLONE_CACHE_DIR:", workflow)
        self.assertIn("backend/uv.lock", workflow)
        self.assertIn("production/dagster/uv.lock", workflow)
        self.assertIn("prototype/auris-flow-ui/package-lock.json", workflow)


if __name__ == "__main__":
    unittest.main()
