#!/usr/bin/env python3
"""Offline structural validation for an Auris Flow MySQL logical dump."""

from __future__ import annotations

import argparse
import gzip
import re
import sys
from pathlib import Path


class DumpError(ValueError):
    pass


def verify(args: argparse.Namespace) -> int:
    path = Path(args.input)
    if path.is_symlink() or not path.is_file():
        raise DumpError("MySQL dump must be a regular gzip file")
    required_databases = {"auris_flow", "keycloak", "dagster"}
    created: set[str] = set()
    used: set[str] = set()
    saw_dump_header = False
    create_pattern = re.compile(r"^CREATE DATABASE .*`([^`]+)`")
    use_pattern = re.compile(r"^USE `([^`]+)`;")
    try:
        with gzip.open(path, mode="rt", encoding="utf-8", errors="strict") as handle:
            for line in handle:
                if line.startswith("-- MySQL dump"):
                    saw_dump_header = True
                if match := create_pattern.match(line):
                    created.add(match.group(1))
                if match := use_pattern.match(line):
                    used.add(match.group(1))
    except (OSError, UnicodeDecodeError) as exc:
        raise DumpError("MySQL gzip stream is corrupt or not UTF-8 SQL") from exc
    if not saw_dump_header:
        raise DumpError("MySQL dump header is missing")
    if not required_databases.issubset(created) or not required_databases.issubset(
        used
    ):
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
