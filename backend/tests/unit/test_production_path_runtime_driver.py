from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load_driver() -> ModuleType:
    path = ROOT / "scripts" / "verify_production_path_runtime.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_payload() -> dict[str, object]:
    return {
        "identity": {"provider": "oidc"},
        "adapters": {"dagster": {"mode": "real"}},
        "observability": {"otel_enabled": True},
        "trace": {"primary_business_trace_id": "trace_runtime_driver"},
        "raw_proofs": {"schema_version": "auris.production-path.raw-proofs.v1"},
        "recovery": {"mysql_restart": {"proven": True}},
    }


def _runtime_inventory(driver: ModuleType) -> dict[str, object]:
    def observation(service: str, *, completed: bool) -> dict[str, object]:
        external = service in driver.EXTERNAL_IMAGE_SERVICES
        repository = f"example/{service}"
        return {
            "container_id_sha256": hashlib.sha256(service.encode()).hexdigest(),
            "configured_image": f"{repository}:pinned",
            "image_id": "sha256:" + hashlib.sha256(f"image:{service}".encode()).hexdigest(),
            "repo_digests": (
                [f"{repository}@sha256:" + hashlib.sha256(f"repo:{service}".encode()).hexdigest()]
                if external
                else []
            ),
            "os": "linux",
            "architecture": "amd64",
            "state": "exited" if completed else "running",
            **({"exit_code": 0} if completed else {"health": "healthy"}),
        }

    return {
        "running_services": {
            service: observation(service, completed=False)
            for service in driver.REQUIRED_RUNNING_SERVICES
        },
        "completed_services": {
            service: observation(service, completed=True)
            for service in driver.REQUIRED_COMPLETED_SERVICES
        },
    }


def _host_runtime() -> dict[str, object]:
    return {
        "schema_version": "auris.production-path.host-runtime.v1",
        "native_linux": True,
        "host_platform": "linux",
        "docker_endpoint_scheme": "unix",
        "docker_endpoint_path": "/var/run/docker.sock",
        "docker_ostype": "linux",
        "docker_operating_system": "Ubuntu 24.04 LTS",
        "architecture": "amd64",
        "rootless": False,
        "cgroup_driver": "systemd",
        "cgroup_version": "2",
        "storage_driver": "overlay2",
    }


def test_fault_plan_covers_each_required_recovery_case_once() -> None:
    driver = _load_driver()

    plan = driver.fault_plan()

    assert [item.name for item in plan] == [
        "mysql_restart",
        "worker_crash",
        "duplicate_delivery",
        "callback_timeout",
        "qdrant_outage",
        "redis_outage",
    ]
    assert plan[0].host_action == "restart"
    assert plan[1].host_action == "kill-start"
    assert plan[2].service == "worker"
    assert plan[2].host_action == "stop-start"
    assert plan[-1].service == "redis"


def test_host_observation_hashes_runtime_identity_without_exposing_container_id() -> None:
    driver = _load_driver()

    observation = driver.build_host_observation(
        "worker_crash",
        before={"container_id": "container-before", "started_at": "2026-01-01T00:00:00Z"},
        after={"container_id": "container-after", "started_at": "2026-01-01T00:01:00Z"},
    )

    assert observation == {
        "schema_version": "auris.production-path.host-observation.v1",
        "dependency": "worker_crash",
        "container_id_sha256": driver._sha256_bytes(b"container-after"),
        "started_at_before": "2026-01-01T00:00:00Z",
        "started_at_after": "2026-01-01T00:01:00Z",
    }
    assert "container-after" not in json.dumps(observation)

    with pytest.raises(driver.RuntimeGateError):
        driver.build_host_observation(
            "mysql_restart",
            before={"container_id": "same", "started_at": "2026-01-01T00:00:00Z"},
            after={"container_id": "same", "started_at": "2026-01-01T00:00:00Z"},
        )


