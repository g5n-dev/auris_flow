from __future__ import annotations

import re
import uuid

import pytest

from app.core import request_identifiers
from app.core.redaction import PHONE_PATTERN
from app.services import hotword_service
from app.services.public_run_projection_service import public_run_projection


def test_public_id_hex_encoding_preserves_entropy_without_decimal_runs() -> None:
    encoded = request_identifiers.public_id_from_hex(
        "task_run",
        "0123456789abcdef",
        suffix_length=16,
    )

    assert encoded == "task_run_abcdefghijklmnop"
    assert PHONE_PATTERN.search(encoded) is None
    assert request_identifiers.is_safe_request_identifier(encoded)


@pytest.mark.parametrize(
    ("prefix", "hex_value", "suffix_length", "separator"),
    [
        ("", "a" * 32, 12, "_"),
        ("run", "A" * 32, 12, "_"),
        ("run", "a" * 8, 12, "_"),
        ("run", "a" * 32, True, "_"),
        ("run", "a" * 32, 12, "/"),
        ("r" * 128, "a" * 32, 12, "_"),
    ],
)
def test_public_id_hex_encoding_rejects_invalid_contracts(
    prefix: str,
    hex_value: str,
    suffix_length: int,
    separator: str,
) -> None:
    with pytest.raises(ValueError):
        request_identifiers.public_id_from_hex(
            prefix,
            hex_value,
            suffix_length=suffix_length,
            separator=separator,
        )


def test_generated_hotword_ids_cannot_be_mistaken_for_phone_numbers(
    monkeypatch,
) -> None:
    phone_like_uuid = uuid.UUID(hex="671967a4f19446488515000000000000")
    monkeypatch.setattr(request_identifiers.uuid, "uuid4", lambda: phone_like_uuid)

    run_id = hotword_service._new_id("hwbuild")

    assert re.fullmatch(r"hwbuild_[a-p]{20}", run_id)
    assert PHONE_PATTERN.search(run_id) is None
    assert public_run_projection({"run_id": run_id})["run_id"] == run_id


def test_server_generated_public_ids_remain_unique_for_phone_like_uuid_values(
    monkeypatch,
) -> None:
    raw_values = iter(
        uuid.UUID(hex=f"{13800138000 + index:011d}a{index:020x}") for index in range(3)
    )
    monkeypatch.setattr(request_identifiers.uuid, "uuid4", lambda: next(raw_values))

    first = request_identifiers.server_generated_public_id("task_run", suffix_length=12)
    second = request_identifiers.server_generated_public_id("task_run", suffix_length=12)
    request_id = request_identifiers.sanitized_request_id(None)

    assert first != second
    assert re.fullmatch(r"task_run_[a-p]{12}", first)
    assert re.fullmatch(r"task_run_[a-p]{12}", second)
    assert re.fullmatch(r"request_[a-p]{32}", request_id)
    assert all(PHONE_PATTERN.search(value) is None for value in (first, second, request_id))
