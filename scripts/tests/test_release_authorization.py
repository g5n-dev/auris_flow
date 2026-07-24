from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_platform_readiness import (  # noqa: E402
    validate_apache_2_license,
    validate_release_authorization,
)


ROOT = Path(__file__).resolve().parents[2]
VALID_EVIDENCE_REFERENCE = (
    "urn:auris-flow:rights-approval:7d529665-1bd4-4b50-b65d-0b37116958c8"
)
VALID_EVIDENCE_SHA256 = (
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)


class ReleaseAuthorizationTests(unittest.TestCase):
    def test_repository_license_keeps_the_standard_apache_boilerplate(self) -> None:
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")

        self.assertIn("Copyright [yyyy] [name of copyright owner]", license_text)
        self.assertNotIn("Copyright 2026 Auris Flow contributors", license_text)
        self.assertEqual([], validate_apache_2_license(ROOT))

    def test_modified_apache_license_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            canonical = (ROOT / "LICENSE").read_text(encoding="utf-8")
            (root / "LICENSE").write_text(
                canonical.replace(
                    "Copyright [yyyy] [name of copyright owner]",
                    "Copyright 2026 Unapproved Fixture",
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                ["LICENSE must be the unmodified canonical Apache License 2.0 text"],
                validate_apache_2_license(root),
            )

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
- Approval evidence SHA-256: PROJECT OWNER TO COMPLETE
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
                f"""\
- Authorization status: APPROVED
- Rights holder legal name: Alexandra Vale
- Copyright notice: Copyright 2026 Alexandra Vale
- Authorized license: Apache-2.0
- Approval date (UTC): 2026-07-18
- Approval evidence reference: {VALID_EVIDENCE_REFERENCE}
- Approval evidence SHA-256: {VALID_EVIDENCE_SHA256}
- Final NOTICE confirmed: YES
""",
                encoding="utf-8",
            )
            (root / "NOTICE").write_text(
                "Auris Flow\n\nCopyright 2026 Alexandra Vale\n",
                encoding="utf-8",
            )

            self.assertEqual([], validate_release_authorization(root))

    def test_notice_must_match_approved_identity_and_copyright(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "open-source-rights-authorization.md").write_text(
                f"""\
- Authorization status: APPROVED
- Rights holder legal name: Alexandra Vale
- Copyright notice: Copyright 2026 Alexandra Vale
- Authorized license: Apache-2.0
- Approval date (UTC): 2026-07-18
- Approval evidence reference: {VALID_EVIDENCE_REFERENCE}
- Approval evidence SHA-256: {VALID_EVIDENCE_SHA256}
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
                "NOTICE does not contain the exact approved copyright notice line",
                failures,
            )

    def test_approval_date_must_be_a_real_non_future_utc_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "open-source-rights-authorization.md").write_text(
                f"""\
- Authorization status: APPROVED
- Rights holder legal name: Alexandra Vale
- Copyright notice: Copyright 2026 Alexandra Vale
- Authorized license: Apache-2.0
- Approval date (UTC): 2026-99-99
- Approval evidence reference: {VALID_EVIDENCE_REFERENCE}
- Approval evidence SHA-256: {VALID_EVIDENCE_SHA256}
- Final NOTICE confirmed: YES
""",
                encoding="utf-8",
            )
            (root / "NOTICE").write_text(
                "Alexandra Vale\nCopyright 2026 Alexandra Vale\n",
                encoding="utf-8",
            )

            self.assertIn(
                "rights authorization approval date is not a real UTC date",
                validate_release_authorization(root),
            )

    def test_example_identity_and_evidence_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "open-source-rights-authorization.md").write_text(
                f"""\
- Authorization status: APPROVED
- Rights holder legal name: Example Rights Holder
- Copyright notice: Copyright 2026 Example Rights Holder
- Authorized license: Apache-2.0
- Approval date (UTC): 2026-07-18
- Approval evidence reference: urn:auris-flow:rights-approval:example-record
- Approval evidence SHA-256: {VALID_EVIDENCE_SHA256}
- Final NOTICE confirmed: YES
""",
                encoding="utf-8",
            )
            (root / "NOTICE").write_text(
                "Auris Flow\n\nCopyright 2026 Example Rights Holder\n",
                encoding="utf-8",
            )

            failures = validate_release_authorization(root)

            for field_name in (
                "Rights holder legal name",
                "Copyright notice",
                "Approval evidence reference",
            ):
                self.assertIn(
                    f"rights authorization field is still a placeholder: {field_name}",
                    failures,
                )

    def test_duplicate_authorization_fields_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "open-source-rights-authorization.md").write_text(
                f"""\
- Authorization status: APPROVED
- Rights holder legal name: Alexandra Vale
- Rights holder legal name: Mallory Override
- Copyright notice: Copyright 2026 Alexandra Vale
- Authorized license: Apache-2.0
- Approval date (UTC): 2026-07-18
- Approval evidence reference: {VALID_EVIDENCE_REFERENCE}
- Approval evidence SHA-256: {VALID_EVIDENCE_SHA256}
- Final NOTICE confirmed: YES
""",
                encoding="utf-8",
            )
            (root / "NOTICE").write_text(
                "Auris Flow\n\nCopyright 2026 Alexandra Vale\n",
                encoding="utf-8",
            )

            self.assertIn(
                "rights authorization field must appear exactly once: "
                "Rights holder legal name",
                validate_release_authorization(root),
            )

    def test_approval_evidence_requires_an_opaque_reference_and_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "open-source-rights-authorization.md").write_text(
                """\
- Authorization status: APPROVED
- Rights holder legal name: Alexandra Vale
- Copyright notice: Copyright 2026 Alexandra Vale
- Authorized license: Apache-2.0
- Approval date (UTC): 2026-07-18
- Approval evidence reference: approval-record-2026-001
- Approval evidence SHA-256: sha256:not-a-digest
- Final NOTICE confirmed: YES
""",
                encoding="utf-8",
            )
            (root / "NOTICE").write_text(
                "Auris Flow\n\nCopyright 2026 Alexandra Vale\n",
                encoding="utf-8",
            )

            failures = validate_release_authorization(root)

            self.assertIn(
                "rights authorization approval evidence reference must use the "
                "urn:auris-flow:rights-approval:<opaque-id> format",
                failures,
            )
            self.assertIn(
                "rights authorization approval evidence SHA-256 is invalid",
                failures,
            )

    def test_notice_requires_the_exact_approved_copyright_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "open-source-rights-authorization.md").write_text(
                f"""\
- Authorization status: APPROVED
- Rights holder legal name: Alexandra Vale
- Copyright notice: Copyright 2026 Alexandra Vale
- Authorized license: Apache-2.0
- Approval date (UTC): 2026-07-18
- Approval evidence reference: {VALID_EVIDENCE_REFERENCE}
- Approval evidence SHA-256: {VALID_EVIDENCE_SHA256}
- Final NOTICE confirmed: YES
""",
                encoding="utf-8",
            )
            (root / "NOTICE").write_text(
                "Auris Flow\n\nNot Copyright 2026 Alexandra Vale (draft)\n",
                encoding="utf-8",
            )

            self.assertIn(
                "NOTICE does not contain the exact approved copyright notice line",
                validate_release_authorization(root),
            )


if __name__ == "__main__":
    unittest.main()
