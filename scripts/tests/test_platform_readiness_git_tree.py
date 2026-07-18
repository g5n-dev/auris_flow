from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_platform_readiness import git_staged_files, git_unstaged_files  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
