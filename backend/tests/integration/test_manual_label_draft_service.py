from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.domain.label_mapping import sha256_document
from app.models import (
    AuditLog,
    HumanReviewDecision,
    HumanReviewTask,
    JsonResource,
    LabelAggregate,
    LabelFact,
    LabelFactHead,
    LabelMappingBundle,
    LabelMappingBundlePath,
    LabelTaxonomy,
    LabelVersion,
    LabelVersionItem,
    ListeningAnnotation,
    OutboxEvent,
    ReleaseBundleHead,
    ReleaseDeployment,
)
from app.schemas.manual_label_drafts import (
    ManualLabelDraftCreateRequest,
    ManualLabelDraftRebaseRequest,
    ManualLabelDraftSubmitRequest,
    ManualLabelEvidenceRef,
)
from app.services.manual_label_draft_service import (
    create_manual_label_draft,
    rebase_manual_label_draft,
    submit_manual_label_draft,
)

TENANT_ID = "tenant_manual_label"
PROJECT_ID = "project_manual_label"
OTHER_PROJECT_ID = "project_manual_label_other"
AUDIO_SESSION_ID = "audio-session-manual-label"
TAXONOMY_ID = "taxonomy-manual-label"
VERSION_V1 = "label-version-manual-v1"
VERSION_V2 = "label-version-manual-v2"
LABEL_V1 = "purchase-intent-legacy"
LABEL_V2 = "purchase-intent"
LABEL_V2_NUMERIC = "purchase-intent-score"
DEPLOYMENT_V1 = "deployment-manual-v1"
DEPLOYMENT_V2 = "deployment-manual-v2"
RELEASE_HEAD_ID = "release-head-manual-production"
BUNDLE_V1_V2 = "mapping-bundle-manual-v1-v2"
BUNDLE_V1_NUMERIC = "mapping-bundle-manual-v1-numeric"
OCCURRED_AT = datetime(2026, 7, 17, 8, 30, 45, tzinfo=UTC)


def _ctx(
    key: str,
    *,
    project_id: str = PROJECT_ID,
    trace_id: str | None = None,
) -> RequestContext:
    return RequestContext(
        tenant_id=TENANT_ID,
        project_id=project_id,
        user_id="manual-label-reviewer",
        roles=("project_admin", "review_arbitrator"),
        request_id=f"request-{key}",
        trace_id=trace_id or f"trace-{key}",
        idempotency_key=key,
        actor_kind="human",
    )


def _deployment(deployment_id: str, label_version_id: str, suffix: str) -> ReleaseDeployment:
    return ReleaseDeployment(
        deployment_id=deployment_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        environment="production",
        status="success",
        stage="production",
        label_version_id=label_version_id,
        prompt_version_id=f"prompt-{suffix}",
        model_version=f"model-{suffix}",
        aggregation_policy_version_id=f"policy-{suffix}",
        eval_dataset_version_id=f"dataset-{suffix}",
        eval_run_id=f"eval-run-{suffix}",
        bundle_sha256=("1" if suffix == "v1" else "2") * 64,
        rollout_percentage=100,
        blocked_reasons=[],
        monitor_metrics={},
        approved_by="manual-label-reviewer",
        trace_id=f"trace-deployment-{suffix}",
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
    value_type: str,
) -> LabelVersionItem:
    return LabelVersionItem(
        label_version_item_id=item_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        label_version_id=label_version_id,
        label_id=label_id,
        canonical_name=label_id,
        aliases=[],
        value_type=value_type,
        risk_level="medium",
        parent_ids=[],
        aggregation_rule={"kind": "manual"},
        status="active",
        definition_sha256=sha256_document([label_version_id, label_id, value_type]),
        trace_id=f"trace-item-{item_id}",
    )


