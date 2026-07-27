from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "check_codeql_policy.py"
SPEC = importlib.util.spec_from_file_location("check_codeql_policy", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _sarif(score: str = "8.1") -> dict[str, object]:
    return {
        "runs": [
            {
                "tool": {
                    "driver": {
                        "rules": [
                            {
                                "id": "py/example",
                                "properties": {"security-severity": score},
                            }
                        ]
                    }
                },
                "results": [
                    {
                        "ruleId": "py/example",
                        "message": {"text": "example finding"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {
                                        "uri": "backend/app/example.py"
                                    },
                                    "region": {"startLine": 12, "startColumn": 3},
                                }
                            }
                        ],
                        "partialFingerprints": {
                            "primaryLocationLineHash": "stable-fingerprint"
                        },
                    }
                ],
            }
        ]
    }


class CodeqlPolicyTests(unittest.TestCase):
    def test_loads_high_findings_with_stable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sarif = Path(temp_dir) / "result.sarif"
            sarif.write_text(json.dumps(_sarif()), encoding="utf-8")

            findings = MODULE.load_findings([sarif])

        self.assertEqual(1, len(findings))
        self.assertEqual("py/example", findings[0].rule_id)
        self.assertEqual("backend/app/example.py", findings[0].path)
        self.assertEqual("stable-fingerprint", findings[0].fingerprint)

    def test_medium_findings_do_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sarif = Path(temp_dir) / "result.sarif"
            sarif.write_text(json.dumps(_sarif("6.9")), encoding="utf-8")
            self.assertEqual([], MODULE.load_findings([sarif]))

    def test_expired_exception_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exceptions = Path(temp_dir) / "exceptions.json"
            exceptions.write_text(
                json.dumps(
                    {
                        "schema_version": MODULE.SCHEMA_VERSION,
                        "exceptions": [
                            {
                                "rule_id": "py/example",
                                "path": "backend/app/example.py",
                                "fingerprint": "stable-fingerprint",
                                "owner": "maintainers",
                                "reason": "demonstrated false positive",
                                "expires_on": (
                                    datetime.now(UTC).date() - timedelta(days=1)
                                ).isoformat(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "expired"):
                MODULE.load_exceptions(exceptions)

    def test_path_traversal_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "escapes"):
            MODULE._normalized_path("../outside.py")


if __name__ == "__main__":
    unittest.main()
