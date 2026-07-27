from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
import re
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


def test_every_service_has_bounded_runtime_and_log_rotation_guardrails() -> None:
    policy = _load_policy()
    document = policy._render_compose()

    for name, service in document["services"].items():
        assert service["cpus"] == 2
        assert service["mem_limit"] == "2147483648"
        assert service["pids_limit"] == 512
        assert service["init"] is True
        assert service["ulimits"]["nofile"] == {"soft": 65536, "hard": 65536}
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-file": "5", "max-size": "10m"},
        }, name

    missing_memory = policy._render_compose()
    missing_memory["services"]["qdrant"].pop("mem_limit")
    unlimited_pids = policy._render_compose()
    unlimited_pids["services"]["worker"]["pids_limit"] = -1
    unbounded_logs = policy._render_compose()
    unbounded_logs["services"]["mysql"]["logging"]["options"].pop("max-file")

    assert "qdrant: memory limit must be between 128 MiB and 64 GiB" in (
        policy.validate_compose(missing_memory)
    )
    assert "worker: PID limit must be between 64 and 4096" in (
        policy.validate_compose(unlimited_pids)
    )
    assert "mysql: json-file logs must rotate at 10m with five retained files" in (
        policy.validate_compose(unbounded_logs)
    )


def test_telemetry_outage_does_not_gate_business_process_startup_or_readiness() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    services = document["services"]
    expected_business_checks = {
        "auth",
        "database",
        "redis",
        "object_storage",
        "qdrant",
        "dagster",
    }
    telemetry_services = {
        "alertmanager",
        "grafana",
        "node-exporter",
        "observability-health",
        "otel-collector",
        "prometheus",
        "tempo",
    }

    for name in ("bff", "worker"):
        assert set(services[name]["environment"]["REQUIRED_DEPENDENCY_CHECKS"].split(",")) == (
            expected_business_checks
        )
    for name in ("bff", "worker", "dagster-code"):
        assert not (set(services[name].get("depends_on") or {}) & telemetry_services)

    hard_readiness = policy._render_compose()
    hard_readiness["services"]["bff"]["environment"]["REQUIRED_DEPENDENCY_CHECKS"] += (
        ",observability"
    )
    startup_cycle = policy._render_compose()
    startup_cycle["services"]["worker"]["depends_on"]["observability-health"] = {
        "condition": "service_healthy"
    }

    assert "bff: strict readiness must contain exactly the business dependencies" in (
        policy.validate_compose(hard_readiness)
    )
    assert "worker: telemetry services must not gate business process startup" in (
        policy.validate_compose(startup_cycle)
    )


def test_capacity_and_backup_freshness_metrics_are_wired_fail_closed() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    node_exporter = document["services"]["node-exporter"]

    assert "--path.rootfs=/host" in node_exporter["command"]
    assert (
        "--collector.textfile.directory=/var/lib/node_exporter/textfile_collector"
        in node_exporter["command"]
    )

    detached_host = policy._render_compose()
    detached_host["services"]["node-exporter"]["volumes"][0]["read_only"] = False
    writable_metrics = policy._render_compose()
    writable_metrics["services"]["node-exporter"]["volumes"][1]["read_only"] = False
    missing_collector = policy._render_compose()
    missing_collector["services"]["node-exporter"]["command"].remove(
        "--collector.textfile.directory=/var/lib/node_exporter/textfile_collector"
    )

    assert (
        "node-exporter: host root must be mounted read-only with rslave propagation"
        in policy.validate_compose(detached_host)
    )
    assert (
        "node-exporter: runtime textfile metrics require one read-only absolute bind"
        in policy.validate_compose(writable_metrics)
    )
    assert (
        "node-exporter: host capacity and textfile collectors must be enabled"
        in policy.validate_compose(missing_collector)
    )


