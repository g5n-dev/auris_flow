from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "verify_github_actions_pins.py"
SPEC = importlib.util.spec_from_file_location("verify_github_actions_pins", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class GitHubActionsPinTests(unittest.TestCase):
    def test_repository_workflows_match_the_controlled_lock(self) -> None:
        self.assertEqual([], MODULE.validate())

    def test_floating_action_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".github/workflows").mkdir(parents=True)
            (root / "security").mkdir()
            (root / "security/github-actions-lock.json").write_text(
                json.dumps(
                    {
                        "schema_version": "auris.github-actions-lock.v1",
                        "actions": {
                            "actions/checkout": {
                                "v4.3.1": "34e114876b0b11c390a56381ad16ebd13914f8d5"
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / ".github/workflows/test.yml").write_text(
                "steps:\n  - uses: actions/checkout@v4\n",
                encoding="utf-8",
            )

            failures = MODULE.validate(root)

        self.assertTrue(any("40-character SHA" in item for item in failures))


if __name__ == "__main__":
    unittest.main()
