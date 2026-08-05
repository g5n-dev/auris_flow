from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.error import HTTPError

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from pydantic import ValidationError

from app.core.audio_playback import create_audio_playback_grant, verify_audio_playback_grant
from app.core.auth import (
    SignedTokenAuthProvider,
    StaticDevAuthProvider,
    get_dev_auth_profile,
    issue_dev_auth_token,
    sign_auth_token,
)
from app.core.config import Settings
from app.core.errors import ApiError
from app.schemas import EvalRunRequest, RunCompletionReceiptRequest, parse_payload
from app.services import adapters as adapter_module
from app.services.adapters import (
    MAX_QDRANT_RESPONSE_BYTES,
    AdapterRegistry,
    RealDagsterClient,
    RealExternalCallbackClient,
    RealObjectStorageClient,
    RealQdrantIndexClient,
    dispatch_event,
)

pytestmark = pytest.mark.usefixtures("allow_inline_production_settings_for_policy_tests")

SECURE_PROD_SETTINGS = {
    "database_url": f"mysql+pymysql://auris:{'M' * 48}@mysql:3306/auris_flow",
    "redis_url": f"redis://:{'R' * 48}@redis:6379/0",
    "auth_provider": "oidc",
    "allow_dev_auth": False,
    "oidc_issuer": "https://identity.example.com/realms/auris",
    "oidc_client_id": "auris-flow-bff",
    "oidc_client_secret": "I" * 48,
    "oidc_audience": "auris-flow-api",
    "oidc_redirect_uri": "https://auris.example.com/api/v1/auth/oidc/callback",
    "browser_session_cookie_name": "__Host-auris_session",
    "audio_playback_grant_secret": "unit-playback-secret-32-characters",
    "completion_receipt_secret": "unit-completion-secret-32-characters",
    "experiment_assignment_secret": "unit-experiment-assignment-secret-32-characters",
    "cors_allowed_origins": "https://auris.example.com",
    "trusted_hosts": "auris.example.com",
    "auris_object_storage_adapter": "real",
    "object_storage_endpoint": "http://minio:9000",
    "object_storage_bucket": "auris-unit",
    "object_storage_access_key": "auris-unit-access",
    "object_storage_secret_key": "O" * 48,
    "auris_qdrant_adapter": "real",
    "qdrant_api_key": "Q" * 48,
    "auris_embedding_provider": "http",
    "embedding_endpoint": "https://embeddings.example.com/v1/embeddings",
    "embedding_model": "multilingual-semantic-v1",
    "embedding_dimension": 1024,
    "embedding_api_key": "G" * 48,
    "auris_dagster_adapter": "real",
    "dagster_graphql_url": "http://dagster:3000/graphql",
    "auris_external_callback_adapter": "real",
    "external_callback_url": "https://callback.example.com/callbacks/platform",
    "external_callback_allowed_hosts": "callback.example.com",
    "external_callback_key_bindings": json.dumps(
        {
            "callback-2026-07": {
                "secret": "callback-production-key-material-2026-07-B!",
                "state": "active",
            }
        }
    ),
    "external_callback_active_key_id": "callback-2026-07",
    "dependency_check_mode": "strict",
    "otel_enabled": True,
    "otel_exporter_otlp_endpoint": "https://telemetry.example.com/v1/traces",
    "metrics_enabled": True,
}


def prod_settings(**overrides):
    values = {**SECURE_PROD_SETTINGS, **overrides}
    return Settings(app_env="prod", **values)


def transitional_signed_settings(**overrides):
    return Settings(app_env="staging", auth_provider="signed", **overrides)


def _signed_raw_token(secret: str, payload: dict[str, object]) -> str:
    payload_part = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    signing_input = f"auris.v1.{payload_part}".encode()
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        )
        .decode("ascii")
        .rstrip("=")
    )
    return f"auris.v1.{payload_part}.{signature}"


def test_completion_result_references_are_normalized_and_typed() -> None:
    receipt = RunCompletionReceiptRequest.model_validate(
        {
            "result_ref": {
                "nested": {
                    "refType": "voiceprint_profile",
                    "refId": "VP-CANONICAL",
                }
            }
        }
    )

    assert receipt.result_ref == {
        "nested": {
            "ref_type": "voiceprint_profile",
            "ref_id": "VP-CANONICAL",
        }
    }
    with pytest.raises(ValidationError, match="require both ref_type and ref_id"):
        RunCompletionReceiptRequest.model_validate(
            {"result_ref": {"refType": "voiceprint_profile"}}
        )
    with pytest.raises(ValidationError, match="duplicate completion reference field"):
        RunCompletionReceiptRequest.model_validate(
            {
                "result_ref": {
                    "refType": "voiceprint_profile",
                    "ref_type": "voiceprint_profile",
                    "refId": "VP-DUPLICATE",
                }
            }
        )


@pytest.mark.parametrize(
    "result_ref",
    [
        {"resourceType": "voiceprint_profile"},
        {"resourceId": "VP-SECRET"},
        {"objectType": "human_review_decision"},
        {"subjectId": "HRD-SECRET"},
        {"aggregateType": "voiceprint_profile", "aggregateId": " "},
        {"RefType": "voiceprint_profile"},
        {"RefId": "VP-SECRET"},
        {"resource_type": "voiceprint_profile", "ref_id": "VP-MIXED"},
    ],
)
def test_completion_result_reference_families_reject_incomplete_or_blank_pairs(
    result_ref: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="require both|non-blank"):
        RunCompletionReceiptRequest.model_validate({"result_ref": result_ref})


