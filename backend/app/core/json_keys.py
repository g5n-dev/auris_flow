from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from types import MappingProxyType

_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_SEPARATORS = re.compile(r"[-\s]+")
_NON_ASCII_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_json_key(value: str) -> str:
    """Normalize conventional snake/camel/acronym JSON field spellings."""
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = _ACRONYM_BOUNDARY.sub(r"\1_\2", normalized)
    normalized = _CAMEL_BOUNDARY.sub(r"\1_\2", normalized)
    return _SEPARATORS.sub("_", normalized).casefold()


def json_key_fingerprint(value: str) -> str:
    """Return a separator- and case-insensitive identity for governed fields."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _NON_ASCII_ALNUM.sub("", normalized)


def build_json_key_aliases(canonical_fields: Iterable[str]) -> Mapping[str, str]:
    aliases: dict[str, str] = {}
    for field in canonical_fields:
        canonical = normalize_json_key(field)
        fingerprint = json_key_fingerprint(canonical)
        existing = aliases.get(fingerprint)
        if existing is not None and existing != canonical:
            raise ValueError(
                f"canonical JSON fields share fingerprint {fingerprint!r}: "
                f"{existing!r}, {canonical!r}"
            )
        aliases[fingerprint] = canonical
    return MappingProxyType(aliases)
