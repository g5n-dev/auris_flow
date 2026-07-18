from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load_policy() -> ModuleType:
    path = ROOT / "scripts" / "verify_production_compose.py"
    spec = importlib.util.spec_from_file_location("verify_production_compose", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_confidential_oidc_compose() -> dict[str, Any]:
    environment = os.environ.copy()
    environment.update(
        {
            "AURIS_PUBLIC_HOST": "auris.example.com",
            "AURIS_EXTERNAL_CALLBACK_URL": ("https://platform.example.com/callbacks/auris-flow"),
            "AURIS_EXTERNAL_CALLBACK_HOST": "platform.example.com",
            "AURIS_OIDC_CLIENT_SECRET_SOURCE_FILE": str(ROOT / "NOTICE"),
        }
    )
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ROOT / "production" / ".env.example"),
            "--file",
            str(ROOT / "production" / "compose.yaml"),
            "--file",
            str(ROOT / "production" / "compose.oidc-confidential.yaml"),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT / "production",
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


def test_rendered_production_compose_satisfies_candidate_policy() -> None:
    policy = _load_policy()
    document = policy._render_compose()

    assert policy.validate_compose(document) == []
    assert "identity-bootstrap" in document["services"]


def test_rendered_networks_confine_egress_to_application_services() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    services = document["services"]

    assert document["networks"]["internal"]["internal"] is True
    assert document["networks"]["app-egress"]["driver"] == "bridge"
    assert document["networks"]["app-egress"].get("internal", False) is False
    assert set(services["bff"]["networks"]) == {"internal", "app-egress"}
    assert set(services["worker"]["networks"]) == {"internal", "app-egress"}
    assert not services["bff"].get("ports")
    assert not services["worker"].get("ports")

    for name, service in services.items():
        networks = set(service.get("networks") or {})
        assert service.get("network_mode") != "host"
        assert service.get("privileged") is not True
        assert "host-gateway" not in str(service.get("extra_hosts") or {})

        if name in {"bff", "worker"}:
            continue
        if name == "edge":
            assert networks == {"internal", "edge"}
        else:
            assert networks == {"internal"}, name
        assert "app-egress" not in networks


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    (
        (
            lambda document: document["services"]["mysql"].__setitem__("network_mode", "host"),
            "mysql: host network mode is forbidden",
        ),
        (
            lambda document: document["services"]["redis"].__setitem__("privileged", True),
            "redis: privileged mode is forbidden",
        ),
        (
            lambda document: document["services"]["qdrant"].__setitem__(
                "extra_hosts", ["host.docker.internal:host-gateway"]
            ),
            "qdrant: extra_hosts is forbidden",
        ),
        (
            lambda document: document["services"]["mysql"].__setitem__(
                "networks", {"internal": None, "app-egress": None}
            ),
            "mysql: networks must be exactly internal",
        ),
        (
            lambda document: document["services"]["worker"].__setitem__(
                "networks", {"internal": None}
            ),
            "worker: networks must be exactly app-egress, internal",
        ),
        (
            lambda document: document["services"]["edge"].__setitem__("networks", {"edge": None}),
            "edge: networks must be exactly edge, internal",
        ),
        (
            lambda document: document["services"]["bff"].__setitem__("cap_add", ["NET_ADMIN"]),
            "bff: added Linux capabilities are forbidden",
        ),
        (
            lambda document: document["services"]["edge"].__setitem__(
                "cap_add", ["NET_BIND_SERVICE", "NET_ADMIN"]
            ),
            "edge: cap_add must contain only NET_BIND_SERVICE",
        ),
        (
            lambda document: document["networks"]["app-egress"].__setitem__("internal", True),
            "app-egress network must be a non-internal bridge",
        ),
        (
            lambda document: document["networks"]["edge"].__setitem__("external", True),
            "edge network must be managed and non-internal",
        ),
    ),
)
def test_policy_rejects_network_and_privilege_boundary_mutations(
    mutate: Any, expected_error: str
) -> None:
    policy = _load_policy()
    document = policy._render_compose()
    mutate(document)

    errors = policy.validate_compose(document)

    assert expected_error in errors