def test_completion_result_reference_alias_collisions_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate completion reference field"):
        RunCompletionReceiptRequest.model_validate(
            {
                "result_ref": {
                    "refType": "voiceprint_profile",
                    "RefType": "data_asset",
                    "refId": "VP-SECRET",
                    "RefId": "asset-safe",
                }
            }
        )


@pytest.mark.parametrize(
    ("canonical_type", "canonical_id", "alias_type", "alias_id"),
    [
        ("ref_type", "ref_id", "REFType", "REFId"),
        ("resource_type", "resource_id", "RESOURCEType", "RESOURCEId"),
        ("aggregate_type", "aggregate_id", "AGGREGATEType", "AGGREGATEId"),
        ("object_type", "object_id", "OBJECTType", "OBJECTId"),
        ("subject_type", "subject_id", "SUBJECTType", "SUBJECTId"),
    ],
)
def test_completion_result_reference_uppercase_prefix_collisions_are_rejected(
    canonical_type: str,
    canonical_id: str,
    alias_type: str,
    alias_id: str,
) -> None:
    with pytest.raises(ValidationError, match="duplicate completion reference field"):
        RunCompletionReceiptRequest.model_validate(
            {
                "result_ref": {
                    canonical_type: "data_asset",
                    canonical_id: "asset-safe",
                    alias_type: "voiceprint_profile",
                    alias_id: "VP-SECRET",
                }
            }
        )


@pytest.mark.parametrize(
    ("result_ref", "expected"),
    [
        (
            {"REFTYPE": "data_asset", "REFID": "asset-001"},
            {"ref_type": "data_asset", "ref_id": "asset-001"},
        ),
        (
            {"RESOURCEType": "data_asset", "RESOURCEId": "asset-002"},
            {"resource_type": "data_asset", "resource_id": "asset-002"},
        ),
        (
            {"AGGREGATEType": "data_asset", "AGGREGATEId": "asset-003"},
            {"aggregate_type": "data_asset", "aggregate_id": "asset-003"},
        ),
        (
            {"OBJECTType": "data_asset", "OBJECTId": "asset-004"},
            {"object_type": "data_asset", "object_id": "asset-004"},
        ),
        (
            {"SUBJECTType": "data_asset", "SUBJECTId": "asset-005"},
            {"subject_type": "data_asset", "subject_id": "asset-005"},
        ),
    ],
)
def test_completion_result_reference_uppercase_variants_are_canonical(
    result_ref: dict[str, str],
    expected: dict[str, str],
) -> None:
    receipt = RunCompletionReceiptRequest.model_validate({"result_ref": result_ref})

    assert receipt.result_ref == expected


def test_completion_result_reference_families_are_canonical_and_complete() -> None:
    receipt = RunCompletionReceiptRequest.model_validate(
        {
            "result_ref": {
                "resourceType": "voiceprint_profile",
                "resourceId": "VP-RESOURCE",
                "nested": {
                    "ObjectType": "human_review_decision",
                    "ObjectId": "HRD-OBJECT",
                },
            }
        }
    )

    assert receipt.result_ref == {
        "resource_type": "voiceprint_profile",
        "resource_id": "VP-RESOURCE",
        "nested": {
            "object_type": "human_review_decision",
            "object_id": "HRD-OBJECT",
        },
    }


def test_dev_auth_provider_disables_demo_tokens_outside_local_without_override():
    provider = StaticDevAuthProvider(Settings(app_env="staging", allow_dev_auth=False))
    with pytest.raises(ApiError) as exc:
        provider.authenticate("dev-token")
    assert exc.value.code == "DEV_TOKEN_DISABLED"


def test_dev_auth_provider_does_not_allow_prod_override():
    with pytest.raises(ValidationError, match="ALLOW_DEV_AUTH"):
        prod_settings(allow_dev_auth=True)


def test_dev_auth_provider_can_be_used_for_ci_smoke():
    provider = StaticDevAuthProvider(Settings(app_env="ci", allow_dev_auth=True))
    actor = provider.authenticate("dev-token")
    assert actor.user_id == "u_admin_001"
    assert "project_admin" in actor.roles


def test_dev_auth_provider_accepts_server_issued_scoped_session():
    settings = Settings(app_env="ci", allow_dev_auth=True, dev_auth_password="unit-password")
    profile = get_dev_auth_profile("annotator@auris.local")
    assert profile is not None
    issued_at = int(time.time())
    token, expires_at = issue_dev_auth_token(profile, settings, now=issued_at)

    actor = StaticDevAuthProvider(settings).authenticate(token)

    assert expires_at == issued_at + settings.dev_auth_session_ttl_seconds
    assert actor.user_id == "u_annotator_001"
    assert actor.roles == ("annotator", "review_arbitrator")
    assert actor.tenant_ids == ("aurora_auto",)
    assert actor.project_ids == ("sales_qa",)
    assert actor.provider == "dev_session"
    assert actor.session_id is not None
    assert actor.issued_at == issued_at
    assert actor.expires_at == expires_at


def test_project_admin_dev_session_can_switch_project_scope_but_not_tenant_scope():
    settings = Settings(app_env="ci", allow_dev_auth=True, dev_auth_password="unit-password")
    profile = get_dev_auth_profile("admin@auris.local")
    assert profile is not None
    token, _ = issue_dev_auth_token(profile, settings, now=int(time.time()))

    actor = StaticDevAuthProvider(settings).authenticate(token)

    assert actor.tenant_ids == ("aurora_auto",)
    assert actor.project_ids == ("*",)