def test_daily_backup_scheduler_is_locked_quiesced_and_recovers_writers() -> None:
    policy = _load_policy()
    wrapper = (ROOT / "production" / "scripts" / "scheduled-backup.sh").read_text(encoding="utf-8")
    service = (ROOT / "production" / "systemd" / "auris-flow-backup.service").read_text(
        encoding="utf-8"
    )
    timer = (ROOT / "production" / "systemd" / "auris-flow-backup.timer").read_text(
        encoding="utf-8"
    )
    environment = (ROOT / "production" / "systemd" / "backup.env.example").read_text(
        encoding="utf-8"
    )

    assert (
        policy.validate_backup_scheduler_assets(
            wrapper_source=wrapper,
            service_source=service,
            timer_source=timer,
            env_source=environment,
        )
        == []
    )

    unlocked = wrapper.replace("flock -n 9", "true", 1)
    destructive = wrapper + "\ncompose down\n"
    ungoverned = service.replace(
        "ExecStart=/opt/auris-flow/production/scripts/scheduled-backup.sh",
        "ExecStart=/bin/true",
        1,
    )
    nonpersistent = timer.replace("Persistent=true", "Persistent=false", 1)
    relative_output = environment.replace(
        "AURIS_BACKUP_OUTPUT_ROOT=/mnt/encrypted-backups/auris-flow",
        "AURIS_BACKUP_OUTPUT_ROOT=./backups",
        1,
    )

    assert any(
        "lock, quiesce" in error
        for error in policy.validate_backup_scheduler_assets(
            wrapper_source=unlocked,
            service_source=service,
            timer_source=timer,
            env_source=environment,
        )
    )
    assert any(
        "destructive Compose down" in error
        for error in policy.validate_backup_scheduler_assets(
            wrapper_source=destructive,
            service_source=service,
            timer_source=timer,
            env_source=environment,
        )
    )
    assert any(
        "hardened governed wrapper" in error
        for error in policy.validate_backup_scheduler_assets(
            wrapper_source=wrapper,
            service_source=ungoverned,
            timer_source=timer,
            env_source=environment,
        )
    )
    assert any(
        "persistent daily timer" in error
        for error in policy.validate_backup_scheduler_assets(
            wrapper_source=wrapper,
            service_source=service,
            timer_source=nonpersistent,
            env_source=environment,
        )
    )
    assert any(
        "absolute and traversal-free" in error
        for error in policy.validate_backup_scheduler_assets(
            wrapper_source=wrapper,
            service_source=service,
            timer_source=timer,
            env_source=relative_output,
        )
    )


def test_scheduled_backup_docker_bind_inputs_remain_visible_with_private_tmp() -> None:
    service = (ROOT / "production" / "systemd" / "auris-flow-backup.service").read_text(
        encoding="utf-8"
    )
    backup = (ROOT / "production" / "scripts" / "backup.sh").read_text(encoding="utf-8")

    assert "PrivateTmp=true" in service
    assert 'mktemp "${STAGING_DIR}/.auris-flow-minio-backup.XXXXXX"' in backup
    assert 'mktemp "${TMPDIR:-/tmp}/auris-flow-minio-backup.XXXXXX"' not in backup


def test_rendered_networks_confine_egress_to_authorized_services() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    services = document["services"]

    assert document["networks"]["internal"]["internal"] is True
    assert document["networks"]["app-egress"]["driver"] == "bridge"
    assert document["networks"]["app-egress"].get("internal", False) is False
    assert set(services["bff"]["networks"]) == {"internal", "app-egress"}
    assert set(services["worker"]["networks"]) == {"internal", "app-egress"}
    assert set(services["alertmanager"]["networks"]) == {"internal", "app-egress"}
    assert set(services["dagster-code"]["networks"]) == {"internal", "app-egress"}
    assert not services["bff"].get("ports")
    assert not services["worker"].get("ports")
    assert set(services["mysql"]["cap_add"]) == {
        "CHOWN",
        "DAC_OVERRIDE",
        "SETGID",
        "SETUID",
    }

    for name, service in services.items():
        networks = set(service.get("networks") or {})
        assert service.get("network_mode") != "host"
        assert service.get("privileged") is not True
        assert "host-gateway" not in str(service.get("extra_hosts") or {})

        if name in {"bff", "worker", "alertmanager", "dagster-code"}:
            continue
        if name == "minio-volume-init":
            assert service.get("network_mode") == "none"
            assert networks == set()
            continue
        if name == "edge":
            assert networks == {"internal", "edge"}
        else:
            assert networks == {"internal"}, name
        assert "app-egress" not in networks


