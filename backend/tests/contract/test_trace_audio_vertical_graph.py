from __future__ import annotations

from pathlib import Path

import yaml

from app.core.database import SessionLocal
from app.models import (
    AudioRecording,
    EvidencePack,
    ExternalCallbackReceipt,
    HumanReviewDecision,
    HumanReviewTask,
    ImportBatch,
    ImportBatchItem,
    JsonResource,
    RunRecord,
    StorageObject,
)

OPENAPI_PATH = Path(__file__).resolve().parents[3] / "doc/backend-spec/openapi-v0.1.yaml"


def _run(
    *,
    run_id: str,
    run_type: str,
    trace_id: str,
    root_trace_id: str,
    payload: dict,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        tenant_id="aurora_auto",
        project_id="sales_qa",
        run_type=run_type,
        status="success",
        trace_id=trace_id,
        payload={
            "run_id": run_id,
            "status": "success",
            "root_trace_id": root_trace_id,
            **payload,
        },
    )


def test_audio_vertical_trace_returns_scoped_stably_ordered_strong_resource_graph(
    client,
    auth_headers,
) -> None:
    root_trace_id = "trace_audio_vertical_graph_contract"
    import_run_id = "run_trace_graph_import"
    intelligence_run_id = "run_trace_graph_intelligence"
    callback_run_id = "run_trace_graph_callback"
    import_batch_id = "batch_trace_graph"
    import_item_id = "item_trace_graph"
    storage_object_id = "object_trace_graph"
    recording_id = "recording_trace_graph"
    audio_session_id = "session_trace_graph"
    asr_result_id = f"{audio_session_id}:asr:{intelligence_run_id}"
    evidence_pack_id = "evidence_trace_graph"
    review_task_id = "review_trace_graph"
    decision_id = "decision_trace_graph"
    callback_receipt_id = "callback_trace_graph"

    # Add deliberately out of lifecycle order. The API contract, rather than
    # database insertion order, owns the public graph ordering.
    with SessionLocal.begin() as session:
        session.add_all(
            [
                ExternalCallbackReceipt(
                    callback_receipt_id=callback_receipt_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="success",
                    trace_id="trace_callback_action",
                    payload={
                        "run_id": callback_run_id,
                        "root_trace_id": root_trace_id,
                    },
                ),
                HumanReviewDecision(
                    decision_id=decision_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    review_task_id=review_task_id,
                    terminal_review_task_id=review_task_id,
                    status="accepted",
                    trace_id=root_trace_id,
                    payload={
                        "review_task_id": review_task_id,
                        "evidence_pack_id": evidence_pack_id,
                        "root_trace_id": root_trace_id,
                    },
                ),
                HumanReviewTask(
                    review_task_id=review_task_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="completed",
                    trace_id=root_trace_id,
                    payload={
                        "queue": "audio_evidence_review",
                        "audio_session_id": audio_session_id,
                        "evidence_pack_id": evidence_pack_id,
                        "root_trace_id": root_trace_id,
                    },
                ),
                EvidencePack(
                    evidence_pack_id=evidence_pack_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    audio_session_id=audio_session_id,
                    recording_id=recording_id,
                    storage_object_id=storage_object_id,
                    storage_object_version="version-trace-graph",
                    audio_sha256="a" * 64,
                    asr_result_id=asr_result_id,
                    asr_result_version="audio-intelligence-result/1",
                    window_start_ms=0,
                    window_end_ms=1200,
                    evidence_sha256="b" * 64,
                    status="ready",
                    source_run_id=intelligence_run_id,
                    resource_version=1,
                    root_trace_id=root_trace_id,
                    current_trace_id="trace_intelligence_action",
                    payload={"root_trace_id": root_trace_id},
                ),
                JsonResource(
                    collection="asr_segments",
                    resource_key=asr_result_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="success",
                    trace_id=root_trace_id,
                    data={
                        "id": asr_result_id,
                        "audio_session_id": audio_session_id,
                        "recording_id": recording_id,
                        "source_run_id": intelligence_run_id,
                        "root_trace_id": root_trace_id,
                    },
                ),
                _run(
                    run_id=callback_run_id,
                    run_type="external_callback",
                    trace_id="trace_callback_action",
                    root_trace_id=root_trace_id,
                    payload={
                        "review_task_id": review_task_id,
                        "decision_id": decision_id,
                    },
                ),
                _run(
                    run_id=intelligence_run_id,
                    run_type="audio_intelligence",
                    trace_id="trace_intelligence_action",
                    root_trace_id=root_trace_id,
                    payload={
                        "audio_session_id": audio_session_id,
                        "recording_id": recording_id,
                    },
                ),
                JsonResource(
                    collection="audio_sessions",
                    resource_key=audio_session_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="ready",
                    trace_id=root_trace_id,
                    data={
                        "id": audio_session_id,
                        "audio_session_id": audio_session_id,
                        "recording_id": recording_id,
                        "import_batch_id": import_batch_id,
                        "root_trace_id": root_trace_id,
                    },
                ),
                AudioRecording(
                    recording_id=recording_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="verified",
                    trace_id=root_trace_id,
                    payload={
                        "audio_session_id": audio_session_id,
                        "storage_object_id": storage_object_id,
                        "import_batch_id": import_batch_id,
                        "root_trace_id": root_trace_id,
                    },
                ),
                StorageObject(
                    storage_object_id=storage_object_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    provider="minio",
                    bucket="trace-contract",
                    object_key="tenant/project/audio.wav",
                    object_key_sha256="c" * 64,
                    source_type="task_run",
                    source_id=import_run_id,
                    content_type="audio/wav",
                    size_bytes=1024,
                    content_sha256="a" * 64,
                    etag="trace-etag",
                    status="verified",
                    trace_id=root_trace_id,
                    payload={"object_version_id": "version-trace-graph"},
                ),
                _run(
                    run_id=import_run_id,
                    run_type="task_run",
                    trace_id="trace_import_action",
                    root_trace_id=root_trace_id,
                    payload={"import_batch_id": import_batch_id},
                ),
            ]
        )
        session.flush()
        session.add(
            ImportBatch(
                import_batch_id=import_batch_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                task_run_id=import_run_id,
                task_version_id="task_version_trace_graph",
                connector_id="connector_trace_graph",
                status="succeeded",
                current_stage="completed",
                total_items=1,
                succeeded_items=1,
                skipped_items=0,
                failed_items=0,
                root_trace_id=root_trace_id,
                trace_id="trace_import_action",
                payload={"target_asset_key": "auris/audio/raw_recordings"},
            )
        )
        session.flush()
        session.add(
            ImportBatchItem(
                import_item_id=import_item_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                import_batch_id=import_batch_id,
                external_record_id="external-trace-graph",
                status="succeeded",
                object_version="version-trace-graph",
                audio_session_id=audio_session_id,
                root_trace_id=root_trace_id,
                trace_id="trace_import_action",
                payload={"storage_object_id": storage_object_id},
            )
        )

        # A maliciously coincident root trace in another tenant must never
        # become a graph node or an edge endpoint.
        session.add(
            StorageObject(
                storage_object_id="object_trace_graph_other_tenant",
                tenant_id="other_tenant",
                project_id="sales_qa",
                provider="minio",
                bucket="other-tenant",
                object_key="forbidden/audio.wav",
                object_key_sha256="d" * 64,
                source_type="task_run",
                source_id="other_run",
                content_type="audio/wav",
                size_bytes=2048,
                content_sha256="e" * 64,
                status="verified",
                trace_id=root_trace_id,
                payload={},
            )
        )
        session.add(
            StorageObject(
                storage_object_id="object_trace_graph_other_project",
                tenant_id="aurora_auto",
                project_id="other_project",
                provider="minio",
                bucket="other-project",
                object_key="forbidden/project-audio.wav",
                object_key_sha256="f" * 64,
                source_type="task_run",
                source_id="other_project_run",
                content_type="audio/wav",
                size_bytes=4096,
                content_sha256="0" * 64,
                status="verified",
                trace_id=root_trace_id,
                payload={},
            )
        )

    first = client.get(f"/api/v1/traces/{root_trace_id}", headers=auth_headers)
    second = client.get(f"/api/v1/traces/{root_trace_id}", headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    first_data = first.json()["data"]
    second_data = second.json()["data"]
    assert first_data["nodes"] == second_data["nodes"]
    assert first_data["edges"] == second_data["edges"]

    nodes = first_data["nodes"]
    node_ids = {node["node_id"] for node in nodes}
    assert [node["kind"] for node in nodes] == [
        "run",
        "import_batch",
        "import_item",
        "storage_object",
        "audio_recording",
        "audio_session",
        "run",
        "asr_result",
        "evidence_pack",
        "human_review_task",
        "human_review_decision",
        "run",
        "callback_receipt",
    ]
    assert {
        f"run:{import_run_id}",
        f"import_batch:{import_batch_id}",
        f"import_item:{import_item_id}",
        f"storage_object:{storage_object_id}",
        f"audio_recording:{recording_id}",
        f"audio_session:{audio_session_id}",
        f"run:{intelligence_run_id}",
        f"asr_result:{asr_result_id}",
        f"evidence_pack:{evidence_pack_id}",
        f"human_review_task:{review_task_id}",
        f"human_review_decision:{decision_id}",
        f"run:{callback_run_id}",
        f"callback_receipt:{callback_receipt_id}",
    } == node_ids
    assert all("other_tenant" not in node_id for node_id in node_ids)
    assert "storage_object:object_trace_graph_other_tenant" not in node_ids
    assert "storage_object:object_trace_graph_other_project" not in node_ids

    edges = first_data["edges"]
    assert all(
        edge["source_node_id"] in node_ids and edge["target_node_id"] in node_ids for edge in edges
    )
    assert [
        (
            edge["source_node_id"],
            edge["relation"],
            edge["target_node_id"],
        )
        for edge in edges
    ] == [
        (
            f"run:{import_run_id}",
            "created",
            f"import_batch:{import_batch_id}",
        ),
        (
            f"import_batch:{import_batch_id}",
            "contains",
            f"import_item:{import_item_id}",
        ),
        (
            f"import_item:{import_item_id}",
            "materialized",
            f"storage_object:{storage_object_id}",
        ),
        (
            f"storage_object:{storage_object_id}",
            "registered_as",
            f"audio_recording:{recording_id}",
        ),
        (
            f"audio_recording:{recording_id}",
            "opened_as",
            f"audio_session:{audio_session_id}",
        ),
        (
            f"audio_session:{audio_session_id}",
            "processed_by",
            f"run:{intelligence_run_id}",
        ),
        (
            f"run:{intelligence_run_id}",
            "materialized",
            f"asr_result:{asr_result_id}",
        ),
        (
            f"asr_result:{asr_result_id}",
            "bound_into",
            f"evidence_pack:{evidence_pack_id}",
        ),
        (
            f"evidence_pack:{evidence_pack_id}",
            "queued_for",
            f"human_review_task:{review_task_id}",
        ),
        (
            f"human_review_task:{review_task_id}",
            "decided_by",
            f"human_review_decision:{decision_id}",
        ),
        (
            f"human_review_decision:{decision_id}",
            "triggered",
            f"run:{callback_run_id}",
        ),
        (
            f"run:{callback_run_id}",
            "received",
            f"callback_receipt:{callback_receipt_id}",
        ),
    ]

    # Existing clients still consume spans. The graph is an additive contract.
    assert first_data["spans"]
    assert any(
        span.get("kind") == "run" and span.get("id") == intelligence_run_id
        for span in first_data["spans"]
    )


def test_trace_openapi_documents_additive_nodes_and_edges_contract() -> None:
    document = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]
    projection = schemas["TraceProjection"]

    assert {"nodes", "edges", "spans"} <= set(projection["required"])
    assert projection["properties"]["nodes"]["items"]["$ref"].endswith("/TraceSpan")
    assert projection["properties"]["edges"]["items"]["$ref"].endswith("/TraceEdge")
    assert set(schemas["TraceEdge"]["required"]) == {
        "edge_id",
        "source_node_id",
        "target_node_id",
        "relation",
    }