def test_system_demo_token_is_local_only():
    provider = StaticDevAuthProvider(Settings(app_env="ci", allow_dev_auth=True))
    actor = provider.authenticate("system-token")
    assert actor.user_id == "system"
    assert actor.roles == ("system",)

    with pytest.raises(ValidationError, match="ALLOW_DEV_AUTH"):
        prod_settings(allow_dev_auth=True)


def test_audio_playback_grant_is_scoped_signed_and_expires():
    test_signing_key = "playback-test-" + "fixture-key-not-for-production"
    settings = Settings(audio_playback_grant_secret=test_signing_key)
    token, created = create_audio_playback_grant(
        settings,
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="u_admin_001",
        audio_session_id="S20250526-000128",
        storage_object_id="sto-audio-v1",
        storage_provider="minio",
        object_version_id="immutable-version-v1",
        etag="audio-etag-v1",
        now=1000,
    )

    verified = verify_audio_playback_grant(settings, token, now=1001)
    assert verified == created
    assert verified.tenant_id == "aurora_auto"
    assert verified.audio_session_id == "S20250526-000128"
    assert verified.object_version_id == "immutable-version-v1"

    with pytest.raises(ApiError) as expired:
        verify_audio_playback_grant(settings, token, now=created.expires_at)
    assert expired.value.code == "AUDIO_PLAYBACK_GRANT_INVALID"


def test_audio_playback_grant_requires_dedicated_or_existing_secure_secret():
    settings = Settings(
        audio_playback_grant_secret="",
        auth_token_secret="",
        completion_receipt_secret="",
    )

    with pytest.raises(ApiError) as exc:
        create_audio_playback_grant(
            settings,
            tenant_id="aurora_auto",
            project_id="sales_qa",
            user_id="u_admin_001",
            audio_session_id="S20250526-000128",
        )
    assert exc.value.code == "AUDIO_PLAYBACK_GRANT_NOT_CONFIGURED"


def test_dev_auth_provider_fails_readiness_outside_local_env():
    with pytest.raises(ValidationError, match="AUTH_PROVIDER=dev"):
        prod_settings(auth_provider="dev", allow_dev_auth=False)


def test_signed_auth_provider_accepts_scoped_signed_token():
    token_key = "unit-signing-key-for-provider-tests-32"
    token = sign_auth_token(
        secret=token_key,
        user_id="u_admin_001",
        roles=("project_admin", "asset_manager"),
        tenant_ids=("aurora_auto",),
        project_ids=("sales_qa",),
        expires_at=int(time.time()) + 300,
    )
    provider = SignedTokenAuthProvider(transitional_signed_settings(auth_token_secret=token_key))

    actor = provider.authenticate(token)

    assert actor.user_id == "u_admin_001"
    assert actor.roles == ("project_admin", "asset_manager")
    assert actor.tenant_ids == ("aurora_auto",)
    assert actor.project_ids == ("sales_qa",)
    assert actor.provider == "signed"
    assert actor.session_id is not None
    assert actor.issued_at is not None
    assert actor.expires_at is not None


def test_signed_auth_provider_rejects_legacy_token_missing_required_claims():
    token_key = "unit-signing-key-for-provider-tests-32"
    legacy = _signed_raw_token(
        token_key,
        {
            "iss": "auris-flow",
            "sub": "u_admin_001",
            "roles": ["project_admin"],
            "tenant_ids": ["aurora_auto"],
            "project_ids": ["sales_qa"],
            "exp": int(time.time()) + 300,
        },
    )
    provider = SignedTokenAuthProvider(transitional_signed_settings(auth_token_secret=token_key))

    with pytest.raises(ApiError) as exc:
        provider.authenticate(legacy)

    assert exc.value.code == "UNAUTHORIZED"


def test_signed_auth_provider_rejects_wrong_audience_and_honors_clock_skew():
    token_key = "unit-signing-key-for-provider-tests-32"
    current_time = int(time.time())
    provider = SignedTokenAuthProvider(
        transitional_signed_settings(
            auth_token_secret=token_key,
            auth_token_clock_skew_seconds=30,
        )
    )
    wrong_audience = sign_auth_token(
        secret=token_key,
        user_id="u_admin_001",
        roles=("project_admin",),
        tenant_ids=("aurora_auto",),
        project_ids=("sales_qa",),
        issued_at=current_time,
        expires_at=current_time + 300,
        audience="another-api",
    )
    within_skew = sign_auth_token(
        secret=token_key,
        user_id="u_admin_001",
        roles=("project_admin",),
        tenant_ids=("aurora_auto",),
        project_ids=("sales_qa",),
        issued_at=current_time + 20,
        expires_at=current_time + 300,
    )
    beyond_skew = sign_auth_token(
        secret=token_key,
        user_id="u_admin_001",
        roles=("project_admin",),
        tenant_ids=("aurora_auto",),
        project_ids=("sales_qa",),
        issued_at=current_time + 31,
        expires_at=current_time + 300,
    )

    with pytest.raises(ApiError) as wrong_aud_error:
        provider.authenticate(wrong_audience, now=current_time)
    assert wrong_aud_error.value.code == "UNAUTHORIZED"
    assert provider.authenticate(within_skew, now=current_time).user_id == "u_admin_001"
    with pytest.raises(ApiError) as future_error:
        provider.authenticate(beyond_skew, now=current_time)
    assert future_error.value.code == "UNAUTHORIZED"


