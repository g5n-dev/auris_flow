from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener

from auris_flow_dagster.contracts import AurisRunContext
from auris_flow_dagster.http_transport import RejectRedirectHandler as _RejectRedirectHandler

SIGNATURE_VERSION = "auris-completion-v1"
SOURCE = "dagster"
MAX_SECRET_FILE_BYTES = 65_536
_SAFE_HEADER_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class CompletionCallbackError(RuntimeError):
    """Secret-free callback configuration or delivery failure."""


@dataclass(frozen=True)
class CompletionKeyBinding:
    key_id: str
    secret: str
    allowed_sources: frozenset[str]
    allowed_scopes: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class CompletionKeyring:
    bindings: Mapping[str, CompletionKeyBinding]

    @classmethod
    def from_file(cls, path: str | Path) -> CompletionKeyring:
        secret_path = Path(path)
        try:
            file_stat = secret_path.stat()
        except OSError as exc:
            raise CompletionCallbackError("completion keyring is unavailable") from exc
        if not stat.S_ISREG(file_stat.st_mode):
            raise CompletionCallbackError("completion keyring must be a regular file")
        if file_stat.st_size <= 0 or file_stat.st_size > MAX_SECRET_FILE_BYTES:
            raise CompletionCallbackError("completion keyring size is invalid")
        try:
            raw_bytes = secret_path.read_bytes()
            if b"\x00" in raw_bytes:
                raise CompletionCallbackError("completion keyring encoding is invalid")
            payload = json.loads(raw_bytes.decode("utf-8"))
        except CompletionCallbackError:
            raise
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise CompletionCallbackError("completion keyring is invalid") from exc
        return cls.from_mapping(payload)

    @classmethod
    def from_mapping(cls, payload: object) -> CompletionKeyring:
        if not isinstance(payload, Mapping) or not payload:
            raise CompletionCallbackError("completion keyring must be a non-empty object")
        parsed: dict[str, CompletionKeyBinding] = {}
        for raw_key_id, raw_binding in payload.items():
            if not isinstance(raw_key_id, str) or not raw_key_id.strip():
                raise CompletionCallbackError("completion key id is invalid")
            key_id = raw_key_id.strip()
            if (
                len(key_id) > 128
                or not _SAFE_HEADER_VALUE.fullmatch(key_id)
                or key_id in parsed
                or not isinstance(raw_binding, Mapping)
            ):
                raise CompletionCallbackError("completion key binding is invalid")
            secret_value = raw_binding.get("secret")
            sources = raw_binding.get("allowed_sources")
            scopes = raw_binding.get("allowed_scopes")
            if not isinstance(secret_value, str) or len(secret_value.strip()) < 32:
                raise CompletionCallbackError("completion key secret is invalid")
            if (
                not isinstance(sources, list)
                or not sources
                or any(
                    not isinstance(source, str) or not source.strip() or source.strip() == "*"
                    for source in sources
                )
                or len({source.strip() for source in sources}) != len(sources)
            ):
                raise CompletionCallbackError("completion key sources are invalid")
            if not isinstance(scopes, list) or not scopes:
                raise CompletionCallbackError("completion key must bind explicit scopes")
            allowed_scopes: set[tuple[str, str]] = set()
            for scope in scopes:
                if not isinstance(scope, Mapping):
                    raise CompletionCallbackError("completion key scope is invalid")
                tenant_id = scope.get("tenant_id")
                project_id = scope.get("project_id")
                if (
                    not isinstance(tenant_id, str)
                    or not tenant_id.strip()
                    or tenant_id.strip() == "*"
                    or not isinstance(project_id, str)
                    or not project_id.strip()
                    or project_id.strip() == "*"
                ):
                    raise CompletionCallbackError("completion key scope must be explicit")
                allowed_scopes.add((tenant_id.strip(), project_id.strip()))
            if len(allowed_scopes) != len(scopes):
                raise CompletionCallbackError("completion key scopes must be unique")
            parsed[key_id] = CompletionKeyBinding(
                key_id=key_id,
                secret=secret_value.strip(),
                allowed_sources=frozenset(source.strip() for source in sources),
                allowed_scopes=frozenset(allowed_scopes),
            )
        return cls(bindings=parsed)

    def select(self, scope: AurisRunContext, *, active_key_id: str | None) -> CompletionKeyBinding:
        eligible = {
            key_id: binding
            for key_id, binding in self.bindings.items()
            if SOURCE in binding.allowed_sources
            and (scope.tenant_id, scope.project_id) in binding.allowed_scopes
        }
        requested = (active_key_id or "").strip()
        if requested:
            binding = eligible.get(requested)
            if binding is None:
                raise CompletionCallbackError("active completion key is not allowed for scope")
            return binding
        if len(eligible) != 1:
            raise CompletionCallbackError("active completion key id is required during rotation")
        return next(iter(eligible.values()))


def canonical_signature_message(
    *,
    method: str,
    path: str,
    query: str,
    scope: AurisRunContext,
    idempotency_key: str,
    timestamp: str,
    nonce: str,
    key_id: str,
    body_sha256: str,
) -> str:
    return "\n".join(
        [
            SIGNATURE_VERSION,
            method.upper(),
            path,
            query,
            scope.tenant_id,
            scope.project_id,
            idempotency_key,
            timestamp,
            nonce,
            key_id,
            SOURCE,
            body_sha256,
        ]
    )


