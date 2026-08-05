from __future__ import annotations

import hashlib
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding

from production.tests.audio_import_platform import (
    INFERENCE_HOSTNAME,
    PLATFORM_HOSTNAME,
    browser_fixture_records,
    browser_fixture_wav_bytes,
    fixture_records,
    fixture_wav_bytes,
    initialize_pki,
)
from scripts.verify_audio_import_stack import (
    _order_by_expected_external_identity,
)

ROOT = Path(__file__).resolve().parents[2]
GATE_DRIVER = ROOT / "scripts" / "verify_audio_import_stack.sh"
BROWSER_GATE_DRIVER = ROOT / "scripts" / "verify_audio_import_browser_e2e.sh"


def test_audio_fixture_is_deterministic_valid_and_identity_stable() -> None:
    import io
    import wave

    records = fixture_records()
    assert [record["recording_id"] for record in records] == [
        "audio-import-gate-001",
        "audio-import-gate-002",
        "audio-import-gate-003",
    ]
    assert [record["updated_at"] for record in records] == sorted(
        record["updated_at"] for record in records
    )
    assert fixture_records() == records
    first_updated_at = datetime.fromisoformat(str(records[0]["updated_at"]))
    assert (
        datetime.now(UTC) - timedelta(minutes=10)
        < first_updated_at
        <= datetime.now(UTC)
    )

    digests: set[str] = set()
    for index in range(1, 4):
        first = fixture_wav_bytes(index)
        second = fixture_wav_bytes(index)
        assert first == second
        assert first.startswith(b"RIFF")
        digest = hashlib.sha256(first).hexdigest()
        assert digest not in digests
        digests.add(digest)
        with wave.open(io.BytesIO(first), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            assert wav.getframerate() == 8_000
            assert wav.getnframes() == 800 + index * 80

    with pytest.raises(ValueError):
        fixture_wav_bytes(0)
    with pytest.raises(ValueError):
        fixture_wav_bytes(4)

    browser_records = browser_fixture_records()
    assert [record["recording_id"] for record in browser_records] == [
        "audio-import-browser-001",
        "audio-import-browser-002",
        "audio-import-browser-003",
    ]
    assert {record["recording_id"] for record in records}.isdisjoint(
        record["recording_id"] for record in browser_records
    )
    assert all(
        browser_fixture_wav_bytes(index) != fixture_wav_bytes(index)
        for index in range(1, 4)
    )


def test_real_stack_identity_proof_is_order_independent_but_exact() -> None:
    newest_first = [
        {"external_record_id": "audio-import-gate-003"},
        {"external_record_id": "audio-import-gate-002"},
        {"external_record_id": "audio-import-gate-001"},
    ]
    ordered = _order_by_expected_external_identity(newest_first)
    assert ordered is not None
    assert [item["external_record_id"] for item in ordered] == [
        "audio-import-gate-001",
        "audio-import-gate-002",
        "audio-import-gate-003",
    ]
    assert _order_by_expected_external_identity(newest_first[:2]) is None
    assert (
        _order_by_expected_external_identity(
            [
                {"external_record_id": "audio-import-gate-001"},
                {"external_record_id": "audio-import-gate-001"},
                {"external_record_id": "audio-import-gate-003"},
            ]
        )
        is None
    )


def test_ephemeral_platform_certificate_is_hostname_bound(tmp_path: Path) -> None:
    ca_dir = tmp_path / "ca"
    tls_dir = tmp_path / "tls"
    initialize_pki(ca_dir, tls_dir)

    ca = x509.load_pem_x509_certificate((ca_dir / "ca.pem").read_bytes())
    server = x509.load_pem_x509_certificate((tls_dir / "server.pem").read_bytes())
    names = server.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName)
    assert names == [PLATFORM_HOSTNAME, INFERENCE_HOSTNAME]
    assert server.issuer == ca.subject
    ca.public_key().verify(
        server.signature,
        server.tbs_certificate_bytes,
        padding.PKCS1v15(),
        server.signature_hash_algorithm,
    )
    assert not (ca_dir / "server-key.pem").exists()
    assert stat.S_IMODE((tls_dir / "server-key.pem").stat().st_mode) == 0o444


def test_real_stack_gate_cannot_be_skipped() -> None:
    result = subprocess.run(
        ["bash", str(GATE_DRIVER)],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "AURIS_SKIP_AUDIO_IMPORT_REAL_STACK_GATE": "1"},
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "is not allowed by this gate" in result.stderr


def test_browser_gate_builds_derived_assets_before_vite_launch() -> None:
    source = BROWSER_GATE_DRIVER.read_text(encoding="utf-8")
    assets_build = 'npm --prefix "${UI_ROOT}" run assets:build'

    assert source.count(assets_build) == 1
    assert source.index(assets_build) < source.index("exec env \\\n")


def test_browser_gate_pins_vite_development_runtime() -> None:
    source = BROWSER_GATE_DRIVER.read_text(encoding="utf-8")
    vite_launch = source[
        source.index("exec env \\\n") : source.index(') >"${VITE_LOG}"')
    ]

    assert "NODE_ENV=development" in vite_launch
    assert "VITE_DEMO_MODE=false" in vite_launch
