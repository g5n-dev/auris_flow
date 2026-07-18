from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.core.secrets import is_production_environment, load_secret_file

EmbeddingPurpose = Literal["document", "query"]
EmbeddingTransport = Callable[[Request, float], object]


class EmbeddingConfigurationError(ValueError):
    """A sanitized embedding provider configuration error."""


class EmbeddingResponseError(RuntimeError):
    """A sanitized embedding provider response error."""


class EmbeddingProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    @property
    def is_semantic(self) -> bool: ...

    def embed(self, text: str, *, purpose: EmbeddingPurpose) -> list[float]: ...


def _urlopen_transport(request: Request, timeout: float) -> object:
    return urlopen(request, timeout=timeout)


@dataclass(frozen=True)
class DeterministicTestEmbeddingProvider:
    """Stable test vectors. This provider is forbidden in production environments."""

    dimension: int = 8
    provider_name: str = "deterministic_test"
    model_name: str = "sha256-test-vector"
    is_semantic: bool = False

    def __post_init__(self) -> None:
        _validate_dimension(self.dimension)

    def embed(self, text: str, *, purpose: EmbeddingPurpose) -> list[float]:
        normalized = _validate_text(text)
        digest = hashlib.sha256(f"{purpose}\0{normalized}".encode()).digest()
        return [
            round((digest[index % len(digest)] / 255.0) * 2 - 1, 6)
            for index in range(self.dimension)
        ]


@dataclass(frozen=True)
class HTTPEmbeddingProvider:
    endpoint: str
    model: str
    dimension: int
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 10.0
    transport: EmbeddingTransport = field(
        default=_urlopen_transport,
        repr=False,
        compare=False,
    )
    provider_name: str = "http"
    is_semantic: bool = True

    def __post_init__(self) -> None:
        _validate_http_endpoint(self.endpoint)
        if not self.model.strip() or len(self.model) > 256:
            raise EmbeddingConfigurationError("EMBEDDING_MODEL is invalid")
        _validate_dimension(self.dimension)
        if not 0 < self.timeout_seconds <= 60:
            raise EmbeddingConfigurationError(
                "EMBEDDING_HTTP_TIMEOUT_SECONDS must be greater than 0 and at most 60"
            )

    @property
    def model_name(self) -> str:
        return self.model

    def embed(self, text: str, *, purpose: EmbeddingPurpose) -> list[float]:
        normalized = _validate_text(text)
        body = json.dumps(
            {
                "input": [normalized],
                "model": self.model,
                "input_type": purpose,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(self.endpoint, data=body, method="POST", headers=headers)
        try:
            response = self.transport(request, self.timeout_seconds)
            with cast(HTTPResponse, response) as opened:
                raw = opened.read()
        except HTTPError as exc:
            raise EmbeddingResponseError(f"embedding provider returned HTTP {exc.code}") from None
        except (OSError, URLError, TimeoutError, ValueError, TypeError):
            raise EmbeddingResponseError("embedding provider request failed") from None
        if len(raw) > 16 * 1024 * 1024:
            raise EmbeddingResponseError("embedding provider response is too large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EmbeddingResponseError("embedding provider returned invalid JSON") from None
        return _extract_vector(payload, dimension=self.dimension)


class HTTPResponse(Protocol):
    def __enter__(self) -> HTTPResponse: ...

    def __exit__(self, *args: object) -> object: ...

    def read(self) -> bytes: ...


def _validate_dimension(value: int) -> None:
    if isinstance(value, bool) or not 1 <= value <= 65536:
        raise EmbeddingConfigurationError("EMBEDDING_DIMENSION must be between 1 and 65536")


def _validate_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EmbeddingResponseError("embedding input must be non-empty text")
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > 2 * 1024 * 1024:
        raise EmbeddingResponseError("embedding input exceeds the maximum size")
    return normalized


def _validate_http_endpoint(value: str, *, production: bool = False) -> None:
    try:
        parsed = urlparse(value.strip())
        _port = parsed.port
    except ValueError:
        raise EmbeddingConfigurationError("EMBEDDING_ENDPOINT is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
    ):
        raise EmbeddingConfigurationError("EMBEDDING_ENDPOINT is invalid")
    if production and parsed.scheme != "https":
        raise EmbeddingConfigurationError("EMBEDDING_ENDPOINT must use HTTPS in prod/release")


def _extract_vector(payload: object, *, dimension: int) -> list[float]:
    raw_vector: object = None
    if isinstance(payload, dict):
        data = payload.get("data")
        embeddings = payload.get("embeddings")
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            raw_vector = data[0].get("embedding")
        elif isinstance(embeddings, list) and len(embeddings) == 1:
            raw_vector = embeddings[0]
    if not isinstance(raw_vector, list) or len(raw_vector) != dimension:
        raise EmbeddingResponseError("embedding provider returned an invalid vector dimension")
    vector: list[float] = []
    for item in raw_vector:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise EmbeddingResponseError("embedding provider returned non-numeric vector data")
        value = float(item)
        if not math.isfinite(value):
            raise EmbeddingResponseError("embedding provider returned non-finite vector data")
        vector.append(value)
    return vector


def _environment_secret(name: str, *, production: bool) -> str:
    inline = os.environ.get(name)
    file_reference = os.environ.get(f"{name}_FILE")
    if inline is not None and file_reference is not None:
        raise EmbeddingConfigurationError(f"{name} cannot be set together with {name}_FILE")
    if file_reference is not None:
        try:
            return load_secret_file(
                file_reference,
                setting_name=name.lower(),
                production=production,
            )
        except ValueError:
            raise EmbeddingConfigurationError(f"{name}_FILE could not be loaded safely") from None
    return inline or ""


def build_embedding_provider(*, dimension: int | None = None) -> EmbeddingProvider:
    app_env = os.environ.get("APP_ENV", "local")
    production = is_production_environment(app_env)
    provider_name = os.environ.get("AURIS_EMBEDDING_PROVIDER", "deterministic_test").strip()
    if dimension is None:
        try:
            resolved_dimension = int(os.environ.get("EMBEDDING_DIMENSION", "8"))
        except ValueError:
            raise EmbeddingConfigurationError("EMBEDDING_DIMENSION must be an integer") from None
    else:
        resolved_dimension = dimension
    _validate_dimension(resolved_dimension)
    if provider_name == "deterministic_test":
        if production:
            raise EmbeddingConfigurationError(
                "deterministic_test embedding provider is forbidden in prod/release"
            )
        return DeterministicTestEmbeddingProvider(dimension=resolved_dimension)
    if provider_name != "http":
        raise EmbeddingConfigurationError("AURIS_EMBEDDING_PROVIDER must be http in production")
    endpoint = os.environ.get("EMBEDDING_ENDPOINT", "").strip()
    model = os.environ.get("EMBEDDING_MODEL", "").strip()
    if not endpoint:
        raise EmbeddingConfigurationError("EMBEDDING_ENDPOINT is required")
    if not model:
        raise EmbeddingConfigurationError("EMBEDDING_MODEL is required")
    _validate_http_endpoint(endpoint, production=production)
    try:
        timeout_seconds = float(os.environ.get("EMBEDDING_HTTP_TIMEOUT_SECONDS", "10"))
    except ValueError:
        raise EmbeddingConfigurationError(
            "EMBEDDING_HTTP_TIMEOUT_SECONDS must be numeric"
        ) from None
    return HTTPEmbeddingProvider(
        endpoint=endpoint,
        model=model,
        dimension=resolved_dimension,
        api_key=_environment_secret("EMBEDDING_API_KEY", production=production),
        timeout_seconds=timeout_seconds,
    )
