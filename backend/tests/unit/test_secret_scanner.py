from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType


def _load_scanner() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts" / "scan_secrets.py"
    spec = importlib.util.spec_from_file_location("auris_secret_scanner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scanner_detects_supported_secret_formats_without_literal_fixtures() -> None:
    scanner = _load_scanner()
    private_key = "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----"
    github_token = "github" + "_pat_" + "A" * 24
    api_key = "sk" + "-" + "A" * 24
    assigned = "AUTH_TOKEN_SECRET=" + "Aa19_" * 7
    credential_url = "mysql://auris:" + "Z" * 16 + "@db.example.internal/auris"

    findings: list[str] = []
    scanner.scan_text(
        "\n".join((private_key, github_token, api_key, assigned, credential_url)),
        "fixture",
        findings,
    )

    kinds = {finding.rsplit(" ", 1)[-1] for finding in findings}
    assert {
        "private_key",
        "github_fine_grained_token",
        "openai_key",
        "generic_secret_assignment",
        "credential_url",
    } <= kinds


def test_scanner_ignores_explicit_placeholders_and_local_urls() -> None:
    scanner = _load_scanner()
    text = "\n".join(
        (
            "AUTH_TOKEN_SECRET=replace-with-32-plus-char-secret",
            "EXTERNAL_CALLBACK_SECRET=auris-dev-callback-secret",
            "DATABASE_URL=mysql://auris:auris_root@127.0.0.1:3306/auris",
            "AUTH_TOKEN_SECRET=" + "Z" * 32 + "  # pragma: allowlist secret",
        )
    )
    findings: list[str] = []

    scanner.scan_text(text, "fixture", findings)

    assert findings == []


def test_scanner_accepts_docker_secret_file_references_but_not_inline_values() -> None:
    scanner = _load_scanner()
    text = "\n".join(
        (
            "OBJECT_STORAGE_SECRET_KEY_FILE: /run/secrets/object_storage_secret_key",
            "MYSQL_ROOT_PASSWORD_FILE=/run/secrets/mysql_root_password",
            "AUTH_TOKEN_SECRET_FILE=/tmp/Qq19-untrusted-inline-secret",
            "AUTH_TOKEN_SECRET=" + "Qq19_" * 7,
        )
    )
    findings: list[str] = []

    scanner.scan_text(text, "production/compose.yaml", findings)

    assert findings == [
        "production/compose.yaml:3: potential generic_secret_assignment",
        "production/compose.yaml:4: potential generic_secret_assignment",
    ]


def test_scanner_ignores_code_expressions_and_named_test_fixtures() -> None:
    scanner = _load_scanner()
    text = "\n".join(
        (
            "owner_token = uuid.uuid4().hex",
            "processing_token = RunCompletionReceipt.processing_token",
            'token_key = "unit-signing-key-for-provider-tests-32"',
            "access_token: smokeSessionToken",
        )
    )
    findings: list[str] = []

    scanner.scan_text(text, "backend/tests/unit/test_fixture.py", findings)

    assert findings == []


def test_index_scan_reads_staged_content_missing_from_worktree(
    tmp_path: Path,
) -> None:
    scanner = _load_scanner()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    staged = tmp_path / "staged.env"
    staged.write_text("AUTH_TOKEN_SECRET=" + "Qq19_" * 7 + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.env"], cwd=tmp_path, check=True)
    staged.unlink()
    scanner.ROOT = tmp_path
    findings: list[str] = []

    scanner.scan_index(findings)

    assert findings == ["index:staged.env:1: potential generic_secret_assignment"]


def test_history_scan_is_bound_to_checked_out_release_lineage() -> None:
    scanner_source = (
        Path(__file__).resolve().parents[3] / "scripts" / "scan_secrets.py"
    ).read_text(encoding="utf-8")

    assert '["git", "rev-list", "--objects", "HEAD"]' in scanner_source
    assert '["git", "rev-list", "--objects", "--all"]' not in scanner_source