def test_bff_healthcheck_uses_a_trusted_http_host() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    bff = document["services"]["bff"]
    trusted_hosts = set(bff["environment"]["TRUSTED_HOSTS"].split(","))
    healthcheck = " ".join(bff["healthcheck"]["test"])

    assert "bff" in trusted_hosts
    assert "http://bff:8000/readyz" in healthcheck
    assert "http://127.0.0.1:8000/readyz" not in healthcheck


def test_oidc_defaults_to_reference_keycloak_but_allows_external_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _load_policy()
    for variable in (
        "AURIS_OIDC_ISSUER",
        "AURIS_OIDC_DISCOVERY_URL",
        "AURIS_OIDC_CLIENT_ID",
        "AURIS_OIDC_AUDIENCE",
        "AURIS_OIDC_REDIRECT_URI",
        "AURIS_OIDC_SCOPES",
        "AURIS_OIDC_JWKS_CACHE_TTL_SECONDS",
        "AURIS_OIDC_CLOCK_SKEW_SECONDS",
        "AURIS_OIDC_HTTP_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(variable, raising=False)

    default_document = policy._render_compose()
    default_environment = default_document["services"]["bff"]["environment"]
    assert default_environment["OIDC_ISSUER"] == ("https://auris.example.com/realms/auris-flow")
    assert default_environment["OIDC_CLIENT_ID"] == "auris-flow-web"
    assert default_environment["OIDC_AUDIENCE"] == "auris-flow-api"
    assert default_environment["OIDC_JWKS_CACHE_TTL_SECONDS"] == "300"
    assert default_environment["OIDC_CLOCK_SKEW_SECONDS"] == "30"
    assert default_environment["OIDC_HTTP_TIMEOUT_SECONDS"] == "5"
    assert "OIDC_CLIENT_SECRET" not in default_environment
    assert "OIDC_CLIENT_SECRET_FILE" not in default_environment
    assert (
        default_document["services"]["identity-bootstrap"]["environment"][
            "AURIS_BOOTSTRAP_OIDC_ISSUER"
        ]
        == default_environment["OIDC_ISSUER"]
    )

    external = {
        "AURIS_OIDC_ISSUER": "https://identity.example.net/tenant",
        "AURIS_OIDC_DISCOVERY_URL": (
            "https://identity.example.net/tenant/.well-known/openid-configuration"
        ),
        "AURIS_OIDC_CLIENT_ID": "external-web-client",
        "AURIS_OIDC_AUDIENCE": "external-api-audience",
        "AURIS_OIDC_REDIRECT_URI": ("https://auris.example.com/api/v1/auth/oidc/callback"),
        "AURIS_OIDC_SCOPES": "openid profile email groups",
        "AURIS_OIDC_JWKS_CACHE_TTL_SECONDS": "900",
        "AURIS_OIDC_CLOCK_SKEW_SECONDS": "45",
        "AURIS_OIDC_HTTP_TIMEOUT_SECONDS": "8.5",
    }
    for key, value in external.items():
        monkeypatch.setenv(key, value)

    external_document = policy._render_compose()
    external_environment = external_document["services"]["bff"]["environment"]
    for key, value in external.items():
        assert external_environment[key.removeprefix("AURIS_")] == value
    assert (
        external_document["services"]["identity-bootstrap"]["environment"][
            "AURIS_BOOTSTRAP_OIDC_ISSUER"
        ]
        == external["AURIS_OIDC_ISSUER"]
    )
    assert "/realms/auris-flow" not in external_environment["OIDC_ISSUER"]
    assert "/realms/auris-flow" not in external_environment["OIDC_DISCOVERY_URL"]
    assert (
        "/realms/auris-flow"
        not in external_document["services"]["identity-bootstrap"]["environment"][
            "AURIS_BOOTSTRAP_OIDC_ISSUER"
        ]
    )


def test_oidc_bootstrap_identity_is_an_optional_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _load_policy()
    bootstrap_overrides = {
        "AURIS_BOOTSTRAP_OIDC_SUBJECT": "external-subject-001",
        "AURIS_BOOTSTRAP_IDENTITY_ID": "oidc_external_identity_001",
        "AURIS_BOOTSTRAP_USER_ID": "u_external_operator_001",
    }
    for key, value in bootstrap_overrides.items():
        monkeypatch.setenv(key, value)

    document = policy._render_compose()
    bootstrap_environment = document["services"]["identity-bootstrap"]["environment"]

    for key, value in bootstrap_overrides.items():
        assert bootstrap_environment[key] == value


def test_confidential_oidc_override_adds_only_a_bff_secret_file() -> None:
    document = _render_confidential_oidc_compose()
    services = document["services"]
    bff = services["bff"]
    worker = services["worker"]
    bff_secret_sources = {entry["source"] for entry in bff["secrets"]}
    worker_secret_sources = {entry["source"] for entry in worker["secrets"]}

    assert bff["environment"]["OIDC_CLIENT_SECRET_FILE"] == ("/run/secrets/oidc_client_secret")
    assert "OIDC_CLIENT_SECRET" not in bff["environment"]
    assert "OIDC_CLIENT_SECRET" not in worker["environment"]
    assert "OIDC_CLIENT_SECRET_FILE" not in worker["environment"]
    assert "oidc_client_secret" in bff_secret_sources
    assert "oidc_client_secret" not in worker_secret_sources
    assert set(worker_secret_sources) == {
        "audio_playback_grant_secret",
        "completion_receipt_key_bindings",
        "embedding_api_key",
        "experiment_assignment_secret",
        "external_callback_key_bindings",
        "object_storage_access_key",
        "object_storage_secret_key",
        "qdrant_api_key",
        "redis_url",
        "runtime_database_url",
    }
    assert bff_secret_sources == worker_secret_sources | {"oidc_client_secret"}


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


def test_policy_rejects_oidc_edge_readiness_cycle_and_wrong_internal_tls_port() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    edge = document["services"]["edge"]
    edge["depends_on"]["bff"]["condition"] = "service_healthy"
    edge["ports"][1]["target"] = 8443
    edge["cap_add"] = []

    errors = policy.validate_compose(document)

    assert any("edge: BFF dependency must use service_started" in error for error in errors)
    assert any("edge: internal HTTPS target must be 443" in error for error in errors)
    assert any("edge: cap_add must contain only NET_BIND_SERVICE" in error for error in errors)


def test_edge_exposes_readiness_but_never_metrics() -> None:
    nginx = (ROOT / "production" / "edge" / "nginx.conf").read_text(encoding="utf-8")

    assert "location = /readyz" in nginx
    assert "proxy_pass http://bff:8000/readyz" in nginx
    metrics_location = nginx.split("location = /metrics", 1)[1].split("}", 1)[0]
    assert "return 404" in metrics_location


def test_plain_http_listener_cannot_redirect_through_an_untrusted_host_header() -> None:
    nginx = (ROOT / "production" / "edge" / "nginx.conf").read_text(encoding="utf-8")
    http_server = nginx.split("server {", 1)[1].split("server {", 1)[0]

    assert "return 421" in http_server
    assert "https://$host" not in http_server


def test_first_party_and_release_gate_dockerfiles_pin_every_base_image_digest() -> None:
    dockerfiles = tuple(
        sorted(path for path in (ROOT / "production").rglob("*Dockerfile*") if path.is_file())
    )
    syntax_frontend = (
        "# syntax=docker/dockerfile:1.7@sha256:"
        "a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e"
    )

    assert {path.relative_to(ROOT).as_posix() for path in dockerfiles} == {
        "production/backend/Dockerfile",
        "production/dagster/Dockerfile",
        "production/edge/Dockerfile",
        "production/tests/dagster-gate-callback.Dockerfile",
        "production/visual/Dockerfile",
    }

    for dockerfile in dockerfiles:
        source = dockerfile.read_text(encoding="utf-8")
        assert source.splitlines()[0] == syntax_frontend, dockerfile
        stage_aliases = {
            line.split()[3]
            for line in source.splitlines()
            if line.strip().startswith("FROM ")
            and len(line.split()) >= 4
            and line.split()[2].upper() == "AS"
        }
        from_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("FROM ", "COPY --from="))
        ]
        assert from_lines, dockerfile
        for line in from_lines:
            image = line.split("--from=", 1)[1].split()[0] if "--from=" in line else line.split()[1]
            if image in stage_aliases:
                continue
            assert "@sha256:" in image, f"mutable base image in {dockerfile}: {image}"
