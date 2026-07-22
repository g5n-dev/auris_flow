#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


MAX_REQUEST_BYTES = 1_048_576
DISPATCH_IDEMPOTENCY_TAG = "auris/dispatch_idempotency_key"
DEFAULT_REPOSITORY_LOCATION_NAME = "auris_flow_defs"
DEFAULT_REPOSITORY_NAME = "__repository__"
DEFAULT_JOB_NAME = "auris_flow_generic_job"
AUDIO_INTELLIGENCE_JOB_NAME = "auris_flow_audio_intelligence_v1"
LAUNCH_QUERY_NAME = "LaunchAurisRun"
ALLOWED_PAYLOAD_KEYS = frozenset({"query", "variables", "operationName"})
OPERATION_PATTERN = re.compile(r"\A\s*(query|mutation)\s+([_A-Za-z][_0-9A-Za-z]*)\b")
ADDITIONAL_OPERATION_PATTERN = re.compile(
    r"}\s*(?:query|mutation|subscription)\s+[_A-Za-z][_0-9A-Za-z]*\b"
)
SUPPORTED_OPERATIONS = {
    "AurisReadinessWorkspace": ("query", ("instance", "repositoriesOrError")),
    "AurisRunByKey": ("query", ("runsOrError",)),
    "LaunchAurisRun": ("mutation", ("launchPipelineExecution",)),
}


def _configured_name(environment_name: str, default: str) -> str:
    return os.environ.get(environment_name, "").strip() or default


class FakeDagsterState:
    def __init__(self, receipt_log: Path | None) -> None:
        self.receipt_log = receipt_log
        self.receipts: dict[str, dict[str, Any]] = {}
        self.repository_location_name = _configured_name(
            "DAGSTER_REPOSITORY_LOCATION_NAME", DEFAULT_REPOSITORY_LOCATION_NAME
        )
        self.repository_name = _configured_name(
            "DAGSTER_REPOSITORY_NAME", DEFAULT_REPOSITORY_NAME
        )
        self.default_job_name = _configured_name(
            "DAGSTER_DEFAULT_JOB_NAME", DEFAULT_JOB_NAME
        )
        self.allowed_job_names = frozenset(
            {self.default_job_name, AUDIO_INTELLIGENCE_JOB_NAME}
        )
        self._lock = threading.Lock()

    def record(self, receipt: dict[str, Any]) -> None:
        with self._lock:
            self.receipts[receipt["run_id"]] = receipt
            if self.receipt_log:
                self.receipt_log.parent.mkdir(parents=True, exist_ok=True)
                with self.receipt_log.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n"
                    )

    def receipt_for_run_key(self, run_key: str) -> dict[str, Any] | None:
        with self._lock:
            return next(
                (
                    receipt
                    for receipt in self.receipts.values()
                    if receipt.get("run_key") == run_key
                ),
                None,
            )