def test_build_evidence_binds_compose_and_every_runtime_source(tmp_path: Path) -> None:
    driver = _load_driver()
    (tmp_path / "production" / "tests").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    base = tmp_path / "production" / "compose.yaml"
    overlay = tmp_path / "production" / "tests" / "production-path-gate.compose.yaml"
    base.write_text("services: {bff: {}}\n", encoding="utf-8")
    overlay.write_text("services: {production-path-verifier: {}}\n", encoding="utf-8")
    for relative in driver.RUNTIME_SOURCE_PATHS:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"source:{relative}\n", encoding="utf-8")

    services = sorted(
        set(driver.REQUIRED_RUNNING_SERVICES)
        | set(driver.REQUIRED_COMPLETED_SERVICES)
        | {"production-path-verifier"}
    )
    rendered = json.dumps({"services": {service: {} for service in services}}).encode()
    evidence = driver.build_evidence(
        root=tmp_path,
        source_commit="a" * 40,
        base_compose=base,
        gate_compose=overlay,
        rendered_config=rendered,
        services=services,
        host_runtime=_host_runtime(),
        runtime_inventory=_runtime_inventory(driver),
        runtime_payload=_runtime_payload(),
    )

    assert evidence["schema_version"] == "auris.production-path-gate.v1"
    assert evidence["status"] == "ok"
    assert evidence["source_commit"] == "a" * 40
    assert evidence["execution_environment"] == "production-compose"
    assert evidence["producer"] == "scripts/verify_production_path_runtime.py"
    assert set(evidence["runtime_sources"]) == set(driver.RUNTIME_SOURCE_PATHS)
    assert evidence["identity"] == {"provider": "oidc"}
    assert evidence["compose"]["host_runtime"] == _host_runtime()
    assert evidence["compose"]["runtime"] == _runtime_inventory(driver)

    emulated_host = {**_host_runtime(), "architecture": "arm64"}
    with pytest.raises(
        driver.RuntimeGateError,
        match="runtime container architecture must match the native Linux host",
    ):
        driver.build_evidence(
            root=tmp_path,
            source_commit="a" * 40,
            base_compose=base,
            gate_compose=overlay,
            rendered_config=rendered,
            services=services,
            host_runtime=emulated_host,
            runtime_inventory=_runtime_inventory(driver),
            runtime_payload=_runtime_payload(),
        )


def test_native_linux_host_runtime_is_local_rootful_and_evidence_safe() -> None:
    driver = _load_driver()
    docker_info = {
        "OSType": "linux",
        "OperatingSystem": "Ubuntu 24.04 LTS",
        "Architecture": "x86_64",
        "SecurityOptions": ["name=seccomp,profile=builtin", "name=cgroupns"],
        "CgroupDriver": "systemd",
        "CgroupVersion": "2",
        "Driver": "overlay2",
    }

    observation = driver.build_native_linux_host_observation(
        platform_name="linux",
        docker_endpoint="unix:///var/run/docker.sock",
        docker_info=docker_info,
    )

    assert observation == _host_runtime()
    driver._scan_evidence_value(observation)


