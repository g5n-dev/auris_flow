from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.domain.label_mapping import sha256_document
from app.models import (
    AuditLog,
    HumanReviewDecision,
    JsonResource,
    LabelFact,
    LabelFactHead,
    LabelMappingBundle,
    LabelMappingBundlePath,
    LabelTaxonomy,
    LabelVersion,
    LabelVersionItem,
    ListeningAnnotation,
    OutboxEvent,
    Project,
    ReleaseBundleHead,
    ReleaseDeployment,
)

TENANT_ID = "aurora_auto"
PROJECT_ID = "sales_qa"
OTHER_PROJECT_ID = "manual_label_other_project"
AUDIO_SESSION_ID = "S20250526-000128"
TAXONOMY_ID = "taxonomy-manual-label-http"
VERSION_V1 = "label-version-manual-http-v1"
VERSION_V2 = "label-version-manual-http-v2"
LABEL_V1 = "purchase-intent-http-legacy"
LABEL_V2 = "purchase-intent-http"
DEPLOYMENT_V1 = "deployment-manual-http-v1"
DEPLOYMENT_V2 = "deployment-manual-http-v2"
RELEASE_HEAD_ID = "release-head-manual-http-production"
MAPPING_BUNDLE_ID = "mapping-bundle-manual-http-v1-v2"
OCCURRED_AT = "2026-07-17T08:30:45Z"


def _headers(
    auth_headers: dict[str, str],
    key: str,
    *,
    token: str = "dev-token",
    project_id: str = PROJECT_ID,
) -> dict[str, str]:
    return {
        **auth_headers,
        "Authorization": f"Bearer {token}",
        "X-Project-Id": project_id,
        "Idempotency-Key": key,
    }


def _deployment(
    deployment_id: str,
    label_version_id: str,
    suffix: str,
) -> ReleaseDeployment:
    return ReleaseDeployment(
        deployment_id=deployment_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        environment="production",
        status="success",
        stage="production",
        label_version_id=label_version_id,
        prompt_version_id=f"prompt-manual-http-{suffix}",
        model_version=f"model-manual-http-{suffix}",
        aggregation_policy_version_id=f"policy-manual-http-{suffix}",
        eval_dataset_version_id=f"dataset-manual-http-{suffix}",
        eval_run_id=f"eval-run-manual-http-{suffix}",
        bundle_sha256=("1" if suffix == "v1" else "2") * 64,
        rollout_percentage=100,
        blocked_reasons=[],
        monitor_metrics={},
        approved_by="u_admin_001",
        trace_id=f"trace-deployment-manual-http-{suffix}",
        payload={"fixture": True},
    )


def _version(
    label_version_id: str,
    semantic_version: str,
    content_character: str,
) -> LabelVersion:
    return LabelVersion(
        label_version_id=label_version_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        status="published",
        resource_version=1,
        taxonomy_id=TAXONOMY_ID,
        semantic_version=semantic_version,
        artifact_status="published",
        artifact_published_at=datetime(2026, 7, 16, tzinfo=UTC),
        content_sha256=content_character * 64,
        trace_id=f"trace-{label_version_id}",
        payload={"fixture": True},
    )


def _version_item(
    item_id: str,
    label_version_id: str,
    label_id: str,
) -> LabelVersionItem:
    return LabelVersionItem(
        label_version_item_id=item_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        label_version_id=label_version_id,
        label_id=label_id,
        canonical_name=label_id,
        aliases=[],
        value_type="boolean",
        risk_level="medium",
        parent_ids=[],
        aggregation_rule={"kind": "manual"},
        status="active",
        definition_sha256=sha256_document([label_version_id, label_id, "boolean"]),
        trace_id=f"trace-{item_id}",
    )