def test_signed_auth_provider_rejects_tampered_or_expired_token():
    token_key = "unit-signing-key-for-provider-tests-32"
    valid = sign_auth_token(
        secret=token_key,
        user_id="u_admin_001",
        roles=("project_admin",),
        tenant_ids=("aurora_auto",),
        project_ids=("sales_qa",),
        expires_at=int(time.time()) + 300,
    )
    provider = SignedTokenAuthProvider(transitional_signed_settings(auth_token_secret=token_key))
    with pytest.raises(ApiError) as tampered:
        provider.authenticate(f"{valid[:-1]}x")
    assert tampered.value.code == "UNAUTHORIZED"

    expired = sign_auth_token(
        secret=token_key,
        user_id="u_admin_001",
        roles=("project_admin",),
        tenant_ids=("aurora_auto",),
        project_ids=("sales_qa",),
        expires_at=int(time.time()) - 1,
    )
    with pytest.raises(ApiError) as expired_error:
        provider.authenticate(expired)
    assert expired_error.value.code == "UNAUTHORIZED"


def test_signed_auth_provider_fails_closed_without_secret_or_scope():
    with pytest.raises(ApiError) as missing_secret:
        SignedTokenAuthProvider(Settings(app_env="local", auth_provider="signed"))
    assert missing_secret.value.code == "AUTH_PROVIDER_NOT_CONFIGURED"

    token_key = "unit-signing-key-for-provider-tests-32"
    token = sign_auth_token(
        secret=token_key,
        user_id="u_admin_001",
        roles=("project_admin",),
        tenant_ids=(),
        project_ids=("sales_qa",),
        expires_at=int(time.time()) + 300,
    )
    provider = SignedTokenAuthProvider(transitional_signed_settings(auth_token_secret=token_key))
    with pytest.raises(ApiError) as missing_scope:
        provider.authenticate(token)
    assert missing_scope.value.code == "UNAUTHORIZED"


def test_dispatch_event_routes_to_expected_local_adapters():
    dagster = dispatch_event(
        "task_run.requested",
        "task_run",
        {
            "run_id": "run_001",
            "task_version_id": "task_v1",
            "partition_key": "p1",
            "execution_mode": "diagnostic",
        },
    )
    assert dagster.adapter == "dagster"
    assert dagster.details["job_name"] == "task_v1"

    qdrant = dispatch_event(
        "knowledge_index.build_requested",
        "knowledge_build",
        {
            "qdrant_payload": {
                "tenant_id": "aurora_auto",
                "project_id": "sales_qa",
                "trace_id": "trace_unit_qdrant",
                "collection": "knowledge_chunks",
                "knowledge_index_id": "ki_001",
                "knowledge_source_id": "ks_001",
                "source_id": "ks_001",
                "source_type": "sop_faq_product_docs",
                "asset_key": "auris/knowledge/ks_001",
                "version": "kb-index-v1",
                "business_ref": {"connector_id": "conn_001"},
            }
        },
    )
    assert qdrant.adapter == "qdrant"
    assert qdrant.details["collection"] == "knowledge_chunks"
    assert qdrant.details["qdrant_payload"]["asset_key"] == "auris/knowledge/ks_001"

    callback = dispatch_event(
        "external_callback.requested",
        "external_callback",
        {"target": "crm_order", "idempotency_key": "cb_001"},
    )
    assert callback.adapter == "external_callback"
    assert callback.details["signature_mode"] == "mock-hmac-sha256"

    provider = dispatch_event(
        "provider_test.requested",
        "provider_test",
        {
            "run_id": "provider_run_001",
            "provider": "asr",
            "execution_mode": "diagnostic",
        },
    )
    assert provider.adapter == "dagster"
    assert provider.details["external_run_id"].startswith("dg_run_")

    audio_intelligence = dispatch_event(
        "audio_intelligence.requested",
        "audio_intelligence",
        {
            "run_id": "audio_intelligence_unit",
            "audio_session_id": "S20250526-000128",
            "recording_id": "A-1001_20250526_122300",
            "job_name": "audio_intelligence_pipeline",
            "execution_mode": "diagnostic",
        },
    )
    assert audio_intelligence.adapter == "dagster"
    assert audio_intelligence.details["job_name"] == "audio_intelligence_pipeline"
    assert audio_intelligence.details["external_run_id"].startswith("dg_run_")

    boundary_sync = dispatch_event(
        "conversation_boundary.sync_requested",
        "boundary_sync",
        {
            "run_id": "boundary_sync_unit",
            "boundary_id": "boundary_s128_v1",
            "job_name": "conversation_boundary_sync_pipeline",
        },
    )
    assert boundary_sync.adapter == "dagster"
    assert boundary_sync.details["job_name"] == "conversation_boundary_sync_pipeline"
    assert boundary_sync.details["external_run_id"].startswith("dg_run_")

    projection = dispatch_event("label_versions.created", "label_versions", {"id": "lv_1"})
    assert projection.adapter == "projection"
    assert projection.details["aggregate_id"] == "lv_1"

    governed_projection = dispatch_event(
        "human_review.decision.created",
        "human_review_decision",
        {
            "subject": {"type": "human_review_decision", "id": "hrd_1"},
            "data": {"review_task_id": "hrt_1"},
        },
    )
    assert governed_projection.details == {
        "event_type": "human_review.decision.created",
        "aggregate_type": "human_review_decision",
        "aggregate_id": "hrd_1",
    }


def test_dispatch_event_can_return_structured_retryable_failure():
    dispatch = dispatch_event(
        "external_callback.requested",
        "external_callback",
        {
            "target": "crm_order",
            "simulate_adapter_failure": True,
            "adapter_error_code": "CALLBACK_TIMEOUT",
            "adapter_error_message": "callback timed out",
            "adapter_retryable": True,
            "retry_after_seconds": 7,
        },
    )
    assert dispatch.status == "failed"
    assert dispatch.error_code == "CALLBACK_TIMEOUT"
    assert dispatch.error_message == "callback timed out"
    assert dispatch.retryable is True
    assert dispatch.retry_after_seconds == 7


