from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_dagster_storage_uses_mysql_env_reference_without_inline_secret() -> None:
    payload = yaml.safe_load((ROOT / "dagster.yaml").read_text(encoding="utf-8"))
    for name in ("run_storage", "event_log_storage", "schedule_storage"):
        assert payload[name]["config"]["mysql_url"] == {"env": "DAGSTER_MYSQL_URL"}
    serialized = (ROOT / "dagster.yaml").read_text(encoding="utf-8").lower()
    assert "password" not in serialized
    assert "mysql://" not in serialized


def test_workspace_points_at_compose_grpc_code_location() -> None:
    payload = yaml.safe_load((ROOT / "workspace.yaml").read_text(encoding="utf-8"))
    grpc = payload["load_from"][0]["grpc_server"]
    assert grpc == {
        "host": "dagster-code",
        "port": 4000,
        "location_name": "auris_flow_defs",
    }


def test_entrypoint_is_valid_shell_and_does_not_enable_trace_or_echo_secret() -> None:
    entrypoint = ROOT / "dagster-entrypoint.sh"
    subprocess.run(["/bin/sh", "-n", str(entrypoint)], check=True)  # noqa: S603
    source = entrypoint.read_text(encoding="utf-8")
    assert "set -x" not in source
    assert "cat /run/secrets" not in source
    assert 'echo "$secret_value"' not in source
    assert "webserver)" in source
    assert "daemon)" in source
    assert "health)" in source
    assert "dagster-daemon liveness-check" in source


def test_entrypoint_has_fail_closed_storage_bootstrap_role() -> None:
    source = (ROOT / "dagster-entrypoint.sh").read_text(encoding="utf-8")
    bootstrap = source.split("storage-bootstrap)", 1)[1].split(";;", 1)[0]

    assert 'exec dagster instance migrate "$@"' in bootstrap
    assert "|| true" not in bootstrap
    assert "set +e" not in bootstrap