def _seed_release_scope() -> None:
    with SessionLocal.begin() as session:
        session.add(
            LabelTaxonomy(
                taxonomy_id=TAXONOMY_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                name="Manual label taxonomy",
                description="manual draft integration fixture",
                status="active",
                resource_version=1,
                content_sha256="a" * 64,
                trace_id="trace-taxonomy-manual",
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
                _version_item("item-manual-v1", VERSION_V1, LABEL_V1, "boolean"),
                _version_item("item-manual-v2", VERSION_V2, LABEL_V2, "boolean"),
                _version_item(
                    "item-manual-v2-numeric",
                    VERSION_V2,
                    LABEL_V2_NUMERIC,
                    "numeric",
                ),
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
                prompt_asset_id="prompt-asset-v1",
                prompt_version_id="prompt-v1",
                label_version_id=VERSION_V1,
                model_version="model-v1",
                aggregation_policy_version_id="policy-v1",
                eval_dataset_version_id="dataset-v1",
                generation=1,
                status="active",
                bootstrapped=True,
                trace_id="trace-release-head-v1",
                payload={"fixture": True},
            )
        )


def _switch_head_to_v2() -> None:
    with SessionLocal.begin() as session:
        head = session.get(ReleaseBundleHead, RELEASE_HEAD_ID)
        assert head is not None
        head.active_deployment_id = DEPLOYMENT_V2
        head.active_bundle_sha256 = "2" * 64
        head.prompt_asset_id = "prompt-asset-v2"
        head.prompt_version_id = "prompt-v2"
        head.label_version_id = VERSION_V2
        head.model_version = "model-v2"
        head.aggregation_policy_version_id = "policy-v2"
        head.eval_dataset_version_id = "dataset-v2"
        head.generation = 2
        head.trace_id = "trace-release-head-v2"


def _create_request(
    annotation_id: str,
    *,
    label_version_id: str = VERSION_V1,
    label_id: str = LABEL_V1,
    expected_generation: int = 1,
) -> ManualLabelDraftCreateRequest:
    return ManualLabelDraftCreateRequest(
        annotation_id=annotation_id,
        label_version_id=label_version_id,
        label_id=label_id,
        subject_scope="business-event",
        subject_key="business-event-4242",
        event_or_segment_id="segment-17",
        assertion_slot="agent-purchase-intent",
        occurred_at=OCCURRED_AT,
        evidence_ref=ManualLabelEvidenceRef(
            type="audio-segment",
            id="audio-session-manual-label:segment-17",
            sha256="e" * 64,
            start_ms=1250,
            end_ms=8840,
        ),
        value_type="boolean",
        value=True,
        environment="production",
        expected_release_head_generation=expected_generation,
    )


def _create_draft(annotation_id: str = "annotation-manual-v1") -> dict[str, object]:
    with SessionLocal.begin() as session:
        return create_manual_label_draft(
            session,
            _ctx(f"create-{annotation_id}", trace_id=f"root-trace-{annotation_id}"),
            audio_session_id=AUDIO_SESSION_ID,
            request=_create_request(annotation_id),
        )


def _submit_request(draft_sha256: str, generation: int) -> ManualLabelDraftSubmitRequest:
    return ManualLabelDraftSubmitRequest(
        expected_draft_sha256=draft_sha256,
        expected_release_head_generation=generation,
        confirmation="submit-frozen-manual-label",
    )


def _count(
    session: Session,
    model: type[object],
    *criteria: ColumnElement[bool],
) -> int:
    statement = select(func.count()).select_from(model)
    for criterion in criteria:
        statement = statement.where(criterion)
    return int(session.scalar(statement) or 0)


