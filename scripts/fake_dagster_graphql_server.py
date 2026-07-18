#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class FakeDagsterState:
    def __init__(self, receipt_log: Path | None) -> None:
        self.receipt_log = receipt_log
        self.receipts: dict[str, dict[str, Any]] = {}

    def record(self, receipt: dict[str, Any]) -> None:
        self.receipts[receipt["run_id"]] = receipt
        if self.receipt_log:
            self.receipt_log.parent.mkdir(parents=True, exist_ok=True)
            with self.receipt_log.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n"
                )


class FakeDagsterHandler(BaseHTTPRequestHandler):
    server_version = "AurisFakeDagster/0.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    @property
    def state(self) -> FakeDagsterState:
        return self.server.state  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/healthz"}:
            self._send_json(200, {"status": "ok", "service": "fake-dagster-graphql"})
            return
        if parsed.path.startswith("/receipts/"):
            run_id = parsed.path.rsplit("/", 1)[-1]
            receipt = self.state.receipts.get(run_id)
            if receipt is None:
                self._send_json(404, {"error": {"code": "RECEIPT_NOT_FOUND"}})
                return
            self._send_json(200, {"data": receipt})
            return
        self._send_json(404, {"error": {"code": "NOT_FOUND"}})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/graphql":
            self._send_json(404, {"error": {"code": "NOT_FOUND"}})
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        request_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError as exc:
            self._send_json(400, {"errors": [{"message": str(exc)}]})
            return
        variables = payload.get("variables") if isinstance(payload, dict) else {}
        query = payload.get("query") if isinstance(payload, dict) else ""
        if "AurisRunByKey" in str(query):
            filter_value = (
                variables.get("filter") if isinstance(variables, dict) else {}
            )
            filter_tags = (
                filter_value.get("tags") if isinstance(filter_value, dict) else []
            )
            run_key = next(
                (
                    str(tag.get("value"))
                    for tag in filter_tags
                    if isinstance(tag, dict)
                    and tag.get("key") == "auris/dispatch_idempotency_key"
                ),
                "",
            )
            receipt = next(
                (
                    item
                    for item in self.state.receipts.values()
                    if item.get("run_key") == run_key
                ),
                None,
            )
            results = []
            if receipt:
                results.append(
                    {
                        "runId": receipt["run_id"],
                        "status": "STARTED",
                        "tags": receipt.get("tags") or [],
                    }
                )
            self._send_json(
                200,
                {
                    "data": {
                        "runsOrError": {
                            "__typename": "Runs",
                            "results": results,
                        }
                    }
                },
            )
            return
        execution_params = (
            variables.get("executionParams") if isinstance(variables, dict) else {}
        )
        execution_metadata = (
            execution_params.get("executionMetadata")
            if isinstance(execution_params, dict)
            else {}
        )
        selector = (
            execution_params.get("selector")
            if isinstance(execution_params, dict)
            else {}
        )
        execution_tags = (
            execution_metadata.get("tags")
            if isinstance(execution_metadata, dict)
            else []
        )
        run_key = next(
            (
                str(tag.get("value"))
                for tag in execution_tags
                if isinstance(tag, dict)
                and tag.get("key") == "auris/dispatch_idempotency_key"
            ),
            None,
        )
        run_id = f"fake_dagster_run_{request_sha256[:16]}"
        receipt_url = f"http://{self.server.server_address[0]}:{self.server.server_address[1]}/receipts/{run_id}"
        receipt = {
            "run_id": run_id,
            "request_sha256": request_sha256,
            "method": "POST",
            "path": parsed.path,
            "query": payload.get("query") if isinstance(payload, dict) else None,
            "variables": variables,
            "run_key": run_key,
            "job_name": selector.get("pipelineName")
            if isinstance(selector, dict)
            else None,
            "tags": execution_tags,
            "receipt_url": receipt_url,
        }
        self.state.record(receipt)
        self._send_json(
            200,
            {
                "data": {
                    "launchPipelineExecution": {
                        "__typename": "LaunchRunSuccess",
                        "run": {"runId": run_id, "status": "STARTED"},
                    }
                },
                "extensions": {
                    "auris_protocol_receipt": {
                        "receipt_url": receipt_url,
                        "request_sha256": request_sha256,
                    }
                },
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--receipt-log")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), FakeDagsterHandler)
    server.state = FakeDagsterState(
        Path(args.receipt_log) if args.receipt_log else None
    )  # type: ignore[attr-defined]
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