def _validated_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CompletionCallbackError("BFF callback URL is invalid")
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise CompletionCallbackError("BFF callback URL must not contain credentials or query")
    if os.environ.get("APP_ENV", "prod").strip().lower() in {"prod", "production", "release"}:
        if parsed.scheme != "http" and parsed.scheme != "https":
            raise CompletionCallbackError("BFF callback URL scheme is invalid")
        if parsed.scheme == "http" and parsed.hostname not in {"bff", "127.0.0.1", "localhost"}:
            raise CompletionCallbackError("plaintext callback is limited to the internal BFF")
    return value


class CompletionCallbackClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        keyring_path: str | Path | None = None,
        active_key_id: str | None = None,
        timeout_seconds: float = 5.0,
        max_attempts: int = 3,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        nonce_factory: Callable[[], str] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        configured_base_url = (
            base_url
            if base_url is not None
            else os.getenv("AURIS_BFF_INTERNAL_URL") or "http://bff:8000"
        )
        self.base_url = _validated_base_url(configured_base_url)
        configured_keyring_path: str | Path = (
            keyring_path
            if keyring_path is not None
            else os.getenv("AURIS_COMPLETION_RECEIPT_KEYRING_FILE")
            or "/run/secrets/completion_receipt_key_bindings"
        )
        self.keyring_path = Path(configured_keyring_path)
        self.active_key_id = active_key_id or os.environ.get(
            "AURIS_COMPLETION_RECEIPT_ACTIVE_KEY_ID"
        )
        if timeout_seconds <= 0 or max_attempts < 1 or max_attempts > 5:
            raise CompletionCallbackError("callback retry configuration is invalid")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self._opener = opener or build_opener(_RejectRedirectHandler()).open
        self._clock = clock or (lambda: datetime.now(UTC))
        self._nonce_factory = nonce_factory or (lambda: secrets.token_hex(24))
        self._sleeper = sleeper

    def post(
        self,
        scope: AurisRunContext,
        *,
        dagster_run_id: str,
        status: str,
        result_ref: Mapping[str, Any] | None = None,
        metrics: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        retryable: bool = True,
    ) -> dict[str, Any]:
        if status not in {"success", "failed"}:
            raise CompletionCallbackError("completion status is invalid")
        if (
            not dagster_run_id.strip()
            or len(dagster_run_id) > 256
            or not _SAFE_HEADER_VALUE.fullmatch(dagster_run_id)
        ):
            raise CompletionCallbackError("Dagster run id is invalid")

        completion_receipt_id = f"dagster:{dagster_run_id}"
        idempotency_key = f"dagster-completion:{dagster_run_id}"
        payload: dict[str, Any] = {
            "adapter": SOURCE,
            "source": SOURCE,
            "status": status,
            "completion_receipt_id": completion_receipt_id,
            "external_id": dagster_run_id,
            "result_ref": dict(result_ref or {}),
            "metrics": dict(metrics or {}),
            "retryable": bool(retryable),
        }
        if status == "failed":
            payload["error_code"] = error_code or "DAGSTER_WORKFLOW_FAILED"
            payload["note"] = "Dagster 领域执行失败；请按 trace_id 查询受控日志"
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        path = f"/api/v1/runs/{quote(scope.run_id, safe='')}/external-completion-receipts"
        url = f"{self.base_url}{path}"

        last_failure: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            keyring = CompletionKeyring.from_file(self.keyring_path)
            binding = keyring.select(scope, active_key_id=self.active_key_id)
            timestamp = self._clock().astimezone(UTC).isoformat()
            nonce = self._nonce_factory()
            body_sha256 = hashlib.sha256(body).hexdigest()
            message = canonical_signature_message(
                method="POST",
                path=path,
                query="",
                scope=scope,
                idempotency_key=idempotency_key,
                timestamp=timestamp,
                nonce=nonce,
                key_id=binding.key_id,
                body_sha256=body_sha256,
            )
            signature = hmac.new(
                binding.secret.encode("utf-8"),
                message.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            request = Request(  # noqa: S310 - base URL is allowlisted above
                url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotency_key,
                    "X-Tenant-Id": scope.tenant_id,
                    "X-Project-Id": scope.project_id,
                    "X-Trace-Id": scope.trace_id,
                    "X-Request-Id": f"dagster-{dagster_run_id}",
                    "X-Auris-Key-Id": binding.key_id,
                    "X-Auris-Timestamp": timestamp,
                    "X-Auris-Nonce": nonce,
                    "X-Auris-Source": SOURCE,
                    "X-Auris-Signature-Mode": "hmac-sha256",
                    "X-Auris-Signature": f"sha256={signature}",
                },
            )
            try:
                with self._opener(request, timeout=self.timeout_seconds) as response:
                    response_body = response.read(1_048_577)
                    if len(response_body) > 1_048_576:
                        raise CompletionCallbackError("BFF callback response is too large")
                    parsed = json.loads(response_body.decode("utf-8")) if response_body else {}
                if not isinstance(parsed, dict):
                    raise CompletionCallbackError("BFF callback response is invalid")
                return parsed
            except HTTPError as exc:
                last_failure = exc
                if 400 <= exc.code < 500 and exc.code not in {408, 409, 425, 429}:
                    break
            except (OSError, URLError, TimeoutError, UnicodeDecodeError, ValueError) as exc:
                last_failure = exc
            if attempt < self.max_attempts:
                self._sleeper(0.25 * (2 ** (attempt - 1)))

        status_code = last_failure.code if isinstance(last_failure, HTTPError) else None
        suffix = f" (HTTP {status_code})" if status_code is not None else ""
        raise CompletionCallbackError(f"BFF completion callback failed{suffix}") from last_failure
