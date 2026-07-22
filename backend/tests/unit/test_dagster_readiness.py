from __future__ import annotations

import json
import time
from typing import Any

import pytest

from app.main import probe_dagster_workspace


class _Response:
    def __init__(self, payload: dict[str, Any], *, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self._body[:limit]


def _workspace_payload(*, pipelines: object = None) -> dict[str, Any]:
    configured_pipelines = [{"name": "auris_flow_generic_job"}] if pipelines is None else pipelines
    return {
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
                        "name": "__repository__",
                        "location": {"name": "auris_flow_defs"},
                        "pipelines": configured_pipelines,
                    }
                ],
            },
        }
    }


def test_dagster_readiness_requires_exact_workspace_and_uses_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_urlopen(request, timeout: float) -> _Response:
        observed.update(
            method=request.method,
            url=request.full_url,
            timeout=timeout,
            headers={name.lower(): value for name, value in request.header_items()},
            body=json.loads(request.data.decode("utf-8")),
        )
        return _Response(_workspace_payload())

    monkeypatch.setenv("DAGSTER_GRAPHQL_BEARER_TOKEN", "readiness-token")
    monkeypatch.setattr("app.main.urlopen", fake_urlopen)

    assert probe_dagster_workspace("http://dagster:3000/graphql") == "ok"
    assert observed["method"] == "POST"
    assert observed["url"] == "http://dagster:3000/graphql"
    assert observed["timeout"] == 1.0
    assert observed["headers"] == {
        "authorization": "Bearer readiness-token",
        "content-type": "application/json",
    }
    assert "repositoriesOrError" in observed["body"]["query"]  # type: ignore[index]
    assert "daemonHealth" in observed["body"]["query"]  # type: ignore[index]
    assert "lastHeartbeatTime" in observed["body"]["query"]  # type: ignore[index]


@pytest.mark.parametrize(
    "payload",
    [
        {"errors": [{"message": "workspace unavailable"}]},
        {
            "data": {
                "repositoriesOrError": {
                    "__typename": "PythonError",
                    "message": "internal detail",
                }
            }
        },
        _workspace_payload(pipelines=[]),
        {"data": {}},
    ],
)
def test_dagster_readiness_fails_closed_for_graphql_or_job_drift(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    monkeypatch.setattr("app.main.urlopen", lambda _request, timeout: _Response(payload))

    assert probe_dagster_workspace("http://dagster:3000/graphql") == "not_ready"


@pytest.mark.parametrize(
    "statuses",
    [
        [
            {
                "daemonType": "QUEUED_RUN_COORDINATOR",
                "required": True,
                "healthy": False,
            }
        ],
        [
            {
                "daemonType": "SENSOR",
                "required": False,
                "healthy": None,
            }
        ],
        [],
    ],
)
def test_dagster_readiness_requires_at_least_one_healthy_required_daemon(
    monkeypatch: pytest.MonkeyPatch,
    statuses: list[dict[str, object]],
) -> None:
    payload = _workspace_payload()
    payload["data"]["instance"]["daemonHealth"]["allDaemonStatuses"] = statuses
    monkeypatch.setattr("app.main.urlopen", lambda _request, timeout: _Response(payload))

    assert probe_dagster_workspace("http://dagster:3000/graphql") == "not_ready"


def test_dagster_readiness_handles_null_pipeline_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _workspace_payload()
    payload["data"]["repositoriesOrError"]["nodes"][0]["pipelines"] = None
    monkeypatch.setattr("app.main.urlopen", lambda _request, timeout: _Response(payload))

    assert probe_dagster_workspace("http://dagster:3000/graphql") == "not_ready"


@pytest.mark.parametrize(
    "heartbeat",
    [
        None,
        "not-a-number",
        True,
        float("nan"),
        time.time() - 3600.0,
        time.time() + 3600.0,
    ],
)
def test_dagster_readiness_rejects_missing_invalid_stale_or_future_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    heartbeat: object,
) -> None:
    payload = _workspace_payload()
    daemon = payload["data"]["instance"]["daemonHealth"]["allDaemonStatuses"][0]
    if heartbeat is None:
        daemon.pop("lastHeartbeatTime")
    else:
        daemon["lastHeartbeatTime"] = heartbeat
    monkeypatch.setattr("app.main.urlopen", lambda _request, timeout: _Response(payload))

    assert probe_dagster_workspace("http://dagster:3000/graphql") == "not_ready"


def test_dagster_readiness_rejects_non_200_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.main.urlopen",
        lambda _request, timeout: _Response(_workspace_payload(), status=401),
    )

    assert probe_dagster_workspace("http://dagster:3000/graphql") == "not_ready"
