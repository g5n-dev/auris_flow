from __future__ import annotations

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
