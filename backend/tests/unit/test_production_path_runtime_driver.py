from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from datetime import UTC, datetime, timedelta
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


def _load_release_gate() -> ModuleType:
    path = ROOT / "scripts" / "verify_production_path_gate.py"
    spec = importlib.util.spec_from_file_location("release_production_path_gate", path)
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


def _resume_environment(
    driver: ModuleType,
    temp_root: Path,
    *,
    source_commit: str,
    suffix: str,
) -> dict[str, str]:
    values = {
        "APP_ENV": "prod",
        "AURIS_PUBLIC_HOST": "auris-production-gate.invalid",
        "AURIS_EXTERNAL_CALLBACK_URL": (
            "https://callback.production-gate.invalid:8443/callbacks/platform"
        ),
        "AURIS_EXTERNAL_CALLBACK_HOST": "callback.production-gate.invalid",
        "AURIS_EMBEDDING_ENDPOINT": (
            "https://embedding.production-gate.invalid:8443/v1/embeddings"
        ),
        "AURIS_EMBEDDING_MODEL": "auris-production-gate-reference-semantic-v1",
        "AURIS_EMBEDDING_DIMENSION": "8",
        "AURIS_AUDIO_INFERENCE_PROVIDER": "audio_intelligence_default",
        "AURIS_AUDIO_INFERENCE_ALLOWED_MODELS": "audio-v2.3.1",
        "AURIS_AUDIO_INFERENCE_ENDPOINT": (
            "https://audio-inference.production-gate.invalid:8443/v1/audio-intelligence"
        ),
        "AURIS_OTEL_TRACE_SAMPLE_RATIO": "1",
        "AURIS_SECRETS_DIR": str(temp_root / "secrets"),
        "AURIS_TLS_DIR": str(temp_root / "tls"),
        "AURIS_RUNTIME_METRICS_DIR": str(temp_root / "runtime-metrics"),
        "AURIS_PRODUCTION_GATE_TLS_DIR": str(temp_root / "tls"),
        "AURIS_PRODUCTION_GATE_ARTIFACT_DIR": str(temp_root / "artifacts"),
        "AURIS_PRODUCTION_GATE_SOURCE_COMMIT": source_commit,
        "AURIS_PRODUCTION_GATE_DEPENDENCY": "none",
        "AURIS_PRODUCTION_GATE_PHASE": "initial",
        "AURIS_HTTP_PORT": "127.0.0.1:18080",
        "AURIS_HTTPS_PORT": "127.0.0.1:18443",
        "AURIS_KEYCLOAK_ADMIN_PORT": "127.0.0.1:18081",
        "AURIS_GRAFANA_PORT": "127.0.0.1:18082",
        "AURIS_PRODUCTION_GATE_CONTROL_SECRET": str(temp_root / "control-secret"),
        "AURIS_PRODUCTION_GATE_RUN_SUFFIX": suffix,
        "AURIS_BFF_IMAGE": f"auris-flow-production-gate-bff:{source_commit[:12]}",
        "AURIS_DAGSTER_IMAGE": (f"auris-flow-production-gate-dagster:{source_commit[:12]}"),
        "AURIS_EDGE_IMAGE": f"auris-flow-production-gate-edge:{source_commit[:12]}",
        **driver.PINNED_EXTERNAL_IMAGES,
    }
    assert set(values) == set(driver.RESUMABLE_RUNTIME_ENV_KEYS)
    return values