def test_qdrant_adapter_rejects_payload_without_recall_back_reference():
    dispatch = dispatch_event(
        "knowledge_index.build_requested",
        "knowledge_build",
        {"vector_collection": "knowledge_chunks", "source_id": "ks_001"},
    )
    assert dispatch.status == "failed"
    assert dispatch.error_code == "QDRANT_PAYLOAD_INVALID"
    assert dispatch.retryable is False
    assert "tenant_id" in dispatch.details["missing_fields"]
    assert "asset_key" in dispatch.details["missing_fields"]


def test_real_qdrant_adapter_builds_collection_and_upserts_point_without_network():
    class StubRealQdrantIndexClient(RealQdrantIndexClient):
        def __init__(self) -> None:
            super().__init__(base_url="http://qdrant.example.test", vector_size=8)
            self.calls: list[tuple[str, str, dict | None]] = []

        def _request(self, method: str, path: str, body: dict | None = None) -> dict:
            self.calls.append((method, path, body))
            if method == "GET":
                raise HTTPError(path, 404, "missing", {}, None)
            if path.endswith("/points?wait=true"):
                return {"result": {"operation_id": 42, "status": "completed"}}
            return {"status": "ok"}

    client = StubRealQdrantIndexClient()
    dispatch = dispatch_event(
        "knowledge_index.build_requested",
        "knowledge_build",
        {
            "qdrant_payload": {
                "tenant_id": "aurora_auto",
                "project_id": "sales_qa",
                "trace_id": "trace_real_qdrant",
                "collection": "knowledge_chunks_test",
                "knowledge_index_id": "ki_001",
                "knowledge_source_id": "ks_001",
                "source_id": "ks_001",
                "source_type": "sop_faq_product_docs",
                "asset_key": "auris/knowledge/ks_001",
                "version": "kb-index-v1",
                "business_ref": {"connector_id": "conn_001"},
            }
        },
        registry=AdapterRegistry(qdrant=client),
    )

    assert dispatch.status == "success"
    assert dispatch.adapter == "qdrant"
    assert dispatch.details["mode"] == "real"
    assert dispatch.details["collection"] == "knowledge_chunks_test"
    assert dispatch.details["point_count"] == 1
    assert dispatch.details["vector_size"] == 8
    assert dispatch.details["operation_id"] == 42
    assert client.calls[0] == ("GET", "/collections/knowledge_chunks_test", None)
    assert client.calls[1][0:2] == ("PUT", "/collections/knowledge_chunks_test")
    assert client.calls[2][0] == "PUT"
    assert client.calls[2][1] == "/collections/knowledge_chunks_test/points?wait=true"
    point = client.calls[2][2]["points"][0]
    assert point["payload"]["asset_key"] == "auris/knowledge/ks_001"
    assert len(point["vector"]) == 8


def test_real_qdrant_adapter_searches_with_scope_filter_without_network():
    point_001 = "11111111-1111-4111-8111-111111111111"
    point_002 = "22222222-2222-4222-8222-222222222222"

    class StubRealQdrantIndexClient(RealQdrantIndexClient):
        def __init__(self) -> None:
            super().__init__(base_url="http://qdrant.example.test", vector_size=8)
            self.calls: list[tuple[str, str, dict | None]] = []

        def _request(self, method: str, path: str, body: dict | None = None) -> dict:
            self.calls.append((method, path, body))
            return {
                "result": [
                    {
                        "id": point_001,
                        "score": 0.91,
                        "payload": {
                            "tenant_id": "aurora_auto",
                            "project_id": "sales_qa",
                            "knowledge_index_id": "ki_001",
                            "asset_key": "auris/knowledge/ks_001",
                        },
                    }
                ]
            }

    client = StubRealQdrantIndexClient()
    result = client.search_index_payload(
        {
            "tenant_id": "aurora_auto",
            "project_id": "sales_qa",
            "collection": "knowledge_chunks_test",
            "knowledge_index_id": "ki_001",
            "embedding_space_fingerprint": client.embedding_space_fingerprint,
            "_authorized_point_ids": [point_001, point_002],
        },
        query="报价金额冲突处理 SOP",
        top_k=3,
    )

    assert result["mode"] == "real_qdrant"
    assert result["collection"] == "knowledge_chunks_test"
    assert result["points"][0]["id"] == point_001
    assert client.calls[0][0:2] == (
        "POST",
        "/collections/knowledge_chunks_test/points/search",
    )
    body = client.calls[0][2]
    assert body["limit"] == 3
    assert body["with_payload"] is True
    assert body["with_vector"] is False
    assert len(body["vector"]) == 8
    assert body["filter"]["must"] == [
        {"key": "tenant_id", "match": {"value": "aurora_auto"}},
        {"key": "project_id", "match": {"value": "sales_qa"}},
        {"key": "knowledge_index_id", "match": {"value": "ki_001"}},
        {
            "key": "embedding_space_fingerprint",
            "match": {"value": client.embedding_space_fingerprint},
        },
        {"has_id": [point_001, point_002]},
    ]


