from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_platform_readiness import validate_release_authorization  # noqa: E402


class ReleaseAuthorizationTests(unittest.TestCase):
    def test_pending_template_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "open-source-rights-authorization.md").write_text(
                """\
- Authorization status: PENDING
- Rights holder legal name: PROJECT OWNER TO COMPLETE
- Copyright notice: PROJECT OWNER TO COMPLETE
- Authorized license: Apache-2.0
- Approval date (UTC): YYYY-MM-DD
- Approval evidence reference: PROJECT OWNER TO COMPLETE
- Final NOTICE confirmed: NO
""",
                encoding="utf-8",
            )
            (root / "NOTICE").write_text(
                "Copyright holder: project-owner confirmation required.\n",
                encoding="utf-8",
            )

            failures = validate_release_authorization(root)

            self.assertIn("rights authorization status is not APPROVED", failures)
            self.assertIn(
                "NOTICE still contains an unapproved rights-holder placeholder",
                failures,
            )

    def test_approved_record_and_matching_notice_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "open-source-rights-authorization.md").write_text(
                """\
- Authorization status: APPROVED
- Rights holder legal name: Example Rights Holder
- Copyright notice: Copyright 2026 Example Rights Holder
- Authorized license: Apache-2.0
- Approval date (UTC): 2026-07-18
- Approval evidence reference: approval-record-2026-001
- Final NOTICE confirmed: YES
""",
                encoding="utf-8",
            )
            (root / "NOTICE").write_text(
                "Auris Flow\n\nCopyright 2026 Example Rights Holder\n",
                encoding="utf-8",
            )

            self.assertEqual([], validate_release_authorization(root))

    def test_notice_must_match_approved_identity_and_copyright(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "open-source-rights-authorization.md").write_text(
                """\
- Authorization status: APPROVED
- Rights holder legal name: Example Rights Holder
- Copyright notice: Copyright 2026 Example Rights Holder
- Authorized license: Apache-2.0
- Approval date (UTC): 2026-07-18
- Approval evidence reference: approval-record-2026-001
- Final NOTICE confirmed: YES
""",
                encoding="utf-8",
            )
            (root / "NOTICE").write_text("Auris Flow\n", encoding="utf-8")

            failures = validate_release_authorization(root)

            self.assertIn(
                "NOTICE does not identify the approved rights holder", failures
            )
            self.assertIn(
                "NOTICE does not contain the approved copyright notice", failures
            )

    def test_approval_date_must_be_a_real_non_future_utc_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "open-source-rights-authorization.md").write_text(
                """\
- Authorization status: APPROVED
- Rights holder legal name: Example Rights Holder
- Copyright notice: Copyright 2026 Example Rights Holder
- Authorized license: Apache-2.0
- Approval date (UTC): 2026-99-99
- Approval evidence reference: approval-record-2026-001
- Final NOTICE confirmed: YES
""",
                encoding="utf-8",
            )
            (root / "NOTICE").write_text(
                "Example Rights Holder\nCopyright 2026 Example Rights Holder\n",
                encoding="utf-8",
            )

            self.assertIn(
                "rights authorization approval date is not a real UTC date",
                validate_release_authorization(root),
            )


if __name__ == "__main__":
    unittest.main()