def _seed_release_scope() -> None:
    with SessionLocal.begin() as session:
        session.add(
            LabelTaxonomy(
                taxonomy_id=TAXONOMY_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                name="Manual label HTTP taxonomy",
                description="manual label BFF contract fixture",
                status="active",
                resource_version=1,
                content_sha256="a" * 64,
                trace_id="trace-taxonomy-manual-label-http",
                payload={"fixture": True},
            )
        )
        session.add_all(
            [
                _version(VERSION_V1, "1.0.0", "b"),
                _version(VERSION_V2, "2.0.0", "c"),
            ]
        )
        session.add_all(
            [
                _version_item("item-manual-label-http-v1", VERSION_V1, LABEL_V1),
                _version_item("item-manual-label-http-v2", VERSION_V2, LABEL_V2),
            ]
        )
        session.add_all(
            [
                _deployment(DEPLOYMENT_V1, VERSION_V1, "v1"),
                _deployment(DEPLOYMENT_V2, VERSION_V2, "v2"),
            ]
        )
        session.flush()
        session.add(
            ReleaseBundleHead(
                release_head_id=RELEASE_HEAD_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                environment="production",
                active_deployment_id=DEPLOYMENT_V1,
                active_bundle_sha256="1" * 64,
                prompt_asset_id="prompt-asset-manual-http-v1",
                prompt_version_id="prompt-manual-http-v1",
                label_version_id=VERSION_V1,
                model_version="model-manual-http-v1",
                aggregation_policy_version_id="policy-manual-http-v1",
                eval_dataset_version_id="dataset-manual-http-v1",
                generation=1,
                status="active",
                bootstrapped=True,
                trace_id="trace-release-head-manual-http-v1",
                payload={"fixture": True},
            )
        )


def _switch_head_to_v2() -> None:
    with SessionLocal.begin() as session:
        head = session.get(ReleaseBundleHead, RELEASE_HEAD_ID)
        assert head is not None
        head.active_deployment_id = DEPLOYMENT_V2
        head.active_bundle_sha256 = "2" * 64
        head.prompt_asset_id = "prompt-asset-manual-http-v2"
        head.prompt_version_id = "prompt-manual-http-v2"
        head.label_version_id = VERSION_V2
        head.model_version = "model-manual-http-v2"
        head.aggregation_policy_version_id = "policy-manual-http-v2"
        head.eval_dataset_version_id = "dataset-manual-http-v2"
        head.generation = 2
        head.trace_id = "trace-release-head-manual-http-v2"


def _seed_mapping_bundle() -> None:
    with SessionLocal.begin() as session:
        session.add(
            LabelMappingBundle(
                mapping_bundle_id=MAPPING_BUNDLE_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                target_label_version_id=VERSION_V2,
                source_label_version_ids=[VERSION_V1],
                source_manifest_sha256="d" * 64,
                compiler_version="manual-http-test-compiler/1",
                status="published",
                resource_version=1,
                canonical_manifest_sha256="e" * 64,
                approval_id="approval-manual-label-http",
                approved_by="u_admin_001",
                approved_at=datetime(2026, 7, 18, 1, tzinfo=UTC),
                published_at=datetime(2026, 7, 18, 2, tzinfo=UTC),
                root_trace_id="root-trace-mapping-manual-label-http",
                trace_id="trace-mapping-manual-label-http",
                payload={"fixture": True},
            )
        )
        session.flush()
        session.add(
            LabelMappingBundlePath(
                bundle_path_id="path-mapping-manual-label-http",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                mapping_bundle_id=MAPPING_BUNDLE_ID,
                source_label_version_id=VERSION_V1,
                target_label_version_id=VERSION_V2,
                source_label_id=LABEL_V1,
                target_label_id=LABEL_V2,
                metric_family="manual-label",
                relation_path=[
                    {
                        "relation_type": "rename",
                        "source_label_id": LABEL_V1,
                        "target_label_id": LABEL_V2,
                    }
                ],
                mapping_version_ids=["mapping-version-manual-label-http"],
                metric_grain="event",
                lineage_key="manual-label-http-lineage",
                reducer=None,
                comparability_status="comparable",
                requires_recompute=False,
                path_sha256=sha256_document([MAPPING_BUNDLE_ID, LABEL_V1, LABEL_V2]),
                trace_id="trace-path-mapping-manual-label-http",
                payload={"fixture": True},
            )
        )


