#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "security/github-actions-lock.json"
WORKFLOW_ROOT = ROOT / ".github/workflows"
USE_PATTERN = re.compile(
    r"^\s*-?\s*uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"(?:/[A-Za-z0-9_./-]+)?@([0-9a-f]{40})"
    r"(?:\s+#\s*(v[0-9][A-Za-z0-9_.-]*))?\s*$"
)
COMMENT_PATTERN = re.compile(
    r"^\s*#\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s+"
    r"(v[0-9][A-Za-z0-9_.-]*)\s*$"
)


def validate(root: Path = ROOT) -> list[str]:
    failures: list[str] = []
    try:
        payload = json.loads((root / LOCK_PATH.relative_to(ROOT)).read_text())
    except (OSError, json.JSONDecodeError) as error:
        return [f"unable to read GitHub Actions lock: {error}"]
    if payload.get("schema_version") != "auris.github-actions-lock.v1":
        return ["unsupported GitHub Actions lock schema"]
    actions = payload.get("actions")
    if not isinstance(actions, dict):
        return ["GitHub Actions lock must contain an actions object"]

    seen: set[tuple[str, str, str]] = set()
    workflow_root = root / WORKFLOW_ROOT.relative_to(ROOT)
    for workflow in sorted(workflow_root.glob("*.y*ml")):
        previous_line = ""
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "uses:" not in line:
                previous_line = line
                continue
            match = USE_PATTERN.match(line)
            if match is None:
                failures.append(
                    f"{workflow.relative_to(root)}:{line_number}: "
                    "action must use a full 40-character SHA"
                )
                previous_line = line
                continue
            action, sha, inline_version = match.groups()
            version = inline_version
            if version is None:
                comment = COMMENT_PATTERN.match(previous_line)
                if comment is not None and comment.group(1) == action:
                    version = comment.group(2)
            if version is None:
                failures.append(
                    f"{workflow.relative_to(root)}:{line_number}: "
                    f"{action} is missing an exact version comment"
                )
                previous_line = line
                continue
            expected = actions.get(action, {}).get(version)
            if expected != sha:
                failures.append(
                    f"{workflow.relative_to(root)}:{line_number}: "
                    f"{action} {version} does not match the controlled SHA"
                )
            seen.add((action, version, sha))
            previous_line = line

    locked = {
        (action, version, sha)
        for action, versions in actions.items()
        if isinstance(versions, dict)
        for version, sha in versions.items()
    }
    unused = sorted(locked - seen)
    for action, version, _sha in unused:
        failures.append(f"unused GitHub Actions lock entry: {action} {version}")
    return failures


def main() -> int:
    failures = validate()
    if failures:
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("GitHub Actions pins ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