def _seed_mapping_bundle(
    *,
    mapping_bundle_id: str = BUNDLE_V1_V2,
    target_label_id: str = LABEL_V2,
    value_marker: str = "d",
) -> None:
    with SessionLocal.begin() as session:
        session.add(
            LabelMappingBundle(
                mapping_bundle_id=mapping_bundle_id,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                target_label_version_id=VERSION_V2,
                source_label_version_ids=[VERSION_V1],
                source_manifest_sha256=value_marker * 64,
                compiler_version="manual-test-compiler/1",
                status="published",
                resource_version=1,
                canonical_manifest_sha256=value_marker * 64,
                approval_id=f"approval-{mapping_bundle_id}",
                approved_by="manual-label-reviewer",
                approved_at=datetime(2026, 7, 18, 1, tzinfo=UTC),
                published_at=datetime(2026, 7, 18, 2, tzinfo=UTC),
                root_trace_id=f"root-trace-{mapping_bundle_id}",
                trace_id=f"trace-{mapping_bundle_id}",
                payload={"fixture": True},
            )
        )
        session.flush()
        session.add(
            LabelMappingBundlePath(
                bundle_path_id=f"path-{mapping_bundle_id}",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                mapping_bundle_id=mapping_bundle_id,
                source_label_version_id=VERSION_V1,
                target_label_version_id=VERSION_V2,
                source_label_id=LABEL_V1,
                target_label_id=target_label_id,
                metric_family="manual-label",
                relation_path=[
                    {
                        "relation_type": "rename",
                        "source_label_id": LABEL_V1,
                        "target_label_id": target_label_id,
                    }
                ],
                mapping_version_ids=[f"mapping-version-{mapping_bundle_id}"],
                metric_grain="event",
                lineage_key="manual-label-lineage",
                reducer=None,
                comparability_status="comparable",
                requires_recompute=False,
                path_sha256=sha256_document([mapping_bundle_id, LABEL_V1, target_label_id]),
                trace_id=f"trace-path-{mapping_bundle_id}",
                payload={"fixture": True},
            )
        )


def _preview_request(
    mapping_bundle_id: str = BUNDLE_V1_V2,
    *,
    target_label_id: str | None = None,
) -> ManualLabelDraftRebaseRequest:
    return ManualLabelDraftRebaseRequest(
        action="preview",
        mapping_bundle_id=mapping_bundle_id,
        target_label_id=target_label_id,
        expected_release_head_generation=2,
    )


def _confirm_request(
    mapping_bundle_id: str,
    preview_sha256: str,
    new_annotation_id: str,
    *,
    target_label_id: str | None = None,
) -> ManualLabelDraftRebaseRequest:
    return ManualLabelDraftRebaseRequest(
        action="confirm",
        mapping_bundle_id=mapping_bundle_id,
        target_label_id=target_label_id,
        expected_release_head_generation=2,
        new_annotation_id=new_annotation_id,
        preview_sha256=preview_sha256,
        confirmation="confirm-reviewed-manual-label-rebase",
    )


def test_create_draft_freezes_release_binding_content_hash_audit_and_outbox() -> None:
    _seed_release_scope()
    request = _create_request("annotation-create-freeze")
    ctx = _ctx("create-freeze", trace_id="root-trace-create-freeze")

    with SessionLocal.begin() as session:
        response = create_manual_label_draft(
            session,
            ctx,
            audio_session_id=AUDIO_SESSION_ID,
            request=request,
        )

    with SessionLocal() as session:
        projection = session.get(ListeningAnnotation, request.annotation_id)
        assert projection is not None
        assert projection.status == "draft"
        payload = projection.payload
        document = payload["draft_document"]
        assert document == {
            **request.model_dump(mode="json"),
            "annotation_kind": "label-fact-draft",
            "audio_session_id": AUDIO_SESSION_ID,
            "schema_version": "auris.manual-label-draft/1",
        }
        assert payload["draft_sha256"] == sha256_document(document)
        assert payload["release_head_id"] == RELEASE_HEAD_ID
        assert payload["release_head_generation"] == 1
        assert payload["release_bundle_sha256"] == "1" * 64
        assert payload["root_trace_id"] == ctx.trace_id
        assert response["draft_sha256"] == payload["draft_sha256"]
        assert response["evidence_sha256"] == "e" * 64

        resource = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == TENANT_ID,
                JsonResource.project_id == PROJECT_ID,
                JsonResource.collection == "listening_annotations",
                JsonResource.resource_key == request.annotation_id,
            )
        )
        assert resource is not None
        assert resource.status == "draft"
        assert resource.data == payload
        audit = session.scalar(
            select(AuditLog).where(
                AuditLog.action == "manual_label_draft.created",
                AuditLog.object_id == request.annotation_id,
            )
        )
        assert audit is not None
        assert audit.trace_id == ctx.trace_id
        assert audit.after_json is not None
        assert audit.after_json["draft_sha256"] == payload["draft_sha256"]
        outbox = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "manual_label_draft.created",
                OutboxEvent.aggregate_id == request.annotation_id,
            )
        )
        assert outbox is not None
        assert outbox.payload["label_version_id"] == VERSION_V1


