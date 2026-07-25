#!/usr/bin/env python3
from __future__ import annotations

import sys

from check_platform_readiness import validate_license_materials


def main() -> int:
    failures = validate_license_materials()
    if failures:
        print("License materials verification failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("License materials ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