def test_real_qdrant_adapter_sends_configured_api_key(monkeypatch):
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size: int = -1) -> bytes:
            captured["read_size"] = size
            return b'{"result":[]}'[:size]

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(adapter_module, "urlopen", fake_urlopen)
    client = RealQdrantIndexClient(
        base_url="http://qdrant.example.test",
        vector_size=8,
        api_key="qdrant-test-key",
    )

    client.search_index_payload(
        {
            "tenant_id": "aurora_auto",
            "project_id": "sales_qa",
            "collection": "knowledge_chunks_test",
            "knowledge_index_id": "ki_001",
            "embedding_space_fingerprint": client.embedding_space_fingerprint,
            "_authorized_point_ids": ["11111111-1111-4111-8111-111111111111"],
        },
        query="报价金额冲突处理 SOP",
        top_k=3,
    )

    request = captured["request"]
    assert request.get_header("Api-key") == "qdrant-test-key"
    assert captured["timeout"] == 5
    assert captured["read_size"] == MAX_QDRANT_RESPONSE_BYTES + 1


def test_real_qdrant_adapter_rejects_oversized_response_before_buffering(monkeypatch):
    read_sizes: list[int] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return b"x" * size

    monkeypatch.setattr(adapter_module, "urlopen", lambda *_args, **_kwargs: Response())
    client = RealQdrantIndexClient(
        base_url="http://qdrant.example.test",
        vector_size=8,
    )

    with pytest.raises(ValueError, match="response is too large"):
        client._request("GET", "/collections/knowledge_chunks_test")

    assert read_sizes == [MAX_QDRANT_RESPONSE_BYTES + 1]


def test_real_dagster_adapter_posts_graphql_run_request_without_network():
    class StubRealDagsterClient(RealDagsterClient):
        def __init__(self) -> None:
            super().__init__(
                graphql_url="http://dagster.example.test/graphql",
                repository_location_name="auris_defs",
                repository_name="auris_repo",
                default_job_name="auris_generic_job",
            )
            self.requests: list[dict] = []

        def _request(self, body: dict) -> dict:
            self.requests.append(body)
            return {
                "data": {
                    "launchPipelineExecution": {
                        "__typename": "LaunchRunSuccess",
                        "run": {"runId": "real_dagster_run_001", "status": "STARTED"},
                    }
                },
                "extensions": {
                    "auris_protocol_receipt": {
                        "receipt_url": "http://dagster.example.test/receipts/real_dagster_run_001",
                        "request_sha256": "stub",
                    }
                },
            }

    client = StubRealDagsterClient()
    dispatch = dispatch_event(
        "task_run.requested",
        "task_run",
        {
            "run_id": "task_run_001",
            "tenant_id": "aurora_auto",
            "project_id": "sales_qa",
            "trace_id": "trace_real_dagster",
            "task_version_id": "task_version_v3_2_1",
            "job_name": "caller_selected_job",
            "dagster_run_draft": {"job_name": "draft_selected_job"},
            "run_config": {
                "execution": {"mode": "caller-selected-mode"},
                "resources": {"unsafe": {"config": {"token": "must-not-forward"}}},
            },
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/12",
            "event_type": "task_run.requested",
            "execution_mode": "diagnostic",
            "dispatch_idempotency_key": "outbox:task-run:task_run_001",
            "outbox_fencing_token": "123:1",
        },
        registry=AdapterRegistry(dagster=client),
    )

    assert dispatch.status == "success"
    assert dispatch.adapter == "dagster"
    assert dispatch.details["mode"] == "real"
    assert dispatch.details["external_run_id"] == "real_dagster_run_001"
    assert dispatch.details["dagster_run_id"] == "real_dagster_run_001"
    assert dispatch.details["run_id"] == "task_run_001"
    assert dispatch.details["run_key"] == "outbox:task-run:task_run_001"
    assert dispatch.details["job_name"] == "auris_generic_job"
    assert dispatch.details["response_typename"] == "LaunchRunSuccess"
    assert len(dispatch.details["graphql_payload_sha256"]) == 64
    assert dispatch.details["protocol_receipt"]["receipt_url"].endswith(
        "/receipts/real_dagster_run_001"
    )
    request = client.requests[0]
    execution_params = request["variables"]["executionParams"]
    assert execution_params["selector"] == {
        "repositoryLocationName": "auris_defs",
        "repositoryName": "auris_repo",
        "pipelineName": "auris_generic_job",
    }
    assert execution_params["runConfigData"]["execution"] == {
        "mode": "control-plane-acknowledgement"
    }
    assert "resources" not in execution_params["runConfigData"]
    assert "runKey" not in execution_params["executionMetadata"]
    tags = {item["key"]: item["value"] for item in execution_params["executionMetadata"]["tags"]}
    assert tags["tenant_id"] == "aurora_auto"
    assert tags["project_id"] == "sales_qa"
    assert tags["trace_id"] == "trace_real_dagster"
    assert tags["run_id"] == "task_run_001"
    assert tags["dispatch_idempotency_key"] == "outbox:task-run:task_run_001"
    assert tags["auris/dispatch_idempotency_key"] == "outbox:task-run:task_run_001"
    assert tags["outbox_fencing_token"] == "123:1"