def test_submit_current_draft_materializes_human_decision_and_bitemporal_fact() -> None:
    _seed_release_scope()
    created = _create_draft("annotation-submit-current")
    submit_ctx = _ctx("submit-current", trace_id="action-trace-submit-current")

    with SessionLocal.begin() as session:
        response = submit_manual_label_draft(
            session,
            submit_ctx,
            audio_session_id=AUDIO_SESSION_ID,
            annotation_id="annotation-submit-current",
            request=_submit_request(str(created["draft_sha256"]), 1),
        )

    with SessionLocal() as session:
        decision = session.get(HumanReviewDecision, response["decision_id"])
        fact = session.get(LabelFact, response["fact_id"])
        projection = session.get(ListeningAnnotation, "annotation-submit-current")
        assert decision is not None
        assert fact is not None
        assert projection is not None
        assert decision.status == "success"
        assert decision.payload["decision"] == "accepted"
        assert decision.payload["source"] == "manual-label-draft"
        assert (
            decision.payload["after_json"]["targets"][f"label_aggregates:{fact.aggregate_id}"][
                "value"
            ]
            is True
        )
        assert fact.human_review_decision_id == decision.decision_id
        assert fact.review_decision_id == decision.decision_id
        assert fact.authority == "human-confirmed"
        assert fact.source_kind == "human-decision"
        assert fact.fact_namespace == "production"
        assert fact.revision == 1
        assert fact.status == "active"
        assert fact.active_slot == "active"
        assert fact.label_version_id == VERSION_V1
        assert fact.label_id == LABEL_V1
        assert fact.value_json is True
        assert fact.event_or_segment_id == "segment-17"
        assert fact.assertion_slot == "agent-purchase-intent"
        assert fact.occurred_at is not None
        assert fact.occurred_at.replace(tzinfo=UTC) == OCCURRED_AT
        assert fact.occurred_at_origin == "source"
        assert fact.recorded_at is not None
        assert fact.recorded_at.replace(tzinfo=UTC) > OCCURRED_AT
        assert fact.logical_key_sha is not None and len(fact.logical_key_sha) == 64
        assert fact.content_sha256 is not None and len(fact.content_sha256) == 64
        assert fact.root_trace_id == "root-trace-annotation-submit-current"
        assert fact.action_trace_id == submit_ctx.trace_id
        head = session.scalar(
            select(LabelFactHead).where(LabelFactHead.current_fact_id == fact.fact_id)
        )
        assert head is not None
        assert head.current_revision == 1
        assert head.generation == 1
        assert projection.status == "submitted"
        assert projection.payload["status"] == "submitted"
        assert projection.payload["draft_sha256"] == created["draft_sha256"]
        assert projection.payload["draft_document"]["occurred_at"] == "2026-07-17T08:30:45Z"
        assert (
            _count(
                session,
                AuditLog,
                AuditLog.action == "manual_label_draft.submitted",
                AuditLog.object_id == projection.annotation_id,
            )
            == 1
        )
        assert (
            _count(
                session,
                OutboxEvent,
                OutboxEvent.event_type == "manual_label_draft.submitted",
                OutboxEvent.aggregate_id == projection.annotation_id,
            )
            == 1
        )