def test_dagster_storage_bootstrap_policy_is_fail_closed() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    services = document["services"]
    bootstrap = services["dagster-storage-bootstrap"]

    assert bootstrap["restart"] == "no"
    assert bootstrap["read_only"] is True
    assert bootstrap["command"] == ["storage-bootstrap"]
    assert bootstrap["environment"] == {"DAGSTER_HOME": "/opt/dagster/home"}
    assert bootstrap["secrets"] == [
        {
            "source": "dagster_database_url",
            "target": "/run/secrets/dagster_database_url",
        }
    ]
    assert bootstrap.get("volumes", []) == []
    assert bootstrap["depends_on"] == {
        "db-bootstrap": {
            "condition": "service_completed_successfully",
            "required": True,
        }
    }
    for service_name in ("dagster-code", "dagster-webserver", "dagster-daemon"):
        assert services[service_name]["depends_on"]["dagster-storage-bootstrap"] == {
            "condition": "service_completed_successfully",
            "required": True,
        }

    missing = policy._render_compose()
    missing["services"].pop("dagster-storage-bootstrap")
    excessive_secret = policy._render_compose()
    excessive_secret["services"]["dagster-storage-bootstrap"]["secrets"].append(
        {
            "source": "completion_receipt_key_bindings",
            "target": "completion_receipt_key_bindings",
        }
    )
    writable = policy._render_compose()
    writable["services"]["dagster-storage-bootstrap"]["read_only"] = False
    wrong_role = policy._render_compose()
    wrong_role["services"]["dagster-storage-bootstrap"]["command"] = ["webserver"]
    premature = policy._render_compose()
    premature["services"]["dagster-storage-bootstrap"]["depends_on"]["db-bootstrap"][
        "condition"
    ] = "service_started"
    detached = policy._render_compose()
    detached["services"]["dagster-webserver"]["depends_on"].pop("dagster-storage-bootstrap")

    assert "missing production services: dagster-storage-bootstrap" in policy.validate_compose(
        missing
    )
    assert (
        "dagster-storage-bootstrap: only the Dagster database URL may be mounted"
        in policy.validate_compose(excessive_secret)
    )
    assert (
        "dagster-storage-bootstrap: root filesystem must be read-only"
        in policy.validate_compose(writable)
    )
    assert (
        "dagster-storage-bootstrap: dedicated entrypoint role is required"
        in policy.validate_compose(wrong_role)
    )
    assert (
        "dagster-storage-bootstrap: database bootstrap dependency must complete successfully"
        in policy.validate_compose(premature)
    )
    assert (
        "dagster-webserver: Dagster storage bootstrap dependency must complete successfully"
        in policy.validate_compose(detached)
    )


def test_qdrant_backup_tool_has_a_single_secret_and_no_application_egress() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    tool = document["services"]["qdrant-backup-tool"]

    assert tool["profiles"] == ["backup-tools"]
    assert tool["restart"] == "no"
    assert tool["read_only"] is True
    assert tool["cap_drop"] == ["ALL"]
    assert set(tool["networks"]) == {"internal"}
    assert tool["environment"] == {
        "AURIS_BACKUP_QDRANT_API_KEY_FILE": "/run/secrets/qdrant_api_key",
        "AURIS_BACKUP_QDRANT_URL": "http://qdrant:6333",
    }
    assert {
        secret["source"] if isinstance(secret, dict) else secret for secret in tool["secrets"]
    } == {"qdrant_api_key"}

    leaked = policy._render_compose()
    leaked["services"]["qdrant-backup-tool"]["secrets"].append(
        {"source": "runtime_database_url", "target": "runtime_database_url"}
    )
    assert "qdrant-backup-tool: only the Qdrant API key may be mounted" in policy.validate_compose(
        leaked
    )