def test_real_dagster_run_config_discards_caller_control_plane_config():
    client = RealDagsterClient(
        graphql_url="http://dagster.example.test/graphql",
        repository_location_name="auris_defs",
        repository_name="auris_repo",
        default_job_name="auris_generic_job",
    )
    payload = {
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "trace_id": "trace_server_authoritative",
        "run_id": "task_run_002",
        "event_type": "task_run.requested",
        "execution_mode": "diagnostic",
        "dispatch_idempotency_key": "outbox:task-run:task_run_002",
        "outbox_fencing_token": "456:2",
        "run_config": {
            "resources": {"io_manager": {"config": {"bucket": "tenant-artifacts"}}},
            "auris_context": {
                "tenant_id": "forged_tenant",
                "project_id": "forged_project",
                "trace_id": "forged_trace",
                "caller_hint": "preserved",
            },
        },
    }

    run_config = client._run_config(payload)

    assert "resources" not in run_config
    assert "caller_hint" not in run_config["auris_context"]
    assert run_config["execution"] == {"mode": "control-plane-acknowledgement"}
    assert run_config["auris_context"]["tenant_id"] == "aurora_auto"
    assert run_config["auris_context"]["project_id"] == "sales_qa"
    assert run_config["auris_context"]["trace_id"] == "trace_server_authoritative"
    assert run_config["auris_context"]["run_id"] == "task_run_002"
    assert run_config["auris_context"]["dispatch_idempotency_key"] == "outbox:task-run:task_run_002"
    assert run_config["auris_context"]["outbox_fencing_token"] == "456:2"
    assert payload["run_config"]["auris_context"]["tenant_id"] == "forged_tenant"


def test_real_dagster_run_config_uses_explicit_ci_service_mode_not_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "ci")
    client = RealDagsterClient(
        graphql_url="http://dagster.example.test/graphql",
        default_job_name="auris_generic_job",
        execution_mode="ci-cancel-delay",
    )

    run_config = client._run_config(
        {
            "tenant_id": "aurora_auto",
            "project_id": "sales_qa",
            "trace_id": "trace_ci_controlled",
            "run_id": "task_run_ci_controlled",
            "run_config": {"execution": {"mode": "caller-selected-mode"}},
        }
    )

    assert run_config["execution"] == {"mode": "ci-cancel-delay"}


def test_real_dagster_rejects_ci_service_mode_outside_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "prod")

    with pytest.raises(ValueError, match="CI-only"):
        RealDagsterClient(execution_mode="ci-cancel-delay")


def test_real_dagster_run_config_propagates_current_otel_parent() -> None:
    client = RealDagsterClient(
        graphql_url="http://dagster.example.test/graphql",
        repository_location_name="auris_defs",
        repository_name="auris_repo",
        default_job_name="auris_generic_job",
    )
    payload = {
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "trace_id": "trace_server_authoritative",
        "run_id": "task_run_otel",
        "dispatch_idempotency_key": "outbox:task-run:task_run_otel",
        "outbox_fencing_token": "456:3",
    }
    provider = TracerProvider()
    tracer = provider.get_tracer("dagster-propagation-test")
    try:
        with tracer.start_as_current_span("outbox.dispatch") as span:
            span_context = span.get_span_context()
            run_context = client._run_config(payload)["auris_context"]
    finally:
        provider.shutdown()

    assert run_context["otel_trace_id"] == trace.format_trace_id(span_context.trace_id)
    assert run_context["otel_parent_span_id"] == trace.format_span_id(span_context.span_id)
    assert run_context["otel_trace_flags"] == f"{int(span_context.trace_flags):02x}"


def test_real_external_callback_adapter_signs_and_records_protocol_receipt():
    client = RealExternalCallbackClient(
        callback_url="http://callback.example.test/callbacks/platform",
        secret="unit-callback-key-material-at-least-32-bytes",
        clock=lambda: 1_783_474_523,
    )
    captured = {}

    def fake_request(body, headers):
        captured["body"] = body
        captured["headers"] = headers
        return {
            "status_code": 202,
            "headers": {"X-Auris-Callback-Receipt-Id": "callback_receipt_123"},
            "body": (
                b'{"status":"ok","data":{"callback_receipt_id":"callback_receipt_123",'
                b'"remote_trace_id":"remote_trace_123",'
                b'"receipt_url":"http://callback.example.test/receipts/callback_receipt_123"}}'
            ),
        }

    client._request = fake_request  # type: ignore[method-assign]
    dispatch = client.send_signed_callback(
        {
            "target": "crm_reception_order",
            "tenant_id": "aurora_auto",
            "project_id": "sales_qa",
            "trace_id": "trace_callback_unit",
            "run_id": "external_callback_unit",
            "idempotency_key": "idem_callback_unit",
            "payload_template": {"processed_wav_url": "obs://demo/processed.wav"},
        }
    )

    assert dispatch.status == "success"
    assert dispatch.adapter == "external_callback"
    assert dispatch.details["mode"] == "real"
    assert dispatch.details["callback_receipt_id"] == "callback_receipt_123"
    assert dispatch.details["remote_trace_id"] == "remote_trace_123"
    assert dispatch.details["request_sha256"] == hashlib.sha256(captured["body"]).hexdigest()
    assert len(dispatch.details["response_sha256"]) == 64
    assert len(dispatch.details["signature_sha256"]) == 64
    assert captured["headers"]["X-Auris-Idempotency-Key"] == "idem_callback_unit"
    assert captured["headers"]["X-Auris-Signature-Mode"] == "hmac-sha256-v2"
    assert captured["headers"]["X-Auris-Signature"].startswith("v2=")
    assert captured["headers"]["X-Auris-Key-Id"] == "local-dev-callback"
    assert "X-Auris-Signature-Id" not in captured["headers"]
    body = captured["body"].decode("utf-8")
    assert "processed_wav_url" in body