@pytest.mark.parametrize(
    ("platform_name", "docker_endpoint", "overrides"),
    [
        ("darwin", "unix:///var/run/docker.sock", {}),
        ("linux", "tcp://docker.example:2376", {}),
        ("linux", "unix:///run/user/1000/docker.sock", {}),
        ("linux", "unix:///var/run/docker.sock", {"OSType": "windows"}),
        (
            "linux",
            "unix:///var/run/docker.sock",
            {"OperatingSystem": "Docker Desktop"},
        ),
        ("linux", "unix:///var/run/docker.sock", {"Architecture": "ppc64le"}),
        (
            "linux",
            "unix:///var/run/docker.sock",
            {"SecurityOptions": ["name=rootless"]},
        ),
        ("linux", "unix:///var/run/docker.sock", {"Driver": ""}),
        ("linux", "unix:///var/run/docker.sock", {"CgroupDriver": ""}),
        ("linux", "unix:///var/run/docker.sock", {"CgroupVersion": ""}),
    ],
)
def test_native_linux_host_runtime_rejects_desktop_remote_or_unsupported_daemons(
    platform_name: str,
    docker_endpoint: str,
    overrides: dict[str, object],
) -> None:
    driver = _load_driver()
    docker_info: dict[str, object] = {
        "OSType": "linux",
        "OperatingSystem": "Ubuntu 24.04 LTS",
        "Architecture": "aarch64",
        "SecurityOptions": ["name=seccomp,profile=builtin"],
        "CgroupDriver": "systemd",
        "CgroupVersion": "2",
        "Driver": "overlay2",
        **overrides,
    }

    with pytest.raises(driver.RuntimeGateError):
        driver.build_native_linux_host_observation(
            platform_name=platform_name,
            docker_endpoint=docker_endpoint,
            docker_info=docker_info,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {**_runtime_payload(), "token": "secret"},
        {**_runtime_payload(), "trace": {"path": "/" + "Users/example/private"}},
        {**_runtime_payload(), "adapters": {"cookie": "opaque-value"}},
        {**_runtime_payload(), "raw_proofs": {"response_body": "hidden"}},
        {**_runtime_payload(), "adapters": {"qdrant_api_key": "hidden"}},
        {**_runtime_payload(), "adapters": {"credential_value": "hidden"}},
        {**_runtime_payload(), "adapters": {"request_headers": {"x": "hidden"}}},
        {**_runtime_payload(), "adapters": {"private_key": "hidden"}},
        {**_runtime_payload(), "adapters": {"access_key": "hidden"}},
        {**_runtime_payload(), "adapters": {"signing_key": "hidden"}},
        {**_runtime_payload(), "adapters": {"encryption_key": "hidden"}},
    ],
)
def test_runtime_payload_rejects_credentials_bodies_and_personal_paths(
    payload: dict[str, object],
) -> None:
    driver = _load_driver()

    with pytest.raises(driver.RuntimeGateError):
        driver.validate_runtime_payload(payload)


def test_runtime_payload_allows_only_hashed_fencing_claim_metadata() -> None:
    driver = _load_driver()
    payload = _runtime_payload()
    recovery = payload["recovery"]
    assert isinstance(recovery, dict)
    recovery["duplicate_delivery"] = {
        "claim_token_sha256_before": "1" * 64,
        "claim_token_sha256_after": "2" * 64,
    }

    assert driver.validate_runtime_payload(payload) == payload


def test_atomic_json_refuses_existing_or_symlink_target(tmp_path: Path) -> None:
    driver = _load_driver()
    artifact = tmp_path / "evidence.json"

    driver.write_json_once(artifact, {"status": "ok"})
    assert json.loads(artifact.read_text(encoding="utf-8")) == {"status": "ok"}
    with pytest.raises(driver.RuntimeGateError):
        driver.write_json_once(artifact, {"status": "replaced"})

    link = tmp_path / "linked.json"
    link.symlink_to(artifact)
    with pytest.raises(driver.RuntimeGateError):
        driver.write_json_once(link, {"status": "forged"})


def test_project_name_and_cleanup_target_are_narrowly_scoped(tmp_path: Path) -> None:
    driver = _load_driver()
    candidate = tmp_path / "auris-production-path-gate.abc123"
    candidate.mkdir()

    assert driver.valid_project_name("auris-production-path-gate-1720000000-1234-a1b2c3d4")
    assert not driver.valid_project_name("auris-flow")
    assert not driver.valid_project_name("auris-production-path-gate-../../home")
    assert driver.safe_temp_root(
        candidate,
        parent=tmp_path,
    )
    assert not driver.safe_temp_root(tmp_path, parent=tmp_path)


def test_verifier_phase_overrides_compose_command_with_exact_phase_and_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    runtime = object.__new__(driver.ComposeRuntime)
    runtime.command_timeout = 900
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_run(*arguments: str, **kwargs: object) -> None:
        calls.append((arguments, kwargs))

    monkeypatch.setattr(runtime, "run", fake_run)

    runtime.verifier_phase("fault-during", "redis_outage", "run-123")

    assert calls == [
        (
            (
                "run",
                "--rm",
                "--no-deps",
                "production-path-verifier",
                "python",
                "/opt/auris-gate/production_path_verifier.py",
                "--phase",
                "fault-during",
                "--dependency",
                "redis_outage",
                "--artifact-dir",
                "/artifacts",
                "--run-suffix",
                "run-123",
            ),
            {
                "timeout": max(runtime.command_timeout, 600),
                "label": "production verifier fault-during/redis_outage",
            },
        )
    ]