def test_storage_bootstrap_role_propagates_migration_failure_without_secret_leak(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_file = tmp_path / "dagster-call"
    fake_dagster = fake_bin / "dagster"
    fake_dagster.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >"$AURIS_DAGSTER_CALL_FILE"\n'
        'exit "${AURIS_FAKE_DAGSTER_EXIT_CODE:-0}"\n',
        encoding="utf-8",
    )
    fake_dagster.chmod(0o755)
    database_url = "mysql+pymysql://dagster:private-value@mysql:3306/dagster"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "DAGSTER_MYSQL_URL": database_url,
        "AURIS_DAGSTER_CALL_FILE": str(call_file),
        "AURIS_FAKE_DAGSTER_EXIT_CODE": "73",
    }

    completed = subprocess.run(  # noqa: S603 - argv and executable are fixed test fixtures.
        ["/bin/sh", str(ROOT / "dagster-entrypoint.sh"), "storage-bootstrap"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 73
    assert call_file.read_text(encoding="utf-8") == "instance migrate\n"
    assert database_url not in completed.stdout
    assert database_url not in completed.stderr


def test_dockerfile_is_pinned_multistage_and_non_root() -> None:
    source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert source.count("FROM ") == 2
    assert "python:3.12.10-slim-bookworm" in source
    assert "uv sync --frozen" in source
    assert "uv sync --frozen --no-dev --no-editable" in source
    assert "USER 10001:10001" in source
    assert ":latest" not in source


def test_runtime_dependencies_match_compose_mysql_url_driver() -> None:
    source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"pymysql==1.1.1"' in source
    assert '"cryptography==49.0.0"' in source


def test_compose_serializes_dagster_storage_bootstrap_with_least_privilege() -> None:
    compose = yaml.safe_load((ROOT.parent / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    bootstrap = services["dagster-storage-bootstrap"]

    assert bootstrap["command"] == ["storage-bootstrap"]
    assert bootstrap["restart"] == "no"
    assert bootstrap["read_only"] is True
    assert bootstrap["cap_drop"] == ["ALL"]
    assert bootstrap["security_opt"] == ["no-new-privileges:true"]
    assert bootstrap["environment"] == {"DAGSTER_HOME": "/opt/dagster/home"}
    assert bootstrap["secrets"] == ["dagster_database_url"]
    assert bootstrap.get("volumes", []) == []
    assert set(bootstrap["networks"]) == {"internal"}
    assert bootstrap["depends_on"] == {
        "db-bootstrap": {"condition": "service_completed_successfully"}
    }

    for service_name in ("dagster-code", "dagster-webserver", "dagster-daemon"):
        assert services[service_name]["depends_on"]["dagster-storage-bootstrap"] == {
            "condition": "service_completed_successfully"
        }

    database_url_consumers = {
        name
        for name, service in services.items()
        if "dagster_database_url" in service.get("secrets", [])
    }
    assert database_url_consumers == {
        "dagster-storage-bootstrap",
        "dagster-code",
        "dagster-webserver",
        "dagster-daemon",
    }


def test_compose_gives_only_code_location_exact_version_audio_read_credentials() -> None:
    compose = yaml.safe_load((ROOT.parent / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    code = services["dagster-code"]

    assert (
        code["environment"]
        | {
            "AURIS_AUDIO_OBJECT_STORAGE_PROVIDER": "minio",
            "AURIS_AUDIO_OBJECT_STORAGE_ENDPOINT": "http://minio:9000",
            "AURIS_AUDIO_OBJECT_STORAGE_REGION": "us-east-1",
            "AURIS_AUDIO_OBJECT_STORAGE_ALLOWED_BUCKETS": "auris-flow",
            "AURIS_AUDIO_OBJECT_STORAGE_ACCESS_KEY_FILE": (
                "/run/secrets/object_storage_access_key"
            ),
            "AURIS_AUDIO_OBJECT_STORAGE_SECRET_KEY_FILE": (
                "/run/secrets/object_storage_secret_key"
            ),
        }
        == code["environment"]
    )
    assert {"object_storage_access_key", "object_storage_secret_key"}.issubset(set(code["secrets"]))
    for service_name in ("dagster-webserver", "dagster-daemon"):
        assert "object_storage_access_key" not in services[service_name].get("secrets", [])
        assert not any(
            name.startswith("AURIS_AUDIO_OBJECT_STORAGE_")
            for name in services[service_name].get("environment", {})
        )


def test_compose_wires_https_audio_provider_and_secret_only_to_code_location() -> None:
    compose = yaml.safe_load((ROOT.parent / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    code = services["dagster-code"]
    environment = code["environment"]

    assert environment["AURIS_AUDIO_INFERENCE_PROVIDER"] == (
        "${AURIS_AUDIO_INFERENCE_PROVIDER:?set AURIS_AUDIO_INFERENCE_PROVIDER}"
    )
    assert environment["AURIS_AUDIO_INFERENCE_ALLOWED_MODELS"] == (
        "${AURIS_AUDIO_INFERENCE_ALLOWED_MODELS:?set AURIS_AUDIO_INFERENCE_ALLOWED_MODELS}"
    )
    assert environment["AURIS_AUDIO_INFERENCE_ENDPOINT"] == (
        "${AURIS_AUDIO_INFERENCE_ENDPOINT:?set AURIS_AUDIO_INFERENCE_ENDPOINT}"
    )
    assert environment["AURIS_AUDIO_INFERENCE_API_TOKEN_FILE"] == (
        "/run/secrets/audio_inference_api_token"  # noqa: S105 - path, not a token
    )
    assert environment["AURIS_AUDIO_INFERENCE_TIMEOUT_SECONDS"] == "30"
    assert environment["AURIS_AUDIO_INFERENCE_MAX_RESPONSE_BYTES"] == "1048576"
    assert environment["AURIS_AUDIO_RESULT_BUCKET"] == "auris-flow"
    assert "audio_inference_api_token" in code["secrets"]
    assert set(code["networks"]) == {"internal", "app-egress"}
    for service_name in ("dagster-webserver", "dagster-daemon", "bff", "worker"):
        service = services[service_name]
        assert "audio_inference_api_token" not in service.get("secrets", [])
        audio_environment = {
            name
            for name in service.get("environment", {})
            if name.startswith("AURIS_AUDIO_INFERENCE_")
        }
        if service_name in {"bff", "worker"}:
            assert audio_environment == {
                "AURIS_AUDIO_INFERENCE_PROVIDER",
                "AURIS_AUDIO_INFERENCE_ALLOWED_MODELS",
            }
        else:
            assert not audio_environment
