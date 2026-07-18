from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[3]


def _load_policy() -> ModuleType:
    path = ROOT / "scripts" / "verify_production_compose.py"
    spec = importlib.util.spec_from_file_location("verify_production_compose", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rendered_production_compose_satisfies_candidate_policy() -> None:
    policy = _load_policy()
    document = policy._render_compose()

    assert policy.validate_compose(document) == []
    assert "identity-bootstrap" in document["services"]


def test_policy_rejects_latest_secret_environment_and_public_database() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    document["services"]["mysql"]["image"] = "mysql:latest"
    document["services"]["mysql"]["ports"] = [{"target": 3306, "published": "3306"}]
    document["services"]["bff"]["environment"]["QDRANT_API_KEY"] = "visible-value"
    document["services"]["worker"]["environment"]["AURIS_EMBEDDING_PROVIDER"] = "deterministic_test"
    document["services"]["worker"]["environment"]["OTEL_ENABLED"] = "false"
    document["services"]["grafana"]["ports"][0]["host_ip"] = "0.0.0.0"

    errors = policy.validate_compose(document)

    assert any("non-latest" in error for error in errors)
    assert any("must not publish host ports" in error for error in errors)
    assert any("QDRANT_API_KEY must use a file reference" in error for error in errors)
    assert any("AURIS_EMBEDDING_PROVIDER must be http" in error for error in errors)
    assert any("OTEL_ENABLED must be true" in error for error in errors)
    assert any("operator port must bind to loopback" in error for error in errors)


def test_release_policy_requires_digest_pins_and_prebuilt_images() -> None:
    policy = _load_policy()
    document = policy._render_compose()

    errors = policy.validate_compose(document, release=True)

    assert any("pinned by sha256 digest" in error for error in errors)
    assert any("consume prebuilt images" in error for error in errors)


def test_edge_exposes_readiness_but_never_metrics() -> None:
    nginx = (ROOT / "production" / "edge" / "nginx.conf").read_text(encoding="utf-8")

    assert "location = /readyz" in nginx
    assert "proxy_pass http://bff:8000/readyz" in nginx
    metrics_location = nginx.split("location = /metrics", 1)[1].split("}", 1)[0]
    assert "return 404" in metrics_location
