from __future__ import annotations

import re
import unicodedata

_UNSAFE_FILENAME_RUN = re.compile(r"[^A-Za-z0-9._-]+")


def safe_content_disposition_filename(
    value: object,
    *,
    fallback: str = "download.bin",
    max_length: int = 128,
) -> str:
    """Return an ASCII filename that is safe inside a quoted HTTP header.

    Object metadata and business identifiers are not trusted header values.  A
    deliberately small alphabet prevents CR/LF, quotes, path separators,
    Unicode control characters and ambiguous path syntax from reaching a
    ``Content-Disposition`` field.
    """

    if max_length <= 0:
        raise ValueError("filename length bound must be positive")

    def normalize(candidate: object) -> str:
        raw = unicodedata.normalize("NFKC", candidate if isinstance(candidate, str) else "")
        safe = _UNSAFE_FILENAME_RUN.sub("-", raw).strip(".-")
        safe = safe[:max_length].rstrip(".-")
        return safe if safe not in {"", ".", ".."} else ""

    return normalize(value) or normalize(fallback) or "download"


def content_disposition_header(
    disposition: str,
    filename: object,
    *,
    fallback: str = "download.bin",
    max_length: int = 128,
) -> str:
    if disposition not in {"attachment", "inline"}:
        raise ValueError("unsupported content disposition")
    safe_filename = safe_content_disposition_filename(
        filename,
        fallback=fallback,
        max_length=max_length,
    )
    return f'{disposition}; filename="{safe_filename}"'
