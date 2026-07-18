#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]

DIRECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:ENCRYPTED |RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        ),
    ),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,}\b")),
    ("github_fine_grained_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    (
        "personal_absolute_path",
        re.compile(r"(?:/" r"Users/|[A-Za-z]:\\\\Users\\\\)[^/\\\\\s]+[/\\\\]"),
    ),
)

SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)^[ \t]*(?:export[ \t]+)?"
    r"(?P<key>[A-Za-z0-9_-]*(?:secret|token|password|api[_-]?key|"
    r"access[_-]?key|private[_-]?key)[A-Za-z0-9_-]*)"
    r"[ \t]*[:=][ \t]*(?P<quote>['\"]?)(?P<value>[^\s'\"#;]+)(?P=quote)"
    r"[ \t]*;?[ \t]*(?:#.*)?$"
)

CREDENTIAL_URL_PATTERN = re.compile(
    r"(?i)\b(?:mysql(?:\+pymysql)?|postgres(?:ql)?|redis|amqp|mongodb(?:\+srv)?)"
    r"://[^\s'\"]+"
)

ALLOWLIST_MARKER = "pragma: allowlist secret"

PLACEHOLDER_PREFIXES = (
    "auris-demo-",
    "auris-dev-",
    "canary",
    "changeme",
    "demo",
    "dev-",
    "dummy",
    "example",
    "fake",
    "local-",
    "replace-with",
    "test-",
    "your-",
)

BINARY_SUFFIXES = {
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".sqlite",
    ".wav",
    ".webp",
}

SKIP_PREFIXES = (
    ".git/",
    ".next/",
    "prototype/auris-flow-ui/node_modules/",
    "prototype/auris-flow-ui/dist/",
)


def tracked_files() -> list[Path]:
    try:
        output = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return [path for path in ROOT.rglob("*") if path.is_file()]
    return [ROOT / line for line in output.splitlines() if line]


def scan_index(findings: list[str]) -> None:
    try:
        output = subprocess.check_output(
            ["git", "ls-files", "--cached", "-z"],
            cwd=ROOT,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return
    for raw_rel in output.split(b"\0"):
        if not raw_rel:
            continue
        rel = raw_rel.decode("utf-8", errors="surrogateescape")
        if should_skip_relative(rel):
            continue
        try:
            content = subprocess.check_output(
                ["git", "show", f":{rel}"],
                cwd=ROOT,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            continue
        scan_text(
            content.decode("utf-8", errors="ignore"),
            f"index:{rel}",
            findings,
        )


def should_skip_relative(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in SKIP_PREFIXES) or (
        Path(rel).suffix.lower() in BINARY_SUFFIXES
    )


def _line_is_allowlisted(text: str, position: int) -> bool:
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    if end == -1:
        end = len(text)
    return ALLOWLIST_MARKER in text[start:end].lower()


def _is_placeholder_secret(value: str) -> bool:
    normalized = value.strip().strip("'\"").lower()
    if not normalized or normalized.startswith(("${", "$", "<")):
        return True
    if normalized in {"minioadmin", "password", "secret", "token"}:
        return True
    return normalized.startswith(PLACEHOLDER_PREFIXES)


def _credential_url_contains_secret(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    if not parsed.password or _is_placeholder_secret(parsed.password):
        return False
    if parsed.hostname in {"127.0.0.1", "localhost", "mysql", "redis"}:
        return False
    return len(parsed.password) >= 8


def _looks_like_unquoted_secret(value: str) -> bool:
    lowered = value.lower()
    if ("." in value and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", value)) or any(
        char in value for char in "()[]{},"
    ):
        return False
    if lowered.startswith(
        (
            "body.",
            "config.",
            "env.",
            "os.",
            "payload.",
            "request.",
            "self.",
            "settings.",
            "_",
        )
    ):
        return False
    character_classes = sum(
        (
            any(char.islower() for char in value),
            any(char.isupper() for char in value),
            any(char.isdigit() for char in value),
            any(not char.isalnum() for char in value),
        )
    )
    return character_classes >= 3 or (len(value) >= 32 and len(set(value)) >= 8)


def _is_deterministic_test_fixture(origin: str, value: str) -> bool:
    normalized_origin = origin.removeprefix("index:")
    if (
        "/tests/" not in f"/{normalized_origin}"
        and "/e2e/" not in f"/{normalized_origin}"
    ):
        return False
    lowered = value.lower()
    if not re.fullmatch(r"[a-z0-9-]+", lowered):
        return False
    return any(
        marker in lowered
        for marker in ("canary", "contract", "fixture", "smoke", "test", "unit")
    )


def scan_text(text: str, origin: str, findings: list[str]) -> None:
    for name, pattern in DIRECT_PATTERNS:
        for match in pattern.finditer(text):
            if _line_is_allowlisted(text, match.start()):
                continue
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{origin}:{line}: potential {name}")

    for match in SECRET_ASSIGNMENT_PATTERN.finditer(text):
        if _line_is_allowlisted(text, match.start()):
            continue
        value = match.group("value")
        if (
            len(value) < 16
            or _is_placeholder_secret(value)
            or _is_deterministic_test_fixture(origin, value)
        ):
            continue
        if not match.group("quote") and not _looks_like_unquoted_secret(value):
            continue
        line = text.count("\n", 0, match.start()) + 1
        findings.append(f"{origin}:{line}: potential generic_secret_assignment")

    for match in CREDENTIAL_URL_PATTERN.finditer(text):
        if _line_is_allowlisted(text, match.start()):
            continue
        if not _credential_url_contains_secret(match.group(0)):
            continue
        line = text.count("\n", 0, match.start()) + 1
        findings.append(f"{origin}:{line}: potential credential_url")


def scan_history(findings: list[str]) -> None:
    try:
        output = subprocess.check_output(
            ["git", "rev-list", "--objects", "--all"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        findings.append("history: unable to enumerate Git objects")
        return

    objects: dict[str, str] = {}
    for line in output.splitlines():
        oid, separator, rel = line.partition(" ")
        if not separator or not rel or should_skip_relative(rel):
            continue
        objects.setdefault(oid, rel)

    process = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        for oid, rel in objects.items():
            process.stdin.write(f"{oid}\n".encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline().decode("ascii", errors="replace").strip()
            parts = header.split()
            if len(parts) != 3:
                continue
            size = int(parts[2])
            content = process.stdout.read(size)
            process.stdout.read(1)
            if parts[1] != "blob":
                continue
            scan_text(
                content.decode("utf-8", errors="ignore"),
                f"history:{oid[:12]}:{rel}",
                findings,
            )
    finally:
        process.stdin.close()
        process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan release files for credential material"
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="also scan all reachable Git blobs before a first public push",
    )
    parser.add_argument(
        "--scope",
        choices=("all", "index", "worktree"),
        default="all",
        help="scan the index, working tree, or both (default: both)",
    )
    args = parser.parse_args()
    findings: list[str] = []
    if args.scope in {"all", "index"}:
        scan_index(findings)
    if args.scope in {"all", "worktree"}:
        for path in tracked_files():
            if not path.exists():
                continue
            rel = path.relative_to(ROOT).as_posix()
            if should_skip_relative(rel):
                continue
            try:
                text = path.read_bytes().decode("utf-8", errors="ignore")
            except OSError:
                continue
            scan_text(text, rel, findings)

    if args.history:
        scan_history(findings)

    findings = list(dict.fromkeys(findings))
    if findings:
        print("Potential secrets found:", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    print("secret scan ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