def _create_body(
    annotation_id: str,
    *,
    value: bool = True,
    generation: int = 1,
    label_version_id: str = VERSION_V1,
    label_id: str = LABEL_V1,
) -> dict[str, object]:
    return {
        "annotation_kind": "label-fact-draft",
        "annotation_id": annotation_id,
        "label_version_id": label_version_id,
        "label_id": label_id,
        "subject_scope": "business-event",
        "subject_key": "business-event-http-4242",
        "event_or_segment_id": "segment-http-17",
        "assertion_slot": "agent-purchase-intent",
        "occurred_at": OCCURRED_AT,
        "evidence_ref": {
            "type": "audio-segment",
            "id": f"{AUDIO_SESSION_ID}:segment-http-17",
            "sha256": "f" * 64,
            "start_ms": 1250,
            "end_ms": 8840,
        },
        "value_type": "boolean",
        "value": value,
        "environment": "production",
        "expected_release_head_generation": generation,
    }


def _create_path() -> str:
    return f"/api/v1/audio-sessions/{AUDIO_SESSION_ID}/annotations"


def _submission_path(annotation_id: str) -> str:
    return f"{_create_path()}/{annotation_id}/submissions"


def _rebase_path(annotation_id: str) -> str:
    return f"{_create_path()}/{annotation_id}/rebases"


def _submit_body(draft_sha256: str, *, generation: int = 1) -> dict[str, object]:
    return {
        "expected_draft_sha256": draft_sha256,
        "expected_release_head_generation": generation,
        "confirmation": "submit-frozen-manual-label",
    }


def _preview_body() -> dict[str, object]:
    return {
        "action": "preview",
        "mapping_bundle_id": MAPPING_BUNDLE_ID,
        "expected_release_head_generation": 2,
    }


def _assert_envelope(payload: dict[str, object]) -> tuple[dict[str, object], str]:
    assert set(payload) == {"data", "meta"}
    data = payload["data"]
    meta = payload["meta"]
    assert isinstance(data, dict)
    assert isinstance(meta, dict)
    trace_id = meta.get("trace_id")
    assert isinstance(trace_id, str) and trace_id.startswith("trace_")
    return data, trace_id


def _create_draft(client, auth_headers, annotation_id: str) -> dict[str, object]:
    response = client.post(
        _create_path(),
        json=_create_body(annotation_id),
        headers=_headers(auth_headers, f"create-{annotation_id}"),
    )
    assert response.status_code == 201, response.text
    data, _trace_id = _assert_envelope(response.json())
    return data