def test_same_subject_label_on_distinct_events_keeps_two_authoritative_heads() -> None:
    _seed_release_scope()
    requests = [
        _create_request("annotation-event-17"),
        _create_request("annotation-event-18").model_copy(
            update={
                "event_or_segment_id": "segment-18",
                "evidence_ref": ManualLabelEvidenceRef(
                    type="audio-segment",
                    id="audio-session-manual-label:segment-18",
                    sha256="f" * 64,
                    start_ms=9000,
                    end_ms=13000,
                ),
            }
        ),
    ]
    created: list[dict[str, object]] = []
    with SessionLocal.begin() as session:
        for request in requests:
            created.append(
                create_manual_label_draft(
                    session,
                    _ctx(
                        f"create-{request.annotation_id}",
                        trace_id=f"root-trace-{request.annotation_id}",
                    ),
                    audio_session_id=AUDIO_SESSION_ID,
                    request=request,
                )
            )
    with SessionLocal.begin() as session:
        for request, draft in zip(requests, created, strict=True):
            submit_manual_label_draft(
                session,
                _ctx(f"submit-{request.annotation_id}"),
                audio_session_id=AUDIO_SESSION_ID,
                annotation_id=request.annotation_id,
                request=_submit_request(str(draft["draft_sha256"]), 1),
            )

    with SessionLocal() as session:
        facts = list(
            session.scalars(
                select(LabelFact).where(
                    LabelFact.tenant_id == TENANT_ID,
                    LabelFact.project_id == PROJECT_ID,
                    LabelFact.subject_key == "business-event-4242",
                    LabelFact.label_id == LABEL_V1,
                )
            )
        )
        assert {fact.event_or_segment_id for fact in facts} == {"segment-17", "segment-18"}
        assert {fact.status for fact in facts} == {"active"}
        assert len({fact.logical_key_sha for fact in facts}) == 2
        assert (
            _count(
                session,
                LabelFactHead,
                LabelFactHead.tenant_id == TENANT_ID,
                LabelFactHead.project_id == PROJECT_ID,
            )
            == 2
        )


def test_stale_draft_submission_has_no_authoritative_side_effects() -> None:
    _seed_release_scope()
    created = _create_draft("annotation-stale-v1")
    _switch_head_to_v2()

    with SessionLocal() as session:
        frozen_before = deepcopy(
            session.get(ListeningAnnotation, "annotation-stale-v1").payload  # type: ignore[union-attr]
        )
        with pytest.raises(ApiError) as error:
            submit_manual_label_draft(
                session,
                _ctx("submit-stale"),
                audio_session_id=AUDIO_SESSION_ID,
                annotation_id="annotation-stale-v1",
                request=_submit_request(str(created["draft_sha256"]), 2),
            )
        session.rollback()
        assert error.value.code == "STALE_LABEL_VERSION"
        assert error.value.status_code == 409

    with SessionLocal() as session:
        projection = session.get(ListeningAnnotation, "annotation-stale-v1")
        assert projection is not None
        assert projection.status == "draft"
        assert projection.payload == frozen_before
        assert (
            _count(
                session,
                LabelAggregate,
                LabelAggregate.tenant_id == TENANT_ID,
                LabelAggregate.project_id == PROJECT_ID,
            )
            == 0
        )
        assert (
            _count(
                session,
                HumanReviewTask,
                HumanReviewTask.tenant_id == TENANT_ID,
                HumanReviewTask.project_id == PROJECT_ID,
            )
            == 0
        )
        assert (
            _count(
                session,
                HumanReviewDecision,
                HumanReviewDecision.tenant_id == TENANT_ID,
                HumanReviewDecision.project_id == PROJECT_ID,
            )
            == 0
        )
        assert (
            _count(
                session,
                LabelFact,
                LabelFact.tenant_id == TENANT_ID,
                LabelFact.project_id == PROJECT_ID,
            )
            == 0
        )
        assert (
            _count(
                session,
                LabelFactHead,
                LabelFactHead.tenant_id == TENANT_ID,
                LabelFactHead.project_id == PROJECT_ID,
            )
            == 0
        )
        assert (
            _count(
                session,
                AuditLog,
                AuditLog.action == "manual_label_draft.submitted",
            )
            == 0
        )
        assert (
            _count(
                session,
                OutboxEvent,
                OutboxEvent.event_type == "manual_label_draft.submitted",
            )
            == 0
        )


