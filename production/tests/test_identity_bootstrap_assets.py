from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = ROOT / "production"
SUBJECT = "9d1c5cc4-e661-4af6-8a6f-7402d2555c35"


def test_keycloak_reference_user_has_fixed_subject_and_temporary_secret_placeholder() -> (
    None
):
    realm = json.loads(
        (PRODUCTION / "keycloak" / "auris-flow-realm.template.json").read_text(
            encoding="utf-8"
        )
    )

    user = next(item for item in realm["users"] if item["id"] == SUBJECT)
    assert user["username"] == "bootstrap-operator"
    assert user["email"] == "bootstrap-operator@auris.invalid"
    assert user["requiredActions"] == ["UPDATE_PASSWORD"]
    assert user["credentials"] == [
        {
            "type": "password",
            "value": "__AURIS_BOOTSTRAP_OPERATOR_PASSWORD__",
            "temporary": True,
        }
    ]


def test_keycloak_reference_client_registers_standard_backchannel_logout() -> None:
    realm = json.loads(
        (PRODUCTION / "keycloak" / "auris-flow-realm.template.json").read_text(
            encoding="utf-8"
        )
    )

    client = next(
        item for item in realm["clients"] if item["clientId"] == "auris-flow-web"
    )
    assert client["frontchannelLogout"] is False
    assert client["attributes"]["backchannel.logout.url"] == (
        "https://__AURIS_PUBLIC_HOST__/api/v1/auth/oidc/back-channel-logout"
    )
    assert client["attributes"]["backchannel.logout.session.required"] == "true"


def test_keycloak_api_audience_is_never_added_to_id_tokens() -> None:
    for realm_path in (
        PRODUCTION / "keycloak" / "auris-flow-realm.template.json",
        PRODUCTION / "tests" / "production-path-keycloak-realm.template.json",
    ):
        realm = json.loads(realm_path.read_text(encoding="utf-8"))
        client = next(
            item for item in realm["clients"] if item["clientId"] == "auris-flow-web"
        )
        mapper = next(
            item
            for item in client["protocolMappers"]
            if item["name"] == "auris-flow-api-audience"
        )

        assert mapper["config"]["id.token.claim"] == "false"
        assert mapper["config"]["access.token.claim"] == "true"


def test_keycloak_entrypoint_reads_operator_password_from_secret_without_exporting_it() -> (
    None
):
    entrypoint = (PRODUCTION / "keycloak" / "entrypoint.sh").read_text(encoding="utf-8")

    assert "/run/secrets/keycloak_bootstrap_operator_password" in entrypoint
    assert "__AURIS_BOOTSTRAP_OPERATOR_PASSWORD__" in entrypoint
    assert "export KC_BOOTSTRAP_OPERATOR_PASSWORD" not in entrypoint
    assert "/opt/keycloak/data/import" in entrypoint


def test_compose_orders_identity_bootstrap_before_bff() -> None:
    environment = {
        **os.environ,
        "AURIS_PUBLIC_HOST": "auris.example.com",
        "AURIS_EXTERNAL_CALLBACK_URL": "https://platform.example.com/callbacks/auris-flow",
        "AURIS_EXTERNAL_CALLBACK_HOST": "platform.example.com",
    }
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(PRODUCTION / ".env.example"),
            "--file",
            str(PRODUCTION / "compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        cwd=PRODUCTION,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    compose = json.loads(completed.stdout)
    bootstrap = compose["services"]["identity-bootstrap"]

    assert bootstrap["command"] == ["python", "-m", "app.identity_bootstrap"]
    assert bootstrap["environment"]["AURIS_BOOTSTRAP_OIDC_SUBJECT"] == SUBJECT
    assert bootstrap["secrets"] == [
        {
            "source": "runtime_database_url",
            "target": "/run/secrets/runtime_database_url",
        }
    ]
    assert (
        bootstrap["depends_on"]["migrate"]["condition"]
        == "service_completed_successfully"
    )
    assert bootstrap["depends_on"]["keycloak"]["condition"] == "service_healthy"
    assert (
        compose["services"]["bff"]["depends_on"]["identity-bootstrap"]["condition"]
        == "service_completed_successfully"
    )