def test_alertmanager_notification_boundary_is_policy_enforced() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    services = document["services"]
    assert "alertmanager" in services

    missing_secret = policy._render_compose()
    missing_secret["services"]["alertmanager"]["secrets"] = []
    unsafe_command = policy._render_compose()
    unsafe_command["services"]["alertmanager"]["command"].append(
        "--webhook-url=https://should-never-be-in-a-command.invalid"
    )
    prometheus_detached = policy._render_compose()
    prometheus_detached["services"]["prometheus"]["depends_on"].pop("alertmanager")

    assert "alertmanager: webhook URL must use the dedicated Docker secret" in (
        policy.validate_compose(missing_secret)
    )
    assert "alertmanager: command must not contain notification URLs" in (
        policy.validate_compose(unsafe_command)
    )
    assert "prometheus: Alertmanager dependency must be healthy" in (
        policy.validate_compose(prometheus_detached)
    )


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
            "edge: cap_add must contain exactly NET_BIND_SERVICE",
        ),
        (
            lambda document: document["services"]["mysql"].__setitem__(
                "cap_add", ["CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID", "SYS_ADMIN"]
            ),
            "mysql: cap_add must contain exactly CHOWN, DAC_OVERRIDE, SETGID, SETUID",
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


def test_bff_proxy_headers_trust_only_the_static_edge_address() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    bff_command = document["services"]["bff"]["command"]
    edge_network = document["services"]["edge"]["networks"]["internal"]
    internal_ipam = document["networks"]["internal"]["ipam"]

    flag_index = bff_command.index("--forwarded-allow-ips")
    trusted_proxy = bff_command[flag_index + 1]
    edge_ip = edge_network["ipv4_address"]
    subnets = [ipaddress.ip_network(item["subnet"]) for item in internal_ipam["config"]]

    assert trusted_proxy == edge_ip
    assert trusted_proxy != "*"
    assert ipaddress.ip_address(edge_ip) in next(
        subnet for subnet in subnets if ipaddress.ip_address(edge_ip) in subnet
    )


def test_bff_disables_uvicorn_access_logs_and_defaults_to_one_process() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    bff = document["services"]["bff"]
    command = [str(item) for item in bff["command"]]
    env_example = (ROOT / "production" / ".env.example").read_text(encoding="utf-8")

    assert command.count("--no-access-log") == 1
    assert bff["environment"]["WEB_CONCURRENCY"] == "1"
    assert re.search(r"^AURIS_BFF_WORKERS=1$", env_example, re.MULTILINE)


def test_compose_policy_rejects_uvicorn_access_logs() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    document["services"]["bff"]["command"].remove("--no-access-log")

    errors = policy.validate_compose(document)

    assert "bff: Uvicorn access logging must be disabled" in errors


@pytest.mark.parametrize(
    "worker_override",
    [
        ["--workers", "2"],
        ["--workers=2"],
        ["-w", "2"],
    ],
)
def test_compose_policy_rejects_uvicorn_cli_worker_overrides(
    worker_override: list[str],
) -> None:
    policy = _load_policy()
    document = policy._render_compose()
    document["services"]["bff"]["command"].extend(worker_override)

    errors = policy.validate_compose(document)

    assert "bff: Uvicorn CLI worker override is forbidden" in errors


@pytest.mark.parametrize("value", ["0", "2"])
def test_compose_policy_rejects_non_singleton_bff_process_configuration(
    value: str,
) -> None:
    policy = _load_policy()
    document = policy._render_compose()
    document["services"]["bff"]["environment"]["WEB_CONCURRENCY"] = value

    errors = policy.validate_compose(document)

    assert "bff: WEB_CONCURRENCY must be 1 in prod/release" in errors


def test_compose_policy_rejects_non_singleton_auris_bff_workers_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _load_policy()
    monkeypatch.setenv("AURIS_BFF_WORKERS", "2")

    document = policy._render_compose()
    errors = policy.validate_compose(document)

    assert document["services"]["bff"]["environment"]["WEB_CONCURRENCY"] == "2"
    assert "bff: WEB_CONCURRENCY must be 1 in prod/release" in errors


def test_compose_policy_rejects_wildcard_or_non_edge_forwarded_proxy_trust() -> None:
    policy = _load_policy()
    wildcard = policy._render_compose()
    wildcard_command = wildcard["services"]["bff"]["command"]
    wildcard_command[wildcard_command.index("--forwarded-allow-ips") + 1] = "*"

    mismatch = policy._render_compose()
    mismatch_command = mismatch["services"]["bff"]["command"]
    mismatch_command[mismatch_command.index("--forwarded-allow-ips") + 1] = "172.31.48.99"

    duplicate = policy._render_compose()
    duplicate_command = duplicate["services"]["bff"]["command"]
    duplicate_command.extend(["--forwarded-allow-ips", "*"])

    assert "bff: forwarded proxy trust must equal the static edge internal IP" in (
        policy.validate_compose(wildcard)
    )
    assert "bff: forwarded proxy trust must equal the static edge internal IP" in (
        policy.validate_compose(mismatch)
    )
    assert "bff: forwarded proxy trust must equal the static edge internal IP" in (
        policy.validate_compose(duplicate)
    )


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
    assert bff_secret_sources == worker_secret_sources | {
        "oidc_client_secret",
        "platform_credential_bindings",
    }


def test_platform_audio_credentials_are_file_mounted_only_where_required() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    services = document["services"]

    assert services["bff"]["environment"]["PLATFORM_CREDENTIAL_BINDINGS_FILE"] == (
        "/run/secrets/platform_credential_bindings"
    )
    assert (
        services["dagster-code"]["environment"]["AURIS_PLATFORM_CREDENTIAL_BINDINGS_FILE"]
        == "/run/secrets/platform_credential_bindings"
    )
    assert (
        services["dagster-code"]["environment"]["AURIS_PLATFORM_AUDIO_ALLOWED_HOSTS"]
        == "media.example.com"
    )

    for name, service in services.items():
        mounted = {
            str(item.get("source") or "") if isinstance(item, dict) else str(item)
            for item in service.get("secrets", [])
        }
        if name in {"bff", "dagster-code"}:
            assert "platform_credential_bindings" in mounted
        else:
            assert "platform_credential_bindings" not in mounted

    wildcard = policy._render_compose()
    wildcard["services"]["dagster-code"]["environment"]["AURIS_PLATFORM_AUDIO_ALLOWED_HOSTS"] = "*"
    errors = policy.validate_compose(wildcard)
    assert any("platform audio host allowlist" in error for error in errors)


def test_backup_manifest_trust_keys_are_declared_but_never_mounted_into_services() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    trust_secrets = {
        "backup_manifest_signing_private_key",
        "backup_manifest_signing_public_key",
    }

    assert document["x-auris-backup-manifest-trust"] == {
        "algorithm": "ed25519",
        "exposure": "host-backup-tools-only",
        "private_key_file": "./secrets/backup_manifest_signing_private_key.pem",
        "public_key_file": "./secrets/backup_manifest_signing_public_key.pem",
    }
    compose_source = (ROOT / "production" / "compose.yaml").read_text(encoding="utf-8")
    assert trust_secrets <= {name for name in trust_secrets if f"  {name}:\n" in compose_source}
    for service in document["services"].values():
        mounted = {
            str(item.get("source") or "") if isinstance(item, dict) else str(item)
            for item in service.get("secrets", [])
        }
        assert not trust_secrets & mounted

    del document["x-auris-backup-manifest-trust"]["public_key_file"]
    errors = policy.validate_compose(document)
    assert any("backup manifest trust keys" in error for error in errors)

    suffix_spoof = policy._render_compose()
    suffix_spoof["x-auris-backup-manifest-trust"]["private_key_file"] = (
        "./secrets/evilbackup_manifest_signing_private_key.pem"
    )
    errors = policy.validate_compose(suffix_spoof)
    assert any("backup manifest trust keys" in error for error in errors)


def test_policy_rejects_latest_secret_environment_and_public_database() -> None:
    policy = _load_policy()
    document = policy._render_compose()
    document["services"]["mysql"]["image"] = "mysql:latest"
    document["services"]["mysql"]["ports"] = [{"target": 3306, "published": "3306"}]
    document["services"]["bff"]["environment"]["QDRANT_API_KEY"] = "visible-value"
    document["services"]["worker"]["environment"]["AURIS_EMBEDDING_PROVIDER"] = "deterministic_test"
    document["services"]["worker"]["environment"]["OTEL_ENABLED"] = "false"
    document["services"]["dagster-code"]["environment"]["AURIS_AUDIO_INFERENCE_ENDPOINT"] = (
        "http://inference.example.com/v1/audio-intelligence"
    )
    document["services"]["grafana"]["ports"][0]["host_ip"] = "0.0.0.0"

    errors = policy.validate_compose(document)

    assert any("non-latest" in error for error in errors)
    assert any("must not publish host ports" in error for error in errors)
    assert any("QDRANT_API_KEY must use a file reference" in error for error in errors)
    assert any("AURIS_EMBEDDING_PROVIDER must be http" in error for error in errors)
    assert any("OTEL_ENABLED must be true" in error for error in errors)
    assert any("audio inference endpoint must use HTTPS" in error for error in errors)
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
    assert any("edge: cap_add must contain exactly NET_BIND_SERVICE" in error for error in errors)


def test_edge_exposes_readiness_but_never_metrics() -> None:
    nginx = (ROOT / "production" / "edge" / "nginx.conf").read_text(encoding="utf-8")

    assert "location = /readyz" in nginx
    assert "proxy_pass http://bff:8000/readyz" in nginx
    assert "limit_req zone=auris_readiness" in nginx
    assert "proxy_cache auris_readyz" in nginx
    assert "proxy_cache_lock on" in nginx
    assert "proxy_cache_lock_timeout 6s" in nginx
    assert "proxy_cache_lock_age 6s" in nginx
    metrics_location = nginx.split("location = /metrics", 1)[1].split("}", 1)[0]
    assert "return 404" in metrics_location


def test_edge_policy_rejects_readiness_stampede_and_trace_boundary_regressions() -> None:
    policy = _load_policy()
    nginx = (ROOT / "production" / "edge" / "nginx.conf").read_text(encoding="utf-8")
    unsafe = re.sub(
        r"proxy_cache_lock_timeout\s+\d+s;",
        "proxy_cache_lock_timeout 1s;",
        nginx,
    )
    unsafe = re.sub(r"^\s*proxy_cache_lock_age\s+\d+s;\n", "", unsafe, flags=re.MULTILINE)
    unsafe = unsafe.replace(
        "proxy_set_header traceparent $http_traceparent;",
        'proxy_set_header traceparent "";',
    )

    errors = policy.validate_edge_nginx(unsafe)

    assert any("cache lock timeout" in error for error in errors)
    assert any("cache lock age" in error for error in errors)
    assert any("business traceparent" in error for error in errors)


def test_edge_overwrites_forwarded_for_and_never_logs_audio_grant_queries() -> None:
    nginx = (ROOT / "production" / "edge" / "nginx.conf").read_text(encoding="utf-8")
    forwarded_for_directives = re.findall(
        r"proxy_set_header\s+X-Forwarded-For\s+([^;]+);",
        nginx,
    )
    audio_location_match = re.search(
        r"location\s*=\s*/api/v1/audio-playback\s*\{(?P<body>.*?)\n\s*\}",
        nginx,
        re.DOTALL,
    )

    assert forwarded_for_directives
    assert set(forwarded_for_directives) == {"$remote_addr"}
    assert "$proxy_add_x_forwarded_for" not in nginx
    assert audio_location_match is not None
    audio_location = audio_location_match.group("body")
    assert re.search(r"\baccess_log\s+off\s*;", audio_location)
    assert "proxy_pass http://bff:8000" in audio_location
    assert "X-Forwarded-For $remote_addr" in audio_location
    assert "$request_uri" not in audio_location


def test_edge_uses_server_scoped_query_safe_json_access_logs() -> None:
    nginx = (ROOT / "production" / "edge" / "nginx.conf").read_text(encoding="utf-8")
    log_format = re.search(
        r"log_format\s+auris_safe_json\s+escape=json\s+'(?P<body>\{[^\n]+\})'\s*;",
        nginx,
    )

    assert log_format is not None
    body = log_format.group("body")
    variables = set(re.findall(r"\$[A-Za-z0-9_]+", body))
    assert variables == {
        "$body_bytes_sent",
        "$request_id",
        "$request_method",
        "$request_time",
        "$status",
        "$time_iso8601",
        "$uri",
    }
    rendered_json = re.sub(r"\$[A-Za-z0-9_]+", "value", body)
    assert json.loads(rendered_json)["path"] == "value"
    assert nginx.count("access_log /dev/stdout auris_safe_json;") == 2


def test_nginx_policy_rejects_query_bearing_log_fields_and_inherited_defaults() -> None:
    policy = _load_policy()
    nginx = (ROOT / "production" / "edge" / "nginx.conf").read_text(encoding="utf-8")
    query_bearing = nginx.replace('"path":"$uri"', '"path":"$request_uri"', 1)
    identifying = nginx.replace(
        '"method":"$request_method"',
        '"remote_addr":"$remote_addr","method":"$request_method"',
        1,
    )
    unsafe_location_override = nginx.replace(
        "    location /api/ {\n",
        "    location /api/ {\n        access_log /dev/stdout combined;\n",
        1,
    )
    inherited_default = nginx.replace(
        "    access_log /dev/stdout auris_safe_json;\n",
        "",
        1,
    )

    assert "edge nginx: access log format contains query-bearing variables" in (
        policy.validate_edge_nginx(query_bearing)
    )
    assert "edge nginx: access log format must use only the approved safe variables" in (
        policy.validate_edge_nginx(identifying)
    )
    assert "edge nginx: access_log directives must use auris_safe_json or off" in (
        policy.validate_edge_nginx(unsafe_location_override)
    )
    assert "edge nginx: every server must override access_log with auris_safe_json" in (
        policy.validate_edge_nginx(inherited_default)
    )


def test_nginx_policy_rejects_forwarded_chain_append_and_playback_access_logging() -> None:
    policy = _load_policy()
    nginx = (ROOT / "production" / "edge" / "nginx.conf").read_text(encoding="utf-8")
    append_chain = nginx.replace(
        "X-Forwarded-For $remote_addr",
        "X-Forwarded-For $proxy_add_x_forwarded_for",
        1,
    )
    logged_playback = nginx.replace(
        "location = /api/v1/audio-playback {\n        access_log off;",
        "location = /api/v1/audio-playback {\n        access_log /var/log/nginx/access.log;",
        1,
    )

    assert policy.validate_edge_nginx(nginx) == []
    assert "edge nginx: every X-Forwarded-For header must overwrite with $remote_addr" in (
        policy.validate_edge_nginx(append_chain)
    )
    assert "edge nginx: audio playback grant location must disable access logging" in (
        policy.validate_edge_nginx(logged_playback)
    )


def test_nginx_policy_requires_unbuffered_audio_range_forwarding() -> None:
    policy = _load_policy()
    nginx = (ROOT / "production" / "edge" / "nginx.conf").read_text(encoding="utf-8")
    missing_proxy_buffering = nginx.replace("        proxy_buffering off;\n", "", 1)
    missing_request_buffering = nginx.replace(
        "        proxy_request_buffering off;\n",
        "",
        1,
    )
    missing_range = nginx.replace(
        "        proxy_set_header Range $http_range;\n",
        "",
        1,
    )
    missing_if_range = nginx.replace(
        "        proxy_set_header If-Range $http_if_range;\n",
        "",
        1,
    )

    assert policy.validate_edge_nginx(nginx) == []
    assert "edge nginx: audio playback location must disable proxy buffering" in (
        policy.validate_edge_nginx(missing_proxy_buffering)
    )
    assert "edge nginx: audio playback location must disable request buffering" in (
        policy.validate_edge_nginx(missing_request_buffering)
    )
    assert "edge nginx: audio playback location must explicitly forward Range" in (
        policy.validate_edge_nginx(missing_range)
    )
    assert "edge nginx: audio playback location must explicitly forward If-Range" in (
        policy.validate_edge_nginx(missing_if_range)
    )


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