def test_rebase_preview_is_read_only_confirm_creates_v2_draft_and_it_can_submit() -> None:
    _seed_release_scope()
    _create_draft("annotation-rebase-old")
    _switch_head_to_v2()
    _seed_mapping_bundle()

    with SessionLocal.begin() as session:
        old_before = deepcopy(
            session.get(ListeningAnnotation, "annotation-rebase-old").payload  # type: ignore[union-attr]
        )
        counts_before = {
            "annotations": _count(session, ListeningAnnotation),
            "audits": _count(session, AuditLog),
            "outbox": _count(session, OutboxEvent),
        }
        preview = rebase_manual_label_draft(
            session,
            _ctx("rebase-preview"),
            audio_session_id=AUDIO_SESSION_ID,
            annotation_id="annotation-rebase-old",
            request=_preview_request(),
        )
        assert preview["status"] == "preview"
        assert preview["can_confirm"] is True
        assert preview["preview_sha256"] == sha256_document(preview["preview"])
        assert preview["preview"]["old_label_version_id"] == VERSION_V1
        assert preview["preview"]["new_label_version_id"] == VERSION_V2
        assert preview["preview"]["old_label_id"] == LABEL_V1
        assert preview["preview"]["new_label_id"] == LABEL_V2
        assert preview["preview"]["mapping_paths"][0]["comparability_status"] == ("comparable")
        assert _count(session, ListeningAnnotation) == counts_before["annotations"]
        assert _count(session, AuditLog) == counts_before["audits"]
        assert _count(session, OutboxEvent) == counts_before["outbox"]

    with SessionLocal.begin() as session:
        confirmed = rebase_manual_label_draft(
            session,
            _ctx("rebase-confirm"),
            audio_session_id=AUDIO_SESSION_ID,
            annotation_id="annotation-rebase-old",
            request=_confirm_request(
                BUNDLE_V1_V2,
                str(preview["preview_sha256"]),
                "annotation-rebase-v2",
            ),
        )
        assert confirmed["status"] == "draft"
        assert confirmed["label_version_id"] == VERSION_V2
        assert confirmed["label_id"] == LABEL_V2
        assert confirmed["preview_sha256"] == preview["preview_sha256"]

    with SessionLocal() as session:
        old = session.get(ListeningAnnotation, "annotation-rebase-old")
        rebased = session.get(ListeningAnnotation, "annotation-rebase-v2")
        assert old is not None
        assert rebased is not None
        assert old.status == "draft"
        assert old.payload == old_before
        assert rebased.status == "draft"
        assert rebased.payload["draft_document"]["label_version_id"] == VERSION_V2
        assert rebased.payload["draft_document"]["label_id"] == LABEL_V2
        assert rebased.payload["draft_document"]["value"] is True
        assert rebased.payload["draft_document"]["occurred_at"] == "2026-07-17T08:30:45Z"
        assert rebased.payload["release_head_generation"] == 2
        assert rebased.payload["release_bundle_sha256"] == "2" * 64
        assert rebased.payload["rebase_provenance"] == {
            "mapping_bundle_id": BUNDLE_V1_V2,
            "old_annotation_id": "annotation-rebase-old",
            "preview_sha256": preview["preview_sha256"],
        }
        rebased_sha256 = rebased.payload["draft_sha256"]

    with SessionLocal.begin() as session:
        submitted = submit_manual_label_draft(
            session,
            _ctx("submit-rebased"),
            audio_session_id=AUDIO_SESSION_ID,
            annotation_id="annotation-rebase-v2",
            request=_submit_request(rebased_sha256, 2),
        )

    with SessionLocal() as session:
        fact = session.get(LabelFact, submitted["fact_id"])
        old = session.get(ListeningAnnotation, "annotation-rebase-old")
        rebased = session.get(ListeningAnnotation, "annotation-rebase-v2")
        assert fact is not None
        assert fact.label_version_id == VERSION_V2
        assert fact.label_id == LABEL_V2
        assert fact.value_json is True
        assert fact.occurred_at is not None
        assert fact.occurred_at.replace(tzinfo=UTC) == OCCURRED_AT
        assert old is not None and old.status == "draft"
        assert rebased is not None and rebased.status == "submitted"