class FakeDagsterHandler(BaseHTTPRequestHandler):
    server_version = "AurisFakeDagster/0.2"

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
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, code: str, message: str) -> None:
        self._send_json(
            400,
            {
                "errors": [
                    {
                        "message": message,
                        "extensions": {"code": f"FAKE_DAGSTER_{code}"},
                    }
                ]
            },
        )

    def _read_payload(self) -> tuple[dict[str, Any], bytes] | None:
        content_length = self.headers.get("Content-Length")
        try:
            length = int(content_length or "0")
        except ValueError:
            self._reject("REQUEST_INVALID", "Content-Length must be an integer")
            return None
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self.close_connection = True
            self._reject(
                "REQUEST_INVALID",
                f"request body must contain 1..{MAX_REQUEST_BYTES} bytes",
            )
            return None
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._reject("REQUEST_INVALID", "request body is truncated")
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reject("JSON_INVALID", "request body must be valid UTF-8 JSON")
            return None
        if not isinstance(payload, dict):
            self._reject("PAYLOAD_INVALID", "GraphQL payload must be an object")
            return None
        unexpected_keys = set(payload) - ALLOWED_PAYLOAD_KEYS
        if unexpected_keys:
            self._reject(
                "PAYLOAD_INVALID", "GraphQL payload contains unsupported fields"
            )
            return None
        if not isinstance(payload.get("variables"), dict):
            self._reject("VARIABLES_INVALID", "GraphQL variables must be an object")
            return None
        return payload, raw

    def _operation_name(self, payload: dict[str, Any]) -> str | None:
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            self._reject("QUERY_INVALID", "GraphQL query must be a non-empty string")
            return None
        match = OPERATION_PATTERN.match(query)
        if match is None:
            self._reject(
                "OPERATION_UNSUPPORTED",
                "GraphQL operation must be explicitly named",
            )
            return None
        operation_type, operation_name = match.groups()
        expected = SUPPORTED_OPERATIONS.get(operation_name)
        if expected is None or expected[0] != operation_type:
            self._reject("OPERATION_UNSUPPORTED", "GraphQL operation is not supported")
            return None
        if ADDITIONAL_OPERATION_PATTERN.search(query):
            self._reject(
                "OPERATION_UNSUPPORTED", "exactly one GraphQL operation is required"
            )
            return None
        if any(field_name not in query for field_name in expected[1]):
            self._reject("OPERATION_INVALID", "GraphQL operation body is not supported")
            return None
        requested_operation = payload.get("operationName")
        if requested_operation is not None and requested_operation != operation_name:
            self._reject(
                "OPERATION_MISMATCH",
                "operationName must match the declared GraphQL operation",
            )
            return None
        return operation_name

    def _validated_tags(self, value: object) -> list[dict[str, str]] | None:
        if not isinstance(value, list) or not value or len(value) > 64:
            self._reject("TAGS_INVALID", "tags must be a non-empty bounded array")
            return None
        validated: list[dict[str, str]] = []
        keys: set[str] = set()
        for tag in value:
            if not isinstance(tag, dict) or set(tag) != {"key", "value"}:
                self._reject("TAGS_INVALID", "each tag must contain only key and value")
                return None
            key = tag.get("key")
            tag_value = tag.get("value")
            if (
                not isinstance(key, str)
                or not key.strip()
                or len(key) > 128
                or not isinstance(tag_value, str)
                or not tag_value.strip()
                or len(tag_value) > 1_024
            ):
                self._reject(
                    "TAGS_INVALID", "tag keys and values must be non-empty strings"
                )
                return None
            if key in keys:
                self._reject("TAGS_INVALID", "duplicate tag keys are not allowed")
                return None
            keys.add(key)
            validated.append({"key": key, "value": tag_value})
        if DISPATCH_IDEMPOTENCY_TAG not in keys:
            self._reject(
                "TAGS_INVALID", "the exact dispatch idempotency tag is required"
            )
            return None
        return validated

    def _send_readiness(self, variables: dict[str, Any]) -> None:
        if variables:
            self._reject(
                "VARIABLES_INVALID", "readiness variables must be an empty object"
            )
            return
        self._send_json(
            200,
            {
                "data": {
                    "instance": {
                        "daemonHealth": {
                            "allDaemonStatuses": [
                                {
                                    "daemonType": "QUEUED_RUN_COORDINATOR",
                                    "required": True,
                                    "healthy": True,
                                    "lastHeartbeatTime": time.time(),
                                }
                            ]
                        }
                    },
                    "repositoriesOrError": {
                        "__typename": "RepositoryConnection",
                        "nodes": [
                            {
                                "name": self.state.repository_name,
                                "location": {
                                    "name": self.state.repository_location_name
                                },
                                "pipelines": [
                                    {"name": name}
                                    for name in sorted(self.state.allowed_job_names)
                                ],
                            }
                        ],
                    },
                }
            },
        )

    def _send_lookup(self, variables: dict[str, Any]) -> None:
        if set(variables) != {"filter"} or not isinstance(
            variables.get("filter"), dict
        ):
            self._reject("FILTER_INVALID", "lookup filter must be an object")
            return
        filter_value = variables["filter"]
        if set(filter_value) != {"tags"}:
            self._reject("FILTER_INVALID", "lookup requires an exact tag filter")
            return
        tags = self._validated_tags(filter_value.get("tags"))
        if tags is None:
            return
        run_key = next(
            tag["value"] for tag in tags if tag["key"] == DISPATCH_IDEMPOTENCY_TAG
        )
        receipt = self.state.receipt_for_run_key(run_key)
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

    def _send_launch(
        self, variables: dict[str, Any], raw: bytes, parsed_path: str, query: str
    ) -> None:
        if set(variables) != {"executionParams"} or not isinstance(
            variables.get("executionParams"), dict
        ):
            self._reject(
                "EXECUTION_PARAMS_INVALID", "executionParams must be an object"
            )
            return
        execution_params = variables["executionParams"]
        required_params = {"selector", "runConfigData", "executionMetadata"}
        if set(execution_params) != required_params:
            self._reject(
                "EXECUTION_PARAMS_INVALID",
                "executionParams must contain selector, runConfigData and executionMetadata",
            )
            return
        selector = execution_params.get("selector")
        if not isinstance(selector, dict):
            self._reject("SELECTOR_INVALID", "selector must be an object")
            return
        required_selector = {
            "repositoryLocationName",
            "repositoryName",
            "pipelineName",
        }
        if set(selector) != required_selector or any(
            not isinstance(selector.get(name), str) or not selector[name].strip()
            for name in required_selector
        ):
            self._reject(
                "SELECTOR_INVALID", "selector must contain three non-empty names"
            )
            return
        expected_selector = {
            "repositoryLocationName": self.state.repository_location_name,
            "repositoryName": self.state.repository_name,
        }
        if {
            key: selector[key] for key in expected_selector
        } != expected_selector or selector[
            "pipelineName"
        ] not in self.state.allowed_job_names:
            self._reject(
                "SELECTOR_INVALID", "selector does not match the configured workspace"
            )
            return
        if not isinstance(execution_params.get("runConfigData"), dict):
            self._reject("RUN_CONFIG_INVALID", "runConfigData must be an object")
            return
        execution_metadata = execution_params.get("executionMetadata")
        if not isinstance(execution_metadata, dict) or set(execution_metadata) != {
            "tags"
        }:
            self._reject(
                "EXECUTION_METADATA_INVALID",
                "executionMetadata must contain only tags",
            )
            return
        execution_tags = self._validated_tags(execution_metadata.get("tags"))
        if execution_tags is None:
            return
        run_key = next(
            tag["value"]
            for tag in execution_tags
            if tag["key"] == DISPATCH_IDEMPOTENCY_TAG
        )
        request_sha256 = hashlib.sha256(raw).hexdigest()
        run_id = f"fake_dagster_run_{request_sha256[:16]}"
        receipt_url = (
            f"http://{self.server.server_address[0]}:"
            f"{self.server.server_address[1]}/receipts/{run_id}"
        )
        receipt = {
            "run_id": run_id,
            "request_sha256": request_sha256,
            "method": "POST",
            "path": parsed_path,
            "query": query,
            "variables": variables,
            "run_key": run_key,
            "job_name": selector["pipelineName"],
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
        parsed_request = self._read_payload()
        if parsed_request is None:
            return
        payload, raw = parsed_request
        operation_name = self._operation_name(payload)
        if operation_name is None:
            return
        variables = payload["variables"]
        if operation_name == "AurisReadinessWorkspace":
            self._send_readiness(variables)
            return
        if operation_name == "AurisRunByKey":
            self._send_lookup(variables)
            return
        if operation_name == LAUNCH_QUERY_NAME:
            self._send_launch(variables, raw, parsed.path, payload["query"])
            return
        self._reject("OPERATION_UNSUPPORTED", "GraphQL operation is not supported")


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
