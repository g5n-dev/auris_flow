#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


SCHEMA_VERSION = "auris.codeql-exceptions.v1"
HIGH_SECURITY_SCORE = 7.0


@dataclass(frozen=True)
class Finding:
    rule_id: str
    path: str
    fingerprint: str
    score: float
    message: str


def _normalized_path(uri: str) -> str:
    parsed = urlparse(uri)
    candidate = unquote(parsed.path if parsed.scheme == "file" else uri)
    candidate = candidate.replace("\\", "/").lstrip("/")
    parts = [part for part in candidate.split("/") if part not in {"", "."}]
    if ".." in parts:
        raise ValueError(f"SARIF path escapes the repository: {uri}")
    return "/".join(parts)


def _security_score(rule: dict[str, Any], result: dict[str, Any]) -> float:
    for properties in (result.get("properties", {}), rule.get("properties", {})):
        raw = properties.get("security-severity")
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"invalid CodeQL security-severity: {raw!r}") from None
    return 0.0


def _fingerprint(result: dict[str, Any], rule_id: str, path: str) -> str:
    fingerprints = result.get("partialFingerprints", {})
    for name in (
        "primaryLocationLineHash",
        "primaryLocationStartColumnFingerprint",
        "primaryLocationStartColumnFingerprint-v2",
    ):
        value = fingerprints.get(name)
        if isinstance(value, str) and value:
            return value

    location = result.get("locations", [{}])[0].get("physicalLocation", {})
    region = location.get("region", {})
    message = result.get("message", {}).get("text", "")
    canonical = json.dumps(
        {
            "rule_id": rule_id,
            "path": path,
            "start_line": region.get("startLine"),
            "start_column": region.get("startColumn"),
            "message": message,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_findings(paths: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for run in payload.get("runs", []):
            rules = {
                rule.get("id"): rule
                for rule in run.get("tool", {}).get("driver", {}).get("rules", [])
                if isinstance(rule.get("id"), str)
            }
            for result in run.get("results", []):
                rule_id = result.get("ruleId")
                if not isinstance(rule_id, str):
                    continue
                rule = rules.get(rule_id, {})
                score = _security_score(rule, result)
                if score < HIGH_SECURITY_SCORE:
                    continue
                locations = result.get("locations", [])
                if not locations:
                    raise ValueError(f"high-severity result has no location: {rule_id}")
                uri = (
                    locations[0]
                    .get("physicalLocation", {})
                    .get("artifactLocation", {})
                    .get("uri")
                )
                if not isinstance(uri, str) or not uri:
                    raise ValueError(f"high-severity result has no path: {rule_id}")
                normalized = _normalized_path(uri)
                findings.append(
                    Finding(
                        rule_id=rule_id,
                        path=normalized,
                        fingerprint=_fingerprint(result, rule_id, normalized),
                        score=score,
                        message=str(result.get("message", {}).get("text", "")),
                    )
                )
    return findings


def load_exceptions(path: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported CodeQL exception schema: {path}")
    raw_exceptions = payload.get("exceptions")
    if not isinstance(raw_exceptions, list):
        raise ValueError("CodeQL exceptions must be a list")

    today = datetime.now(UTC).date()
    exceptions: dict[tuple[str, str, str], dict[str, Any]] = {}
    for index, item in enumerate(raw_exceptions):
        if not isinstance(item, dict):
            raise ValueError(f"CodeQL exception #{index + 1} must be an object")
        required = ("rule_id", "path", "fingerprint", "owner", "reason", "expires_on")
        missing = [field for field in required if not item.get(field)]
        if missing:
            raise ValueError(
                f"CodeQL exception #{index + 1} is missing: {', '.join(missing)}"
            )
        try:
            expires_on = date.fromisoformat(str(item["expires_on"]))
        except ValueError:
            raise ValueError(
                f"CodeQL exception #{index + 1} has invalid expires_on"
            ) from None
        if expires_on < today:
            raise ValueError(f"CodeQL exception #{index + 1} expired on {expires_on}")
        key = (str(item["rule_id"]), str(item["path"]), str(item["fingerprint"]))
        if key in exceptions:
            raise ValueError(f"duplicate CodeQL exception: {key}")
        exceptions[key] = item
    return exceptions


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail on unexpired, unreviewed High/Critical CodeQL SARIF results."
    )
    parser.add_argument("sarif", nargs="+", type=Path)
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=Path("security/codeql-exceptions.json"),
    )
    args = parser.parse_args()

    sarif_paths: list[Path] = []
    for candidate in args.sarif:
        if candidate.is_dir():
            sarif_paths.extend(sorted(candidate.rglob("*.sarif")))
        elif candidate.is_file():
            sarif_paths.append(candidate)
    if not sarif_paths:
        print("CodeQL policy found no SARIF input.", file=sys.stderr)
        return 2

    try:
        exceptions = load_exceptions(args.exceptions)
        findings = load_findings(sarif_paths)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"CodeQL policy input is invalid: {error}", file=sys.stderr)
        return 2

    unresolved = [
        finding
        for finding in findings
        if (finding.rule_id, finding.path, finding.fingerprint) not in exceptions
    ]
    if unresolved:
        print("Unreviewed High/Critical CodeQL findings:", file=sys.stderr)
        for finding in unresolved:
            print(
                f"- {finding.rule_id} {finding.path} "
                f"score={finding.score:g} fingerprint={finding.fingerprint}",
                file=sys.stderr,
            )
        return 1

    print(
        f"CodeQL Policy ok: {len(findings)} High/Critical finding(s), "
        f"{len(findings)} explicitly resolved or exempted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
