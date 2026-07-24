#!/usr/bin/env python3
"""Offline structural validation for an Auris Flow MySQL logical dump."""

from __future__ import annotations

import argparse
import codecs
import gzip
import os
import re
import stat
import sys
from pathlib import Path
from typing import BinaryIO


class DumpError(ValueError):
    pass


DEFAULT_MAX_SQL_BYTES = 512 * 1024**3
HARD_MAX_SQL_BYTES = 4 * 1024**4
READ_CHUNK_BYTES = 64 * 1024
MAX_LINE_PREFIX_CHARS = 4096
CLIENT_COMMAND_PATTERN = re.compile(r"^(?:source|system)(?:\s|$)", re.IGNORECASE)


def _sql_byte_budget() -> int:
    raw = os.environ.get(
        "AURIS_BACKUP_MAX_MYSQL_SQL_BYTES",
        str(DEFAULT_MAX_SQL_BYTES),
    )
    if not raw.isascii() or not raw.isdecimal():
        raise DumpError("invalid MySQL SQL byte budget")
    value = int(raw)
    if value <= 0 or value > HARD_MAX_SQL_BYTES:
        raise DumpError("invalid MySQL SQL byte budget")
    return value


def _open_regular_nofollow(path: Path) -> BinaryIO:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise DumpError("this platform cannot safely reject MySQL dump symlinks")
    try:
        descriptor = os.open(path, flags | nofollow)
    except OSError as exc:
        raise DumpError("MySQL dump must be a regular gzip file") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise DumpError("MySQL dump must be a regular gzip file")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


class _SqlStructureScanner:
    def __init__(self) -> None:
        self.created: set[str] = set()
        self.used: set[str] = set()
        self.saw_dump_header = False
        self._line_prefix = ""
        self._seeking_non_space = True
        self._create_pattern = re.compile(r"^CREATE DATABASE .*`([^`]+)`")
        self._use_pattern = re.compile(r"^USE `([^`]+)`;")

    def _reject_client_command(self, *, complete: bool) -> None:
        prefix = self._line_prefix
        if prefix.startswith("\\"):
            raise DumpError("MySQL dump contains a forbidden client command")
        if CLIENT_COMMAND_PATTERN.match(prefix):
            raise DumpError("MySQL dump contains a forbidden client command")
        if complete and prefix.casefold() in {"source", "system"}:
            raise DumpError("MySQL dump contains a forbidden client command")

    def _finish_line(self) -> None:
        self._reject_client_command(complete=True)
        line = self._line_prefix
        if line.startswith("-- MySQL dump"):
            self.saw_dump_header = True
        if match := self._create_pattern.match(line):
            self.created.add(match.group(1))
        if match := self._use_pattern.match(line):
            self.used.add(match.group(1))
        self._line_prefix = ""
        self._seeking_non_space = True

    def feed(self, text: str) -> None:
        segments = text.split("\n")
        for index, segment in enumerate(segments):
            if self._seeking_non_space:
                segment = segment.lstrip(" \t\r\f\v")
                if segment:
                    self._seeking_non_space = False
            remaining = MAX_LINE_PREFIX_CHARS - len(self._line_prefix)
            if remaining > 0:
                self._line_prefix += segment[:remaining]
                self._reject_client_command(complete=False)
            if index < len(segments) - 1:
                self._finish_line()

    def finish(self) -> None:
        if self._line_prefix or not self._seeking_non_space:
            self._finish_line()


def verify(args: argparse.Namespace) -> int:
    path = Path(args.input)
    maximum = _sql_byte_budget()
    required_databases = {"auris_flow", "keycloak", "dagster"}
    scanner = _SqlStructureScanner()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    decompressed_bytes = 0
    try:
        with _open_regular_nofollow(path) as source:
            with gzip.GzipFile(fileobj=source, mode="rb") as handle:
                while chunk := handle.read(READ_CHUNK_BYTES):
                    decompressed_bytes += len(chunk)
                    if decompressed_bytes > maximum:
                        raise DumpError(
                            "MySQL dump exceeds the configured SQL byte budget"
                        )
                    scanner.feed(decoder.decode(chunk, final=False))
                scanner.feed(decoder.decode(b"", final=True))
                scanner.finish()
    except DumpError:
        raise
    except (EOFError, OSError, UnicodeDecodeError) as exc:
        raise DumpError("MySQL gzip stream is corrupt or not UTF-8 SQL") from exc
    if not scanner.saw_dump_header:
        raise DumpError("MySQL dump header is missing")
    if not required_databases.issubset(
        scanner.created
    ) or not required_databases.issubset(scanner.used):
        raise DumpError("MySQL dump does not contain all required databases")
    print("MySQL logical dump structure verified")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verify", choices=["verify"])
    parser.add_argument("--input", required=True)
    try:
        return verify(parser.parse_args())
    except (DumpError, OSError) as exc:
        print(f"MySQL dump error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
