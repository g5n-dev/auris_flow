#!/usr/bin/env python3
"""Run one command with a portable wall-clock deadline and bounded termination."""

from __future__ import annotations

import argparse
import math
import os
import signal
import subprocess
import sys
from collections.abc import Sequence

TIMEOUT_EXIT_CODE = 124
TERMINATION_GRACE_SECONDS = 1.0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", required=True, type=float)
    parser.add_argument("--label", default="command")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be a finite positive number")
    if not args.command:
        parser.error("a command is required after --")
    return args


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def run_with_deadline(args: argparse.Namespace) -> int:
    try:
        process = subprocess.Popen(args.command, start_new_session=True)
    except OSError as exc:
        print(f"{args.label} could not start: {exc}", file=sys.stderr)
        return 127
    try:
        return_code = process.wait(timeout=args.timeout_seconds)
    except subprocess.TimeoutExpired:
        print(
            f"{args.label} exceeded {args.timeout_seconds:g}s deadline",
            file=sys.stderr,
        )
        terminate_process_group(process)
        return TIMEOUT_EXIT_CODE
    except KeyboardInterrupt:
        terminate_process_group(process)
        return 130
    return return_code if return_code >= 0 else 128 - return_code


def main(argv: Sequence[str] | None = None) -> int:
    return run_with_deadline(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