def test_real_object_storage_adapter_creates_bucket_and_verifies_object_without_network():
    class StubRealObjectStorageClient(RealObjectStorageClient):
        def __init__(self) -> None:
            super().__init__(
                endpoint="http://minio.example.test",
                bucket="auris-test",
                access_key="access",
                secret_key="secret",
            )
            self.calls: list[tuple[str, str, bytes | None, str]] = []

        def _request(
            self,
            method: str,
            path: str,
            body: bytes | None = None,
            content_type: str = "application/json",
            extra_headers: dict[str, str] | None = None,
            timeout_seconds: float = 5.0,
            query: dict[str, str] | None = None,
            max_response_bytes: int | None = None,
        ) -> dict:
            del timeout_seconds, query, max_response_bytes
            self.calls.append((method, path, body, content_type))
            if method == "HEAD" and path == "/auris-test":
                raise HTTPError(path, 404, "missing", {}, None)
            if method == "PUT" and path == "/auris-test":
                return {"status": 200}
            if method == "PUT":
                return {"status": 200, "etag": "etag-001"}
            return {"status": 200}

    client = StubRealObjectStorageClient()
    dispatch = dispatch_event(
        "export.requested",
        "export",
        {
            "run_id": "export_001",
            "trace_id": "trace_object_storage",
            "tenant_id": "aurora_auto",
            "project_id": "sales_qa",
            "asset_ref": "auris/reports/daily",
            "content_type": "application/json",
        },
        registry=AdapterRegistry(object_storage=client),
    )

    assert dispatch.status == "success"
    assert dispatch.adapter == "object_storage"
    assert dispatch.details["mode"] == "real"
    assert dispatch.details["bucket"] == "auris-test"
    assert dispatch.details["object_key"].startswith(
        "tenants/aurora_auto/projects/sales_qa/auris/reports/daily/"
    )
    assert dispatch.details["object_uri"].startswith(
        "s3://auris-test/tenants/aurora_auto/projects/sales_qa/auris/reports/daily/"
    )
    assert dispatch.details["protocol"] == "s3"
    assert dispatch.details["provider"] == "minio"
    assert len(dispatch.details["content_sha256"]) == 64
    assert dispatch.details["content_length"] > 0
    assert dispatch.details["etag"] == "etag-001"
    assert client.calls[0] == ("HEAD", "/auris-test", None, "application/json")
    assert client.calls[1] == ("PUT", "/auris-test", b"", "application/json")
    assert client.calls[2][0] == "PUT"
    assert client.calls[2][1].startswith(
        "/auris-test/tenants/aurora_auto/projects/sales_qa/auris/reports/daily/"
    )


def test_real_object_storage_get_object_forwards_http_range_without_network():
    class StubRealObjectStorageClient(RealObjectStorageClient):
        def __init__(self) -> None:
            super().__init__(
                endpoint="http://minio.example.test",
                bucket="auris-test",
                access_key="access",
                secret_key="secret",
            )
            self.extra_headers: dict[str, str] | None = None

        def _request(
            self,
            method: str,
            path: str,
            body: bytes | None = None,
            content_type: str = "application/json",
            extra_headers: dict[str, str] | None = None,
            timeout_seconds: float = 5.0,
            query: dict[str, str] | None = None,
            max_response_bytes: int | None = None,
        ) -> dict:
            del timeout_seconds, query, max_response_bytes
            self.extra_headers = extra_headers
            return {
                "status": 206,
                "headers": {
                    "Accept-Ranges": "bytes",
                    "Content-Range": "bytes 10-19/100",
                    "Content-Length": "10",
                },
                "content_range": "bytes 10-19/100",
                "content_length": "10",
                "content_type": "audio/wav",
                "body": b"0123456789",
            }

    client = StubRealObjectStorageClient()
    result = client.get_object(
        "auris-test",
        "tenants/aurora_auto/projects/sales_qa/audio/raw/demo.wav",
        byte_range="bytes=10-19",
    )

    assert client.extra_headers == {"Range": "bytes=10-19"}
    assert result["status"] == 206
    assert result["body"] == b"0123456789"
    assert result["content_range"] == "bytes 10-19/100"


def test_real_object_storage_sigv4_signs_range_header():
    client = RealObjectStorageClient(
        endpoint="http://minio.example.test",
        bucket="auris-test",
        access_key="access",
        secret_key="secret",
    )
    auth = client._authorization_header(
        method="GET",
        canonical_uri="/auris-test/demo.wav",
        headers={
            "Content-Type": "audio/wav",
            "Host": "minio.example.test",
            "Range": "bytes=10-19",
            "x-amz-content-sha256": hashlib.sha256(b"").hexdigest(),
            "x-amz-date": "20250526T122300Z",
        },
        payload_hash=hashlib.sha256(b"").hexdigest(),
        date_stamp="20250526",
        timestamp="20250526T122300Z",
    )

    assert "SignedHeaders=content-type;host;range;x-amz-content-sha256;x-amz-date" in auth


def test_dispatch_event_can_return_structured_terminal_failure():
    dispatch = dispatch_event(
        "external_callback.requested",
        "external_callback",
        {
            "target": "crm_order",
            "simulate_adapter_failure": True,
            "adapter_error_code": "CALLBACK_SIGNATURE_INVALID",
            "adapter_retryable": False,
        },
    )
    assert dispatch.status == "failed"
    assert dispatch.error_code == "CALLBACK_SIGNATURE_INVALID"
    assert dispatch.retryable is False


def test_parse_payload_returns_structured_validation_errors():
    with pytest.raises(ApiError) as exc:
        parse_payload(EvalRunRequest, {"model_version": "candidate"})
    assert exc.value.code == "VALIDATION_ERROR"
    assert exc.value.status_code == 422
    assert exc.value.details[0]["field"] == "dataset_id"
