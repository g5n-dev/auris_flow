#!/usr/bin/env python3
from __future__ import annotations

import sys

from check_platform_readiness import validate_release_authorization


def main() -> int:
    failures = validate_release_authorization()
    if failures:
        print("Open-source publication authorization failed closed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("Open-source publication authorization ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