def test_main_never_deletes_a_preexisting_canonical_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    artifact = tmp_path / "production-path-gate.json"
    artifact.write_text('{"owner":"another-run"}\n', encoding="utf-8")
    args = SimpleNamespace(
        base_compose=tmp_path / "compose.yaml",
        gate_compose=tmp_path / "gate.yaml",
        source_commit="a" * 40,
        artifact=artifact,
        command_timeout=900,
        wait_timeout=600,
    )
    monkeypatch.setattr(driver, "parse_args", lambda: args)

    def fail_without_ownership(**_kwargs: object) -> None:
        raise driver.RuntimeGateError("artifact already exists")

    monkeypatch.setattr(driver, "run_gate", fail_without_ownership)

    assert driver.main() == 1
    assert artifact.read_text(encoding="utf-8") == '{"owner":"another-run"}\n'


def test_runtime_environment_drops_host_compose_overrides_and_pins_dependencies() -> None:
    driver = _load_driver()
    host = {
        "PATH": "/trusted/bin",
        "HOME": "/trusted/home",
        "DOCKER_CONFIG": "/trusted/docker",
        "MYSQL_IMAGE": "attacker/mysql:latest",
        "AURIS_BFF_IMAGE": "attacker/bff:latest",
        "COMPOSE_FILE": "/tmp/attacker.yaml",
        "PYTHONPATH": "/tmp/injected",
        "AWS_SECRET_ACCESS_KEY": "must-not-propagate",
    }

    clean = driver.clean_host_environment(host)

    assert clean == {
        "PATH": "/trusted/bin",
        "HOME": "/trusted/home",
        "DOCKER_CONFIG": "/trusted/docker",
    }
    assert driver.PINNED_EXTERNAL_IMAGES == {
        "MYSQL_IMAGE": "mysql:8.4.5",
        "REDIS_IMAGE": "redis:7.4.2-alpine3.21",
        "MINIO_IMAGE": "minio/minio:RELEASE.2025-04-22T22-12-26Z",
        "MINIO_MC_IMAGE": "minio/mc:RELEASE.2025-04-16T18-13-26Z",
        "QDRANT_IMAGE": "qdrant/qdrant:v1.14.1",
        "KEYCLOAK_IMAGE": "quay.io/keycloak/keycloak:26.2.5",
        "OTEL_COLLECTOR_IMAGE": "otel/opentelemetry-collector-contrib:0.128.0",
        "TEMPO_IMAGE": "grafana/tempo:2.8.0",
        "PROMETHEUS_IMAGE": "prom/prometheus:v3.4.1",
        "GRAFANA_IMAGE": "grafana/grafana:12.0.1",
        "NODE_EXPORTER_IMAGE": "prom/node-exporter:v1.9.1",
    }


def test_gate_leaf_key_and_control_secret_are_readable_without_mounting_ca_key(
    tmp_path: Path,
) -> None:
    driver = _load_driver()
    tls_dir = tmp_path / "tls"
    driver._generate_tls(tls_dir, env=os.environ)
    control_secret = tmp_path / "control-secret"
    driver.write_control_secret(control_secret)

    assert tls_dir.joinpath("ca-key.pem").stat().st_mode & 0o777 == 0o600
    assert tls_dir.joinpath("privkey.pem").stat().st_mode & 0o777 == 0o444
    assert tls_dir.joinpath("fullchain.pem").stat().st_mode & 0o777 == 0o444
    assert control_secret.stat().st_mode & 0o777 == 0o444

    overlay = driver.GATE_COMPOSE.read_text(encoding="utf-8")
    assert (
        "${AURIS_PRODUCTION_GATE_TLS_DIR:?set AURIS_PRODUCTION_GATE_TLS_DIR}:"
        "/run/auris-gate-tls:ro" not in overlay
    )
    assert "}/fullchain.pem:/run/auris-gate-tls/fullchain.pem:ro" in overlay
    assert "}/privkey.pem:/run/auris-gate-tls/privkey.pem:ro" in overlay


