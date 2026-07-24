from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_platform_readiness import (  # noqa: E402
    RELEASE_SKIP_GUARD_PREAMBLE,
    git_historical_files,
    git_staged_files,
    git_unstaged_files,
    historical_release_artifacts,
    validate_release_skip_guards,
)

RELEASE_SKIP_VARIABLES = (
    "AURIS_SKIP_REAL_STACK_E2E",
    "AURIS_SKIP_REAL_DAGSTER",
    "AURIS_SKIP_PRODUCT_DAGSTER_GATE",
    "AURIS_SKIP_PRODUCTION_PATH_GATE",
    "AURIS_SKIP_BACKUP_RESTORE_GATE",
)


def valid_release_skip_guards() -> str:
    guards = "".join(
        f"""\
if [ "${{{variable}:-0}}" = "1" ]; then
  echo "{variable}=1 is not allowed" >&2
  exit 2
fi
"""
        for variable in RELEASE_SKIP_VARIABLES
    )
    preamble = "\n".join(RELEASE_SKIP_GUARD_PREAMBLE)
    return (
        f'{preamble}\n{guards}\nif [ -L "${{ROOT}}/build" ]; then\n'
        '  echo "build must not be a symlink" >&2\n'
        "  exit 2\n"
        "fi\n\n"
        'if [ "${PRE_IMAGE_ONLY}" = true ]; then\n'
        "  exit 0\n"
        "fi\n"
    )


class PlatformReadinessGitTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="auris_readiness_git_")
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "readiness@example.invalid"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Readiness Test"],
            cwd=self.root,
            check=True,
        )
        (self.root / "tracked.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.root, check=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_unstaged_inventory_includes_deleted_paths(self) -> None:
        (self.root / "tracked.txt").unlink()

        self.assertEqual(("tracked.txt",), git_unstaged_files(self.root))

    def test_staged_inventory_detects_index_drift_from_head(self) -> None:
        (self.root / "tracked.txt").write_text("two\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.root, check=True)

        self.assertEqual(("tracked.txt",), git_staged_files(self.root))
        self.assertEqual((), git_unstaged_files(self.root))

    def test_history_inventory_keeps_deleted_release_artifacts_visible(self) -> None:
        artifact = self.root / "build" / "release-evidence" / "result.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("{}\n", encoding="utf-8")
        subprocess.run(["git", "add", str(artifact)], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "add generated artifact"],
            cwd=self.root,
            check=True,
        )
        artifact.unlink()
        subprocess.run(["git", "add", "-u"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "remove generated artifact"],
            cwd=self.root,
            check=True,
        )

        self.assertIn(
            "build/release-evidence/result.json",
            git_historical_files(self.root),
        )
        self.assertEqual(
            ("build/release-evidence/result.json",),
            historical_release_artifacts(self.root),
        )

    def test_release_skip_guards_allow_unrelated_pre_image_success(self) -> None:
        self.assertEqual([], validate_release_skip_guards(valid_release_skip_guards()))

    def test_release_skip_guard_rejects_zero_exit(self) -> None:
        source = valid_release_skip_guards().replace(
            'echo "AURIS_SKIP_REAL_STACK_E2E=1 is not allowed" >&2\n  exit 2',
            'echo "AURIS_SKIP_REAL_STACK_E2E=1 is not allowed" >&2\n  exit 0',
        )

        self.assertIn(
            "scripts/verify_release.sh must reject AURIS_SKIP_REAL_STACK_E2E "
            "with an unconditional exit 2",
            validate_release_skip_guards(source),
        )

    def test_release_skip_guard_rejects_nested_unreachable_exit(self) -> None:
        source = valid_release_skip_guards().replace(
            '  echo "AURIS_SKIP_REAL_STACK_E2E=1 is not allowed" >&2\n  exit 2',
            "  if false; then\n    exit 2\n  fi",
        )

        self.assertIn(
            "scripts/verify_release.sh must reject AURIS_SKIP_REAL_STACK_E2E "
            "with an unconditional exit 2",
            validate_release_skip_guards(source),
        )

    def test_release_skip_guards_reject_unreachable_outer_contexts(self) -> None:
        canonical = valid_release_skip_guards()
        first_guard = 'if [ "${AURIS_SKIP_REAL_STACK_E2E:-0}" = "1" ]; then'
        last_guard_end = (
            'if [ "${AURIS_SKIP_BACKUP_RESTORE_GATE:-0}" = "1" ]; then\n'
            '  echo "AURIS_SKIP_BACKUP_RESTORE_GATE=1 is not allowed" >&2\n'
            "  exit 2\n"
            "fi"
        )
        guard_start = canonical.index(first_guard)
        guard_end = canonical.index(last_guard_end) + len(last_guard_end)
        guard_section = canonical[guard_start:guard_end]
        for wrapper in (
            f"if false; then\n{guard_section}\nfi",
            f"cat <<'GUARDS'\n{guard_section}\nGUARDS",
            f"release_guards() {{\n{guard_section}\n}}",
        ):
            with self.subTest(wrapper=wrapper.splitlines()[0]):
                source = canonical[:guard_start] + wrapper + canonical[guard_end:]
                self.assertTrue(validate_release_skip_guards(source))

    def test_release_skip_guard_is_mandatory(self) -> None:
        source = valid_release_skip_guards().replace(
            """\
if [ "${AURIS_SKIP_REAL_DAGSTER:-0}" = "1" ]; then
  echo "AURIS_SKIP_REAL_DAGSTER=1 is not allowed" >&2
  exit 2
fi
""",
            "",
        )

        self.assertIn(
            "scripts/verify_release.sh is missing the AURIS_SKIP_REAL_DAGSTER fail-closed guard",
            validate_release_skip_guards(source),
        )


if __name__ == "__main__":
    unittest.main()