def test_label_version_items_are_readable_only_inside_the_version_scope(
    client,
    auth_headers,
) -> None:
    _seed_release_scope()

    current = client.get(
        f"/api/v1/label-versions/{VERSION_V1}/items?status=active",
        headers=_headers(auth_headers, "unused-read-key"),
    )
    cross_scope = client.get(
        f"/api/v1/label-versions/{VERSION_V1}/items",
        headers=_headers(
            auth_headers,
            "unused-cross-scope-read-key",
            project_id=OTHER_PROJECT_ID,
        ),
    )

    assert current.status_code == 200, current.text
    assert current.json()["data"]["items"] == [
        {
            "label_version_item_id": "item-manual-label-http-v1",
            "label_version_id": VERSION_V1,
            "label_id": LABEL_V1,
            "canonical_name": LABEL_V1,
            "aliases": [],
            "value_type": "boolean",
            "risk_level": "medium",
            "mutual_exclusion_group": None,
            "parent_ids": [],
            "aggregation_rule": {"kind": "manual"},
            "status": "active",
            "definition_sha256": sha256_document([VERSION_V1, LABEL_V1, "boolean"]),
            "trace_id": "trace-item-manual-label-http-v1",
        }
    ]
    assert cross_scope.status_code == 403
    assert cross_scope.json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_label_version_items_use_stable_label_id_cursor_pagination(
    client,
    auth_headers,
) -> None:
    _seed_release_scope()
    first_label_id = "a-manual-label-http"
    with SessionLocal.begin() as session:
        session.add(
            _version_item(
                "item-manual-label-http-a",
                VERSION_V1,
                first_label_id,
            )
        )

    first = client.get(
        f"/api/v1/label-versions/{VERSION_V1}/items?status=active&limit=1",
        headers=_headers(auth_headers, "unused-first-page-read-key"),
    )
    second = client.get(
        f"/api/v1/label-versions/{VERSION_V1}/items?status=active&limit=1&cursor={first_label_id}",
        headers=_headers(auth_headers, "unused-second-page-read-key"),
    )

    assert first.status_code == 200, first.text
    assert [item["label_id"] for item in first.json()["data"]["items"]] == [first_label_id]
    assert first.json()["meta"] == {
        "trace_id": first.json()["meta"]["trace_id"],
        "request_id": first.json()["meta"]["request_id"],
        "total": 2,
        "limit": 1,
        "next_cursor": first_label_id,
    }
    assert second.status_code == 200, second.text
    assert [item["label_id"] for item in second.json()["data"]["items"]] == [LABEL_V1]
    assert second.json()["meta"]["total"] == 2
    assert second.json()["meta"]["limit"] == 1
    assert second.json()["meta"]["next_cursor"] is None


def test_create_manual_label_draft_has_strict_envelope_and_exact_idempotency(
    client,
    auth_headers,
) -> None:
    _seed_release_scope()
    annotation_id = "annotation-manual-http-create"
    body = _create_body(annotation_id)
    headers = _headers(auth_headers, "manual-label-http-create-replay")

    first = client.post(_create_path(), json=body, headers=headers)
    replay = client.post(_create_path(), json=body, headers=headers)
    conflict = client.post(
        _create_path(),
        json={**body, "value": False},
        headers=headers,
    )

    assert first.status_code == 201, first.text
    assert replay.status_code == 201
    assert replay.json() == first.json()
    data, trace_id = _assert_envelope(first.json())
    assert data == {
        **data,
        "annotation_id": annotation_id,
        "audio_session_id": AUDIO_SESSION_ID,
        "event_or_segment_id": "segment-http-17",
        "evidence_sha256": "f" * 64,
        "label_id": LABEL_V1,
        "label_version_id": VERSION_V1,
        "occurred_at": OCCURRED_AT,
        "release_head_generation": 1,
        "status": "draft",
        "trace_id": trace_id,
    }
    assert isinstance(data["draft_sha256"], str)
    assert len(str(data["draft_sha256"])) == 64
    assert isinstance(data["audit_id"], int) and data["audit_id"] > 0
    assert isinstance(data["outbox_event_id"], int) and data["outbox_event_id"] > 0
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    with SessionLocal() as session:
        projection = session.get(ListeningAnnotation, annotation_id)
        assert projection is not None
        assert projection.status == "draft"
        assert projection.payload["draft_sha256"] == data["draft_sha256"]
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "manual_label_draft.created",
                    AuditLog.object_id == annotation_id,
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.event_type == "manual_label_draft.created",
                    OutboxEvent.aggregate_id == annotation_id,
                )
            )
            == 1
        )


