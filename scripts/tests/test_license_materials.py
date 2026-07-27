from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_platform_readiness import (
    EXPECTED_NOTICE,
    validate_apache_2_license,
    validate_license_materials,
)


ROOT = Path(__file__).resolve().parents[2]


class LicenseMaterialsTests(unittest.TestCase):
    def _write_valid_materials(self, root: Path) -> None:
        (root / "LICENSE").write_bytes((ROOT / "LICENSE").read_bytes())
        (root / "NOTICE").write_text(EXPECTED_NOTICE, encoding="utf-8")
        (root / "THIRD_PARTY_NOTICES.md").write_text(
            """# Third-Party Notices

## Runtime and build dependencies

The locked inventory uses exact-artifact conclusions.

## Public datasets

Datasets are downloaded on demand.
""",
            encoding="utf-8",
        )

    def test_repository_license_keeps_standard_apache_text(self) -> None:
        self.assertEqual([], validate_apache_2_license(ROOT))

    def test_canonical_minimal_materials_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_valid_materials(root)
            self.assertEqual([], validate_license_materials(root))

    def test_notice_must_be_exact_and_identity_free(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_valid_materials(root)
            (root / "NOTICE").write_text(
                EXPECTED_NOTICE + "\nCopyright 2026 Placeholder\n",
                encoding="utf-8",
            )
            self.assertIn(
                "NOTICE must use the concise canonical project notice",
                validate_license_materials(root),
            )

    def test_missing_third_party_inventory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_valid_materials(root)
            (root / "THIRD_PARTY_NOTICES.md").unlink()
            self.assertIn(
                "missing THIRD_PARTY_NOTICES.md",
                validate_license_materials(root),
            )


if __name__ == "__main__":
    unittest.main()