def test_rebase_failures_are_scope_bound_and_atomic() -> None:
    _seed_release_scope()
    _create_draft("annotation-rebase-negative")
    _switch_head_to_v2()
    _seed_mapping_bundle()
    _seed_mapping_bundle(
        mapping_bundle_id=BUNDLE_V1_NUMERIC,
        target_label_id=LABEL_V2_NUMERIC,
        value_marker="f",
    )

    with SessionLocal.begin() as session:
        preview = rebase_manual_label_draft(
            session,
            _ctx("negative-preview"),
            audio_session_id=AUDIO_SESSION_ID,
            annotation_id="annotation-rebase-negative",
            request=_preview_request(),
        )
        assert preview["status"] == "preview"

    with SessionLocal() as session:
        baseline = {
            "annotations": _count(session, ListeningAnnotation),
            "audits": _count(session, AuditLog),
            "outbox": _count(session, OutboxEvent),
        }
        with pytest.raises(ApiError) as hash_error:
            rebase_manual_label_draft(
                session,
                _ctx("negative-bad-sha"),
                audio_session_id=AUDIO_SESSION_ID,
                annotation_id="annotation-rebase-negative",
                request=_confirm_request(
                    BUNDLE_V1_V2,
                    "0" * 64,
                    "annotation-should-not-exist-bad-sha",
                ),
            )
        session.rollback()
        assert hash_error.value.code == "MANUAL_LABEL_REBASE_PREVIEW_CONFLICT"

    with SessionLocal() as session:
        with pytest.raises(ApiError) as scope_error:
            rebase_manual_label_draft(
                session,
                _ctx("negative-cross-scope", project_id=OTHER_PROJECT_ID),
                audio_session_id=AUDIO_SESSION_ID,
                annotation_id="annotation-rebase-negative",
                request=_preview_request(),
            )
        session.rollback()
        assert scope_error.value.code == "MANUAL_LABEL_DRAFT_NOT_FOUND"

    with SessionLocal.begin() as session:
        numeric_preview = rebase_manual_label_draft(
            session,
            _ctx("numeric-preview"),
            audio_session_id=AUDIO_SESSION_ID,
            annotation_id="annotation-rebase-negative",
            request=_preview_request(BUNDLE_V1_NUMERIC),
        )
        assert numeric_preview["can_confirm"] is True

    with SessionLocal() as session:
        with pytest.raises(ApiError) as value_type_error:
            rebase_manual_label_draft(
                session,
                _ctx("negative-value-type"),
                audio_session_id=AUDIO_SESSION_ID,
                annotation_id="annotation-rebase-negative",
                request=_confirm_request(
                    BUNDLE_V1_NUMERIC,
                    str(numeric_preview["preview_sha256"]),
                    "annotation-should-not-exist-value-type",
                ),
            )
        session.rollback()
        assert value_type_error.value.code == "MANUAL_LABEL_REBASE_VALUE_TYPE_CHANGED"

    with SessionLocal() as session:
        assert _count(session, ListeningAnnotation) == baseline["annotations"]
        assert _count(session, AuditLog) == baseline["audits"]
        assert _count(session, OutboxEvent) == baseline["outbox"]
        assert session.get(ListeningAnnotation, "annotation-should-not-exist-bad-sha") is None
        assert session.get(ListeningAnnotation, "annotation-should-not-exist-value-type") is None
        original = session.get(ListeningAnnotation, "annotation-rebase-negative")
        assert original is not None and original.status == "draft"