def test_manual_label_write_requests_are_strict_and_role_guarded(
    client,
    auth_headers,
) -> None:
    _seed_release_scope()

    forged_create = client.post(
        _create_path(),
        json={
            **_create_body("annotation-manual-http-forged"),
            "draft_sha256": "0" * 64,
        },
        headers=_headers(auth_headers, "manual-label-http-forged-create"),
    )
    assert forged_create.status_code == 422
    assert "extra_forbidden" in forged_create.text

    forbidden = client.post(
        _create_path(),
        json=_create_body("annotation-manual-http-forbidden"),
        headers=_headers(
            auth_headers,
            "manual-label-http-forbidden",
            token="model-token",
        ),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"

    created = _create_draft(client, auth_headers, "annotation-manual-http-strict")
    forged_submission = client.post(
        _submission_path("annotation-manual-http-strict"),
        json={
            **_submit_body(str(created["draft_sha256"])),
            "fact_id": "forged-fact-id",
        },
        headers=_headers(auth_headers, "manual-label-http-forged-submission"),
    )
    assert forged_submission.status_code == 422
    assert "extra_forbidden" in forged_submission.text

    _switch_head_to_v2()
    _seed_mapping_bundle()
    forged_rebase = client.post(
        _rebase_path("annotation-manual-http-strict"),
        json={**_preview_body(), "can_confirm": True},
        headers=_headers(auth_headers, "manual-label-http-forged-rebase"),
    )
    assert forged_rebase.status_code == 422
    assert "extra_forbidden" in forged_rebase.text


def test_submit_current_draft_replays_exactly_and_materializes_human_fact(
    client,
    auth_headers,
) -> None:
    _seed_release_scope()
    annotation_id = "annotation-manual-http-submit"
    created_response = client.post(
        _create_path(),
        json=_create_body(annotation_id),
        headers=_headers(auth_headers, "manual-label-http-submit-create"),
    )
    assert created_response.status_code == 201, created_response.text
    created, create_trace_id = _assert_envelope(created_response.json())
    body = _submit_body(str(created["draft_sha256"]))
    headers = _headers(auth_headers, "manual-label-http-submit-replay")

    first = client.post(_submission_path(annotation_id), json=body, headers=headers)
    replay = client.post(_submission_path(annotation_id), json=body, headers=headers)
    conflict = client.post(
        _submission_path(annotation_id),
        json={**body, "expected_draft_sha256": "0" * 64},
        headers=headers,
    )

    assert first.status_code == 201, first.text
    assert replay.status_code == 201
    assert replay.json() == first.json()
    submitted, action_trace_id = _assert_envelope(first.json())
    assert submitted["annotation_id"] == annotation_id
    assert submitted["status"] == "submitted"
    assert submitted["draft_sha256"] == created["draft_sha256"]
    assert submitted["action_trace_id"] == action_trace_id
    assert submitted["root_trace_id"] == create_trace_id
    assert submitted["trace_id"] == create_trace_id
    assert isinstance(submitted["decision_id"], str)
    assert isinstance(submitted["fact_id"], str)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    with SessionLocal() as session:
        fact = session.get(LabelFact, str(submitted["fact_id"]))
        decision = session.get(HumanReviewDecision, str(submitted["decision_id"]))
        projection = session.get(ListeningAnnotation, annotation_id)
        assert fact is not None
        assert decision is not None
        assert projection is not None
        assert fact.authority == "human-confirmed"
        assert fact.source_kind == "human-decision"
        assert fact.label_version_id == VERSION_V1
        assert fact.label_id == LABEL_V1
        assert fact.value_json is True
        assert fact.occurred_at_origin == "source"
        assert fact.occurred_at is not None
        assert fact.occurred_at.replace(tzinfo=UTC) == datetime(
            2026,
            7,
            17,
            8,
            30,
            45,
            tzinfo=UTC,
        )
        assert fact.human_review_decision_id == decision.decision_id
        assert decision.payload["source"] == "manual-label-draft"
        head = session.scalar(
            select(LabelFactHead).where(LabelFactHead.current_fact_id == fact.fact_id)
        )
        assert head is not None
        assert head.current_revision == 1
        assert projection.status == "submitted"


def test_submission_rejects_stale_label_version_without_fact_side_effects(
    client,
    auth_headers,
) -> None:
    _seed_release_scope()
    annotation_id = "annotation-manual-http-stale"
    created = _create_draft(client, auth_headers, annotation_id)
    _switch_head_to_v2()

    response = client.post(
        _submission_path(annotation_id),
        json=_submit_body(str(created["draft_sha256"]), generation=2),
        headers=_headers(auth_headers, "manual-label-http-submit-stale"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_LABEL_VERSION"
    details = response.json()["error"]["details"]
    assert details[0]["current_label_version_id"] == VERSION_V2
    assert details[0]["draft_label_version_id"] == VERSION_V1
    assert details[0]["rebase_required"] is True
    with SessionLocal() as session:
        projection = session.get(ListeningAnnotation, annotation_id)
        assert projection is not None and projection.status == "draft"
        assert projection.payload["draft_sha256"] == created["draft_sha256"]
        assert session.scalar(select(func.count()).select_from(LabelFact)) == 0
        assert session.scalar(select(func.count()).select_from(LabelFactHead)) == 0
        assert session.scalar(select(func.count()).select_from(HumanReviewDecision)) == 0


def test_rebase_preview_is_read_only_and_confirm_creates_a_new_v2_draft(
    client,
    auth_headers,
) -> None:
    _seed_release_scope()
    old_annotation_id = "annotation-manual-http-rebase-old"
    _create_draft(client, auth_headers, old_annotation_id)
    _switch_head_to_v2()
    _seed_mapping_bundle()
    with SessionLocal() as session:
        old_before = deepcopy(session.get(ListeningAnnotation, old_annotation_id).payload)  # type: ignore[union-attr]
        annotation_count = session.scalar(select(func.count()).select_from(ListeningAnnotation))
        audit_count = session.scalar(select(func.count()).select_from(AuditLog))
        outbox_count = session.scalar(select(func.count()).select_from(OutboxEvent))

    preview_headers = _headers(auth_headers, "manual-label-http-rebase-preview")
    preview = client.post(
        _rebase_path(old_annotation_id),
        json=_preview_body(),
        headers=preview_headers,
    )
    preview_replay = client.post(
        _rebase_path(old_annotation_id),
        json=_preview_body(),
        headers=preview_headers,
    )
    preview_conflict = client.post(
        _rebase_path(old_annotation_id),
        json={**_preview_body(), "mapping_bundle_id": "mapping-bundle-other"},
        headers=preview_headers,
    )

    assert preview.status_code == 200, preview.text
    assert preview_replay.status_code == 200
    assert preview_replay.json() == preview.json()
    preview_data, _preview_trace = _assert_envelope(preview.json())
    assert preview_data["status"] == "preview"
    assert preview_data["can_confirm"] is True
    assert isinstance(preview_data["preview_sha256"], str)
    mapping_diff = preview_data["preview"]
    assert isinstance(mapping_diff, dict)
    assert mapping_diff["old_label_version_id"] == VERSION_V1
    assert mapping_diff["new_label_version_id"] == VERSION_V2
    assert mapping_diff["old_label_id"] == LABEL_V1
    assert mapping_diff["new_label_id"] == LABEL_V2
    assert mapping_diff["requires_manual_selection"] is False
    assert preview_conflict.status_code == 409
    assert preview_conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(ListeningAnnotation)) == (
            annotation_count
        )
        assert session.scalar(select(func.count()).select_from(AuditLog)) == audit_count
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == outbox_count

    new_annotation_id = "annotation-manual-http-rebase-v2"
    confirm_body = {
        "action": "confirm",
        "mapping_bundle_id": MAPPING_BUNDLE_ID,
        "expected_release_head_generation": 2,
        "new_annotation_id": new_annotation_id,
        "preview_sha256": preview_data["preview_sha256"],
        "confirmation": "confirm-reviewed-manual-label-rebase",
    }
    confirm_headers = _headers(auth_headers, "manual-label-http-rebase-confirm")
    confirmed = client.post(
        _rebase_path(old_annotation_id),
        json=confirm_body,
        headers=confirm_headers,
    )
    confirm_replay = client.post(
        _rebase_path(old_annotation_id),
        json=confirm_body,
        headers=confirm_headers,
    )
    confirm_conflict = client.post(
        _rebase_path(old_annotation_id),
        json={**confirm_body, "new_annotation_id": "annotation-manual-http-rebase-other"},
        headers=confirm_headers,
    )

    assert confirmed.status_code == 201, confirmed.text
    assert confirm_replay.status_code == 201
    assert confirm_replay.json() == confirmed.json()
    confirmed_data, _confirm_trace = _assert_envelope(confirmed.json())
    assert confirmed_data["status"] == "draft"
    assert confirmed_data["annotation_id"] == new_annotation_id
    assert confirmed_data["new_annotation_id"] == new_annotation_id
    assert confirmed_data["label_version_id"] == VERSION_V2
    assert confirmed_data["label_id"] == LABEL_V2
    assert confirmed_data["preview_sha256"] == preview_data["preview_sha256"]
    assert confirm_conflict.status_code == 409
    assert confirm_conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    with SessionLocal() as session:
        old = session.get(ListeningAnnotation, old_annotation_id)
        rebased = session.get(ListeningAnnotation, new_annotation_id)
        assert old is not None and old.status == "draft"
        assert old.payload == old_before
        assert rebased is not None and rebased.status == "draft"
        assert rebased.payload["draft_document"]["label_version_id"] == VERSION_V2
        assert rebased.payload["draft_document"]["label_id"] == LABEL_V2
        assert rebased.payload["draft_document"]["occurred_at"] == OCCURRED_AT
        assert rebased.payload["rebase_provenance"] == {
            "mapping_bundle_id": MAPPING_BUNDLE_ID,
            "old_annotation_id": old_annotation_id,
            "preview_sha256": preview_data["preview_sha256"],
        }


def test_generic_annotation_upsert_cannot_overwrite_manual_label_draft(
    client,
    auth_headers,
) -> None:
    _seed_release_scope()
    annotation_id = "annotation-manual-http-immutable"
    created = _create_draft(client, auth_headers, annotation_id)
    with SessionLocal() as session:
        frozen_before = deepcopy(session.get(ListeningAnnotation, annotation_id).payload)  # type: ignore[union-attr]

    response = client.post(
        _create_path(),
        json={
            "annotation_id": annotation_id,
            "track": "qa",
            "label": "attempted overwrite",
            "value": False,
        },
        headers=_headers(auth_headers, "manual-label-http-generic-overwrite"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MANUAL_LABEL_DRAFT_IMMUTABLE"
    with SessionLocal() as session:
        projection = session.get(ListeningAnnotation, annotation_id)
        assert projection is not None
        assert projection.status == "draft"
        assert projection.payload == frozen_before
        assert projection.payload["draft_sha256"] == created["draft_sha256"]


def test_manual_label_draft_is_not_visible_or_submittable_across_project_scope(
    client,
    auth_headers,
) -> None:
    _seed_release_scope()
    annotation_id = "annotation-manual-http-scope"
    created = _create_draft(client, auth_headers, annotation_id)
    with SessionLocal.begin() as session:
        session.add(
            Project(
                project_id=OTHER_PROJECT_ID,
                tenant_id=TENANT_ID,
                name="Manual Label Other Project",
                status="active",
                data={"member_user_ids": ["u_admin_001"]},
            )
        )
        session.add(
            JsonResource(
                collection="audio_sessions",
                resource_key=AUDIO_SESSION_ID,
                tenant_id=TENANT_ID,
                project_id=OTHER_PROJECT_ID,
                status="success",
                trace_id="trace-manual-label-other-project-audio",
                data={
                    "audio_session_id": AUDIO_SESSION_ID,
                    "status": "success",
                },
            )
        )

    response = client.post(
        _submission_path(annotation_id),
        json=_submit_body(str(created["draft_sha256"])),
        headers=_headers(
            auth_headers,
            "manual-label-http-cross-scope-submit",
            project_id=OTHER_PROJECT_ID,
        ),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MANUAL_LABEL_DRAFT_NOT_FOUND"
    assert annotation_id not in response.text
    with SessionLocal() as session:
        projection = session.get(ListeningAnnotation, annotation_id)
        assert projection is not None and projection.status == "draft"
        assert session.scalar(select(func.count()).select_from(LabelFact)) == 0