def test_runtime_service_observation_binds_health_platform_and_registry_digest() -> None:
    driver = _load_driver()
    container = {
        "Id": "container-redis",
        "Image": "sha256:" + "1" * 64,
        "Config": {"Image": "redis:7.4.2-alpine3.21"},
        "State": {"Status": "running", "Health": {"Status": "healthy"}},
    }
    image_document = {
        "Id": "sha256:" + "1" * 64,
        "Os": "linux",
        "Architecture": "arm64",
        "RepoDigests": ["redis@sha256:" + "2" * 64],
    }

    observation = driver.build_runtime_service_observation(
        "redis",
        container,
        image_document,
        expected_state="running",
        require_repo_digest=True,
    )

    assert observation == {
        "container_id_sha256": driver._sha256_bytes(b"container-redis"),
        "configured_image": "redis:7.4.2-alpine3.21",
        "image_id": "sha256:" + "1" * 64,
        "repo_digests": ["redis@sha256:" + "2" * 64],
        "os": "linux",
        "architecture": "arm64",
        "state": "running",
        "health": "healthy",
    }

    unhealthy = json.loads(json.dumps(container))
    unhealthy["State"]["Health"]["Status"] = "unhealthy"
    with pytest.raises(driver.RuntimeGateError):
        driver.build_runtime_service_observation(
            "redis",
            unhealthy,
            image_document,
            expected_state="running",
            require_repo_digest=True,
        )

    with pytest.raises(driver.RuntimeGateError):
        driver.build_runtime_service_observation(
            "redis",
            container,
            {**image_document, "RepoDigests": []},
            expected_state="running",
            require_repo_digest=True,
        )

    with pytest.raises(driver.RuntimeGateError):
        driver.build_runtime_service_observation(
            "redis",
            container,
            {**image_document, "RepoDigests": ["attacker/redis@sha256:" + "2" * 64]},
            expected_state="running",
            require_repo_digest=True,
        )


def test_completed_service_observation_requires_exit_zero() -> None:
    driver = _load_driver()
    container = {
        "Id": "container-migrate",
        "Image": "sha256:" + "3" * 64,
        "Config": {"Image": "auris-flow-production-gate-bff:aaaaaaaaaaaa"},
        "State": {"Status": "exited", "ExitCode": 0},
    }
    image_document = {
        "Id": "sha256:" + "3" * 64,
        "Os": "linux",
        "Architecture": "amd64",
        "RepoDigests": [],
    }

    observation = driver.build_runtime_service_observation(
        "migrate",
        container,
        image_document,
        expected_state="completed",
        require_repo_digest=False,
    )
    assert observation["state"] == "exited"
    assert observation["exit_code"] == 0

    failed = json.loads(json.dumps(container))
    failed["State"]["ExitCode"] = 1
    with pytest.raises(driver.RuntimeGateError):
        driver.build_runtime_service_observation(
            "migrate",
            failed,
            image_document,
            expected_state="completed",
            require_repo_digest=False,
        )


def test_compose_runtime_uses_the_checked_in_empty_env_file(tmp_path: Path) -> None:
    driver = _load_driver()
    runtime = driver.ComposeRuntime(
        root=tmp_path,
        base_compose=tmp_path / "production" / "compose.yaml",
        gate_compose=tmp_path / "production" / "tests" / "gate.yaml",
        project_name="auris-production-path-gate-1720000000-1234-a1b2c3d4",
        env={"PATH": "/trusted/bin"},
        command_timeout=900,
        wait_timeout=600,
    )

    env_index = runtime.command.index("--env-file")
    assert runtime.command[env_index + 1] == str(
        tmp_path / "production" / "tests" / "production-path-gate.env"
    )
