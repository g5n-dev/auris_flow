from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_platform_readiness import (  # noqa: E402
    git_historical_files,
    git_staged_files,
    git_unstaged_files,
    historical_release_artifacts,
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


if __name__ == "__main__":
    unittest.main()