def _release_runtime_fixture(
    driver: ModuleType,
    root: Path,
) -> tuple[Path, Path, bytes, list[str], dict[str, object]]:
    release_tag = "v1.0.0-rc.1"
    source_commit = "a" * 40
    gate_aliases = {
        "production-gate-callback": "bff",
        "production-gate-embedding": "bff",
        "production-path-seed": "bff",
        "production-path-verifier": "bff",
    }
    observed_services = set(driver.REQUIRED_RUNNING_SERVICES) | set(
        driver.REQUIRED_COMPLETED_SERVICES
    )
    locked_services = observed_services - set(gate_aliases)
    images = {
        service: (
            f"registry.example/auris/{service}:{release_tag}@sha256:"
            + hashlib.sha256(f"manifest:{service}".encode()).hexdigest()
        )
        for service in sorted(locked_services)
    }
    release_compose = root / "production" / "compose.release.json"
    gate_compose = root / "production" / "tests" / "production-path-gate.compose.yaml"
    image_lock = root / "build" / "release" / "images.lock.json"
    release_compose.parent.mkdir(parents=True, exist_ok=True)
    gate_compose.parent.mkdir(parents=True, exist_ok=True)
    image_lock.parent.mkdir(parents=True, exist_ok=True)
    release_compose.write_text(
        json.dumps(
            {
                "services": {
                    service: {"image": reference} for service, reference in images.items()
                },
                "x-auris-release": {
                    "schema_version": "auris.release-image-lock.v1",
                    "release_tag": release_tag,
                    "source_commit": source_commit,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    gate_compose.write_text("services: {}\n", encoding="utf-8")
    image_lock.write_text(
        json.dumps(
            {
                "schema_version": "auris.release-image-lock.v1",
                "release_tag": release_tag,
                "source_commit": source_commit,
                "images": images,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    for relative in driver.RUNTIME_SOURCE_PATHS:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"source:{relative}\n", encoding="utf-8")

    rendered_images = {
        **images,
        **{service: images[locked_service] for service, locked_service in gate_aliases.items()},
    }
    rendered = json.dumps(
        {
            "services": {
                service: {"image": reference} for service, reference in rendered_images.items()
            }
        },
        sort_keys=True,
    ).encode()

    def observation(service: str, *, completed: bool) -> dict[str, object]:
        locked_service = gate_aliases.get(service, service)
        configured_image = images[locked_service]
        repository, digest = configured_image.rsplit("@", 1)
        repository = repository.rsplit(":", 1)[0]
        return {
            "container_id_sha256": hashlib.sha256(service.encode()).hexdigest(),
            "configured_image": configured_image,
            "image_id": "sha256:" + hashlib.sha256(f"image:{service}".encode()).hexdigest(),
            "repo_digests": [f"{repository}@{digest}"],
            "os": "linux",
            "architecture": "amd64",
            "state": "exited" if completed else "running",
            **({"exit_code": 0} if completed else {"health": "healthy"}),
        }

    inventory: dict[str, object] = {
        "running_services": {
            service: observation(service, completed=False)
            for service in driver.REQUIRED_RUNNING_SERVICES
        },
        "completed_services": {
            service: observation(service, completed=True)
            for service in driver.REQUIRED_COMPLETED_SERVICES
        },
    }
    return (
        release_compose,
        image_lock,
        rendered,
        sorted(rendered_images),
        inventory,
    )


def test_fault_plan_covers_each_required_recovery_case_once() -> None:
    driver = _load_driver()

    plan = driver.fault_plan()

    assert [item.name for item in plan] == [
        "mysql_restart",
        "worker_crash",
        "duplicate_delivery",
        "callback_timeout",
        "dead_letter_retry",
        "qdrant_outage",
        "redis_outage",
    ]
    assert plan[0].host_action == "restart"
    assert plan[1].host_action == "kill-start"
    assert plan[2].service == "worker"
    assert plan[2].host_action == "stop-start"
    assert plan[4].service == "worker"
    assert plan[4].host_action == "stop-start"
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


def test_release_evidence_cross_binds_lock_tag_compose_and_runtime_digests(
    tmp_path: Path,
) -> None:
    driver = _load_driver()
    release_compose, image_lock, rendered, services, inventory = _release_runtime_fixture(
        driver,
        tmp_path,
    )

    evidence = driver.build_release_evidence(
        root=tmp_path,
        source_commit="a" * 40,
        release_tag="v1.0.0-rc.1",
        base_compose=release_compose,
        gate_compose=tmp_path / "production" / "tests" / "production-path-gate.compose.yaml",
        image_lock=image_lock,
        rendered_config=rendered,
        services=services,
        host_runtime=_host_runtime(),
        runtime_inventory=inventory,
        runtime_payload=_runtime_payload(),
    )

    assert evidence["schema_version"] == "auris.production-path-release-gate.v1"
    assert evidence["source_commit"] == "a" * 40
    assert evidence["release_tag"] == "v1.0.0-rc.1"
    assert evidence["execution_environment"] == "production-compose-prebuilt-release"
    assert evidence["compose"]["base"] == "production/compose.release.json"
    assert evidence["release"]["image_lock"] == "build/release/images.lock.json"
    assert evidence["release"]["image_lock_sha256"] == driver._sha256_file(image_lock)
    assert "scripts/verify_production_path_gate.py" in evidence["runtime_sources"]
    bindings = evidence["release"]["runtime_images"]
    assert set(bindings) == (
        set(driver.REQUIRED_RUNNING_SERVICES) | set(driver.REQUIRED_COMPLETED_SERVICES)
    )
    assert bindings["production-gate-callback"]["lock_service"] == "bff"
    assert bindings["worker"]["configured_image"].endswith(
        "@sha256:" + hashlib.sha256(b"manifest:worker").hexdigest()
    )
    assert bindings["worker"]["repo_digest"].endswith(
        "@sha256:" + hashlib.sha256(b"manifest:worker").hexdigest()
    )


def test_release_runtime_driver_minimal_six_section_payload_fails_semantic_gate(
    tmp_path: Path,
) -> None:
    driver = _load_driver()
    gate = _load_release_gate()
    release_compose, image_lock, rendered, services, inventory = _release_runtime_fixture(
        driver,
        tmp_path,
    )
    evidence = driver.build_release_evidence(
        root=tmp_path,
        source_commit="a" * 40,
        release_tag="v1.0.0-rc.1",
        base_compose=release_compose,
        gate_compose=tmp_path / "production" / "tests" / "production-path-gate.compose.yaml",
        image_lock=image_lock,
        rendered_config=rendered,
        services=services,
        host_runtime=_host_runtime(),
        runtime_inventory=inventory,
        runtime_payload=_runtime_payload(),
    )

    errors = gate.validate_release_evidence(
        evidence,
        root=tmp_path,
        expected_commit="a" * 40,
        expected_release_tag="v1.0.0-rc.1",
        release_compose_path=release_compose,
        image_lock_path=image_lock,
    )

    assert errors
    assert any("identity" in error for error in errors)
    assert any("raw proof" in error for error in errors)


def test_release_evidence_rejects_runtime_image_or_repo_digest_not_in_lock(
    tmp_path: Path,
) -> None:
    driver = _load_driver()
    release_compose, image_lock, rendered, services, inventory = _release_runtime_fixture(
        driver,
        tmp_path,
    )
    running = inventory["running_services"]
    assert isinstance(running, dict)
    worker = running["worker"]
    assert isinstance(worker, dict)
    worker["configured_image"] = "registry.example/auris/worker:v1.0.0-rc.1@sha256:" + "f" * 64

    with pytest.raises(driver.RuntimeGateError, match="configured image does not match"):
        driver.build_release_evidence(
            root=tmp_path,
            source_commit="a" * 40,
            release_tag="v1.0.0-rc.1",
            base_compose=release_compose,
            gate_compose=tmp_path / "production" / "tests" / "production-path-gate.compose.yaml",
            image_lock=image_lock,
            rendered_config=rendered,
            services=services,
            host_runtime=_host_runtime(),
            runtime_inventory=inventory,
            runtime_payload=_runtime_payload(),
        )

    _, _, _, _, inventory = _release_runtime_fixture(driver, tmp_path)
    running = inventory["running_services"]
    assert isinstance(running, dict)
    bff = running["bff"]
    assert isinstance(bff, dict)
    bff["repo_digests"] = ["registry.example/auris/bff@sha256:" + "e" * 64]
    with pytest.raises(driver.RuntimeGateError, match="repository digest does not match"):
        driver.build_release_evidence(
            root=tmp_path,
            source_commit="a" * 40,
            release_tag="v1.0.0-rc.1",
            base_compose=release_compose,
            gate_compose=tmp_path / "production" / "tests" / "production-path-gate.compose.yaml",
            image_lock=image_lock,
            rendered_config=rendered,
            services=services,
            host_runtime=_host_runtime(),
            runtime_inventory=inventory,
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
        prebuilt_release=False,
        image_lock=None,
        release_tag=None,
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
        "ALERTMANAGER_IMAGE": "attacker/alertmanager:latest",
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
        "ALERTMANAGER_IMAGE": "prom/alertmanager:v0.28.1",
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


def test_runtime_environment_writes_alertmanager_webhook_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    ports = iter((18443, 18080, 18081, 18082))

    monkeypatch.setattr(driver, "_free_loopback_port", lambda: next(ports))
    monkeypatch.setattr(driver, "_generate_tls", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        driver,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=b"", stderr=b""),
    )

    driver._initialize_runtime_environment(tmp_path, source_commit="a" * 40)

    secret = tmp_path / "secrets" / "alertmanager_webhook_url"
    assert secret.read_text(encoding="ascii") == (
        "https://callback.production-gate.invalid:8443/alerts\n"
    )
    assert secret.stat().st_mode & 0o777 == 0o444


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


def test_prebuilt_release_runtime_pulls_and_never_builds_or_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    runtime = driver.ComposeRuntime(
        root=tmp_path,
        base_compose=tmp_path / "production" / "compose.release.json",
        gate_compose=tmp_path / "production" / "tests" / "gate.yaml",
        project_name="auris-production-path-gate-1720000000-1234-a1b2c3d4",
        env={"PATH": "/trusted/bin"},
        command_timeout=900,
        wait_timeout=600,
        prebuilt_release=True,
    )
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_run(*arguments: str, **kwargs: object) -> None:
        calls.append((arguments, kwargs))

    monkeypatch.setattr(runtime, "run", fake_run)

    runtime.start()
    runtime.wait_service("redis")
    runtime.verifier_phase("initial", "none", "release-123")

    arguments = [call[0] for call in calls]
    assert arguments[0] == ("pull", "--policy", "always")
    assert arguments[1][:5] == ("up", "--detach", "--no-build", "--pull", "never")
    assert "--build" not in arguments[1]
    assert "alertmanager" in arguments[1]
    assert arguments[2][:6] == (
        "up",
        "--detach",
        "--no-build",
        "--pull",
        "never",
        "--no-deps",
    )
    assert arguments[3][:5] == ("run", "--rm", "--pull", "never", "--no-deps")


def test_verifier_failure_preserves_runtime_and_next_process_resumes_exact_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    root = tmp_path / "repo"
    build = root / "build"
    temp_parent = build / "tmp"
    evidence_dir = build / "release-evidence"
    base = root / "production" / "compose.yaml"
    overlay = root / "production" / "tests" / "production-path-gate.compose.yaml"
    artifact = evidence_dir / "production-path-gate.json"
    overlay.parent.mkdir(parents=True)
    evidence_dir.mkdir(parents=True)
    temp_parent.mkdir(parents=True)
    base.write_text("services: {}\n", encoding="utf-8")
    overlay.write_text("services: {}\n", encoding="utf-8")

    monkeypatch.setattr(driver, "ROOT", root)
    monkeypatch.setattr(driver, "BUILD_DIR", build)
    monkeypatch.setattr(driver, "TEMP_PARENT", temp_parent)
    monkeypatch.setattr(driver, "BASE_COMPOSE", base)
    monkeypatch.setattr(driver, "GATE_COMPOSE", overlay)
    monkeypatch.setattr(driver, "EVIDENCE_PATH", artifact)
    monkeypatch.setattr(driver, "_require_clean_source", lambda *_args: None)
    monkeypatch.setattr(
        driver,
        "collect_native_linux_host_observation",
        lambda _env: _host_runtime(),
    )
    monkeypatch.setattr(
        driver,
        "fault_plan",
        lambda: (driver.FaultStep("dead_letter_retry", "worker", "stop-start"),),
    )
    monkeypatch.setattr(driver, "_load_runtime_payload", lambda _path: _runtime_payload())
    monkeypatch.setattr(
        driver,
        "build_evidence",
        lambda **_kwargs: {"status": "ok", "schema_version": "test"},
    )

    initialized: list[Path] = []

    def fake_initialize(temp_root: Path, *, source_commit: str) -> dict[str, str]:
        initialized.append(temp_root)
        (temp_root / "artifacts").mkdir()
        environment = _resume_environment(
            driver,
            temp_root,
            source_commit=source_commit,
            suffix="placeholder",
        )
        for key in (
            "AURIS_PRODUCTION_GATE_RUN_SUFFIX",
            "AURIS_BFF_IMAGE",
            "AURIS_DAGSTER_IMAGE",
            "AURIS_EDGE_IMAGE",
        ):
            environment.pop(key)
        return {"PATH": "/trusted/bin", **environment}

    monkeypatch.setattr(driver, "_initialize_runtime_environment", fake_initialize)

    class FakeRuntime:
        verifier_calls: list[tuple[str, str, str]] = []
        starts = 0
        downs = 0
        constructors: list[tuple[str, dict[str, str]]] = []
        failures_remaining = 1

        def __init__(self, **kwargs: object) -> None:
            self.command_timeout = int(kwargs["command_timeout"])
            self.prebuilt_release = bool(kwargs["prebuilt_release"])
            FakeRuntime.constructors.append(
                (str(kwargs["project_name"]), dict(kwargs["env"]))  # type: ignore[arg-type]
            )

        def render(self) -> bytes:
            return b'{"services":{"bff":{}}}'

        def start(self) -> None:
            FakeRuntime.starts += 1

        def runtime_inventory(self) -> dict[str, dict[str, object]]:
            return {"running_services": {}, "completed_services": {}}

        def verifier_phase(self, phase: str, dependency: str, suffix: str) -> None:
            FakeRuntime.verifier_calls.append((phase, dependency, suffix))
            if (
                phase == "fault-verify"
                and dependency == "dead_letter_retry"
                and FakeRuntime.failures_remaining > 0
            ):
                FakeRuntime.failures_remaining -= 1
                raise driver.VerifierPhaseError(phase, dependency, "injected verifier crash")

        def run(self, *_args: str, **_kwargs: object) -> None:
            return None

        def wait_service(self, _service: str) -> None:
            return None

        def service_observation(self, _service: str) -> dict[str, str]:
            return {"container_id": "container", "started_at": "2026-01-01T00:00:00Z"}

        def down(self) -> None:
            FakeRuntime.downs += 1

    monkeypatch.setattr(driver, "ComposeRuntime", FakeRuntime)
    source_commit = "a" * 40
    kwargs = {
        "base_compose": base,
        "gate_compose": overlay,
        "source_commit": source_commit,
        "artifact": artifact,
        "command_timeout": 900,
        "wait_timeout": 600,
    }

    with pytest.raises(driver.RuntimeGateError, match="injected verifier crash"):
        driver.run_gate(**kwargs)

    resume_path = driver.runtime_resume_path(source_commit=source_commit, prebuilt_release=False)
    assert resume_path.is_file()
    assert len(initialized) == 1
    assert initialized[0].is_dir()
    assert FakeRuntime.starts == 1
    assert FakeRuntime.downs == 0

    driver.run_gate(**kwargs)

    assert artifact.is_file()
    assert not resume_path.exists()
    assert not initialized[0].exists()
    assert len(initialized) == 1
    assert FakeRuntime.starts == 1
    assert FakeRuntime.downs == 1
    assert len({project for project, _env in FakeRuntime.constructors}) == 1
    first_suffix = FakeRuntime.verifier_calls[0][2]
    assert FakeRuntime.verifier_calls.count(("initial", "none", first_suffix)) == 1
    assert (
        sum(
            phase == "fault-verify" and dependency == "dead_letter_retry"
            for phase, dependency, _suffix in FakeRuntime.verifier_calls
        )
        == 2
    )

    # A new diagnostic may preserve its first verifier crash once. If the exact
    # resumed phase crashes again, the driver exhausts that allowance and tears
    # down both the Compose project and its private checkpoint instead of
    # creating an indefinitely reusable recovery state.
    artifact.unlink()
    initialized.clear()
    FakeRuntime.verifier_calls.clear()
    FakeRuntime.starts = 0
    FakeRuntime.downs = 0
    FakeRuntime.constructors.clear()
    FakeRuntime.failures_remaining = 2

    with pytest.raises(driver.RuntimeGateError, match="injected verifier crash"):
        driver.run_gate(**kwargs)

    second_temp_root = initialized[0]
    assert resume_path.is_file()
    assert second_temp_root.is_dir()
    assert FakeRuntime.downs == 0

    with pytest.raises(driver.RuntimeGateError, match="injected verifier crash"):
        driver.run_gate(**kwargs)

    assert not artifact.exists()
    assert not resume_path.exists()
    assert not second_temp_root.exists()
    assert FakeRuntime.downs == 1

    # Even when load rejects a stale state before entering the main execution
    # try/finally, the driver must use its already validated project metadata to
    # tear down that one project and remove only its own state.
    for terminal_reason in ("expired", "exhausted"):
        terminal_temp_root = temp_parent / f"auris-production-path-gate.{terminal_reason}1"
        terminal_temp_root.mkdir()
        terminal_checkpoint = driver.build_runtime_resume_checkpoint(
            source_commit=source_commit,
            prebuilt_release=False,
            release_tag=None,
            temp_root=terminal_temp_root,
            project_name="auris-production-path-gate-1720000000-1234-a1b2c3d4",
            run_suffix=f"{terminal_reason}-resume",
            environment=_resume_environment(
                driver,
                terminal_temp_root,
                source_commit=source_commit,
                suffix=f"{terminal_reason}-resume",
            ),
            base_compose_sha256=driver._sha256_file(base),
            gate_compose_sha256=driver._sha256_file(overlay),
            now=(
                datetime.now(UTC) - timedelta(seconds=driver.RUNTIME_RESUME_TTL_SECONDS + 60)
                if terminal_reason == "expired"
                else datetime.now(UTC)
            ),
        )
        expected_error = driver.RuntimeResumeExpiredError
        expected_message = "expired"
        if terminal_reason == "exhausted":
            terminal_checkpoint["resume_attempt_count"] = 1
            expected_error = driver.RuntimeResumeExhaustedError
            expected_message = "retry limit"
        driver.write_runtime_resume_checkpoint(resume_path, terminal_checkpoint, prior=None)
        FakeRuntime.downs = 0

        with pytest.raises(expected_error, match=expected_message):
            driver.run_gate(**kwargs)

        assert FakeRuntime.downs == 1
        assert not terminal_temp_root.exists()
        assert not resume_path.exists()

    # Host-side operations cannot be replayed safely after a process crash.
    # A checkpoint left in that ambiguous state is terminal and must be torn
    # down on the first recovery attempt, before any new host action runs.
    ambiguous_temp_root = temp_parent / "auris-production-path-gate.ambiguous1"
    ambiguous_temp_root.mkdir()
    ambiguous_checkpoint = driver.build_runtime_resume_checkpoint(
        source_commit=source_commit,
        prebuilt_release=False,
        release_tag=None,
        temp_root=ambiguous_temp_root,
        project_name="auris-production-path-gate-1720000000-1234-a1b2c3d4",
        run_suffix="ambiguous-resume",
        environment=_resume_environment(
            driver,
            ambiguous_temp_root,
            source_commit=source_commit,
            suffix="ambiguous-resume",
        ),
        base_compose_sha256=driver._sha256_file(base),
        gate_compose_sha256=driver._sha256_file(overlay),
    )
    ambiguous_checkpoint["operation_attempts"] = {"compose:start": 1}
    ambiguous_checkpoint["inflight_operation"] = "compose:start"
    driver.write_runtime_resume_checkpoint(
        resume_path,
        ambiguous_checkpoint,
        prior=None,
    )
    FakeRuntime.downs = 0

    with pytest.raises(driver.RuntimeGateError, match="ambiguous host operation"):
        driver.run_gate(**kwargs)

    assert FakeRuntime.downs == 1
    assert not ambiguous_temp_root.exists()
    assert not resume_path.exists()


def test_runtime_resume_checkpoint_rejects_identity_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    temp_parent = tmp_path / "tmp"
    temp_parent.mkdir()
    monkeypatch.setattr(driver, "TEMP_PARENT", temp_parent)
    source_commit = "b" * 40
    path = driver.runtime_resume_path(source_commit=source_commit, prebuilt_release=False)
    temp_root = temp_parent / "auris-production-path-gate.abcdef"
    temp_root.mkdir()
    checkpoint = driver.build_runtime_resume_checkpoint(
        source_commit=source_commit,
        prebuilt_release=False,
        release_tag=None,
        temp_root=temp_root,
        project_name="auris-production-path-gate-1720000000-1234-a1b2c3d4",
        run_suffix="resume-123",
        environment=_resume_environment(
            driver, temp_root, source_commit=source_commit, suffix="resume-123"
        ),
        base_compose_sha256="c" * 64,
        gate_compose_sha256="d" * 64,
    )
    driver.write_runtime_resume_checkpoint(path, checkpoint, prior=None)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["source_commit"] = "e" * 40
    path.write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(driver.RuntimeGateError, match="resume checkpoint"):
        driver.load_runtime_resume_checkpoint(
            path,
            source_commit=source_commit,
            prebuilt_release=False,
            release_tag=None,
            base_compose_sha256="c" * 64,
            gate_compose_sha256="d" * 64,
        )


def test_runtime_resume_allows_only_one_cross_process_verifier_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    temp_parent = tmp_path / "tmp"
    temp_parent.mkdir()
    monkeypatch.setattr(driver, "TEMP_PARENT", temp_parent)
    source_commit = "c" * 40
    path = driver.runtime_resume_path(source_commit=source_commit, prebuilt_release=False)
    temp_root = temp_parent / "auris-production-path-gate.ghijkl"
    temp_root.mkdir()
    checkpoint = driver.build_runtime_resume_checkpoint(
        source_commit=source_commit,
        prebuilt_release=False,
        release_tag=None,
        temp_root=temp_root,
        project_name="auris-production-path-gate-1720000000-1234-a1b2c3d4",
        run_suffix="resume-456",
        environment=_resume_environment(
            driver, temp_root, source_commit=source_commit, suffix="resume-456"
        ),
        base_compose_sha256="d" * 64,
        gate_compose_sha256="e" * 64,
    )
    driver.write_runtime_resume_checkpoint(path, checkpoint, prior=None)
    operation = "verifier:fault-verify:dead_letter_retry"

    first = driver._transition_runtime_resume(path, checkpoint, operation=operation, complete=False)
    second = driver._transition_runtime_resume(path, first, operation=operation, complete=False)

    assert first["operation_attempts"] == {operation: 1}
    assert second["operation_attempts"] == {operation: 2}
    with pytest.raises(driver.RuntimeResumeExhaustedError, match="retry limit"):
        driver._transition_runtime_resume(path, second, operation=operation, complete=False)
    assert json.loads(path.read_text(encoding="utf-8")) == second

    assert driver.preserve_after_verifier_failure(checkpoint) is True
    resumed = driver.advance_runtime_resume_checkpoint(path, second)
    assert resumed["resume_attempt_count"] == 1
    assert driver.preserve_after_verifier_failure(resumed) is False
    with pytest.raises(driver.RuntimeResumeExhaustedError, match="retry limit"):
        driver.advance_runtime_resume_checkpoint(path, resumed)


def test_runtime_resume_checkpoint_excludes_host_env_and_rejects_extra_or_expired_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    temp_parent = tmp_path / "tmp"
    temp_parent.mkdir()
    monkeypatch.setattr(driver, "TEMP_PARENT", temp_parent)
    source_commit = "d" * 40
    path = driver.runtime_resume_path(source_commit=source_commit, prebuilt_release=False)
    temp_root = temp_parent / "auris-production-path-gate.mnopqr"
    temp_root.mkdir()
    created_at = datetime(2026, 7, 21, 1, 0, tzinfo=UTC)
    environment = _resume_environment(
        driver, temp_root, source_commit=source_commit, suffix="resume-789"
    )
    checkpoint = driver.build_runtime_resume_checkpoint(
        source_commit=source_commit,
        prebuilt_release=False,
        release_tag=None,
        temp_root=temp_root,
        project_name="auris-production-path-gate-1720000000-1234-a1b2c3d4",
        run_suffix="resume-789",
        environment=environment,
        base_compose_sha256="e" * 64,
        gate_compose_sha256="f" * 64,
        now=created_at,
    )

    assert set(checkpoint["environment"]) == set(driver.RESUMABLE_RUNTIME_ENV_KEYS)
    assert not {"PATH", "HOME", "DOCKER_CONFIG"} & set(checkpoint["environment"])
    assert "AURIS_AUDIO_INFERENCE_API_TOKEN" not in checkpoint["environment"]
    assert "raw-secret-canary" not in json.dumps(checkpoint)
    with pytest.raises(driver.RuntimeGateError, match="environment"):
        driver.build_runtime_resume_checkpoint(
            source_commit=source_commit,
            prebuilt_release=False,
            release_tag=None,
            temp_root=temp_root,
            project_name="auris-production-path-gate-1720000000-1234-a1b2c3d4",
            run_suffix="resume-789",
            environment={**environment, "UNSAFE_SECRET": "raw-secret-canary"},
            base_compose_sha256="e" * 64,
            gate_compose_sha256="f" * 64,
            now=created_at,
        )

    driver.write_runtime_resume_checkpoint(path, checkpoint, prior=None)
    assert path.stat().st_mode & 0o777 == 0o600
    expired = created_at + timedelta(seconds=driver.RUNTIME_RESUME_TTL_SECONDS + 1)
    with pytest.raises(driver.RuntimeGateError, match="expired"):
        driver.load_runtime_resume_checkpoint(
            path,
            source_commit=source_commit,
            prebuilt_release=False,
            release_tag=None,
            base_compose_sha256="e" * 64,
            gate_compose_sha256="f" * 64,
            now=expired,
        )
