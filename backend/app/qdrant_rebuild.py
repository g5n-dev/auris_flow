"""Governed reconstruction of Qdrant points from authoritative MySQL delivery facts.

The command never reads from, deletes, or scans Qdrant directly.  It derives a
deterministic plan from successfully delivered Qdrant Outbox events, then
creates new, independently auditable Outbox events that preserve the original
point identity.  ``production/scripts/finalize-restore.sh`` remains the final
authority: it compares the rebuilt Qdrant contents with the signed backup
fingerprints before the write plane can be reopened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.models import AuditLog, OutboxDeliveryAttempt, OutboxEvent, Project, Tenant
from app.services.audit_service import record_audit
from app.services.outbox_service import enqueue_event

PLAN_SCHEMA = "auris-flow.qdrant-rebuild-plan/v1"
EVIDENCE_SCHEMA = "auris-flow.qdrant-rebuild-evidence/v1"
REBUILD_AGGREGATE_TYPE = "qdrant_rebuild"
REBUILD_ACTOR = "qdrant_rebuild_operator"
MAX_PLAN_BYTES = 8 * 1024 * 1024
MAX_PLAN_ITEMS = 100_000
SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TRACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
COLLECTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
SUPPORTED_EVENT_COLLECTIONS = {
    "knowledge_source.sync_requested": "knowledge_chunks",
    "knowledge_index.build_requested": "knowledge_chunks",
}
POINT_ID_FIELDS = (
    "tenant_id",
    "project_id",
    "trace_id",
    "knowledge_index_id",
    "knowledge_source_id",
    "source_id",
    "source_type",
    "version",
    "collection",
)
EMBEDDING_INPUT_FIELDS = ("embedding_text", "document_text", "content", "text")


class QdrantRebuildError(ValueError):
    """Raised when a rebuild plan or live authority cannot be trusted."""


@dataclass(frozen=True)
class RebuildCandidate:
    event: OutboxEvent
    replay_payload: dict[str, Any]
    plan_item: dict[str, Any]
    processed_at: datetime


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        + "\n"
    ).encode("ascii")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _outbox_payload_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_scope(value: str, *, label: str) -> str:
    if not SCOPE_RE.fullmatch(value) or value == "*":
        raise QdrantRebuildError(f"{label} is invalid")
    return value


def _point_id(payload: Mapping[str, Any]) -> str:
    parts = {key: payload.get(key) for key in POINT_ID_FIELDS if payload.get(key) is not None}
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"auris-flow:{raw}"))


def _business_payload(event: OutboxEvent) -> dict[str, Any]:
    payload = event.payload
    business = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(business, dict):
        raise QdrantRebuildError("source Outbox event is missing its business payload")
    expected = payload.get("business_payload_sha256")
    if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
        raise QdrantRebuildError("source Outbox event is missing its payload digest")
    if _outbox_payload_sha256(business) != expected:
        raise QdrantRebuildError("source Outbox business payload digest does not match")
    return business


def _successful_receipt(
    attempts: list[OutboxDeliveryAttempt],
    *,
    expected_point_id: str,
) -> OutboxDeliveryAttempt:
    valid: list[OutboxDeliveryAttempt] = []
    for attempt in attempts:
        details = attempt.details if isinstance(attempt.details, dict) else {}
        dispatch = details.get("dispatch_details")
        point_ids = dispatch.get("point_ids") if isinstance(dispatch, dict) else None
        if (
            attempt.status == "succeeded"
            and attempt.adapter == "qdrant"
            and attempt.operation in {"upsert_payload", "reconcile_index_payload"}
            and isinstance(point_ids, list)
            and expected_point_id in point_ids
        ):
            valid.append(attempt)
    if not valid:
        raise QdrantRebuildError(
            "processed Qdrant source event lacks a matching successful delivery receipt"
        )
    return max(valid, key=lambda item: item.lease_generation)


def _candidate(
    event: OutboxEvent,
    attempts: list[OutboxDeliveryAttempt],
) -> RebuildCandidate | None:
    expected_collection = SUPPORTED_EVENT_COLLECTIONS[event.event_type]
    business = _business_payload(event)
    qdrant_payload = business.get("qdrant_payload")
    if not isinstance(qdrant_payload, dict):
        raise QdrantRebuildError("processed Qdrant source event has no Qdrant payload")
    effective_payload = {
        **qdrant_payload,
        "tenant_id": event.tenant_id,
        "project_id": event.project_id,
        "trace_id": event.payload.get("trace_id"),
    }
    required = {
        "tenant_id",
        "project_id",
        "trace_id",
        "collection",
        "knowledge_source_id",
        "source_id",
        "source_type",
        "asset_key",
        "version",
        "business_ref",
    }
    if any(not effective_payload.get(field) for field in required):
        raise QdrantRebuildError("processed Qdrant source payload is incomplete")
    trace_id = effective_payload["trace_id"]
    collection = effective_payload["collection"]
    if not isinstance(trace_id, str) or not TRACE_RE.fullmatch(trace_id):
        raise QdrantRebuildError("processed Qdrant source trace is invalid")
    if (
        not isinstance(collection, str)
        or not COLLECTION_RE.fullmatch(collection)
        or collection != expected_collection
    ):
        raise QdrantRebuildError("processed Qdrant source collection is outside policy")
    if len(_canonical(effective_payload)) > 2 * 1024 * 1024:
        raise QdrantRebuildError("processed Qdrant source payload exceeds the byte budget")

    expected_point_id = _point_id(effective_payload)
    _successful_receipt(attempts, expected_point_id=expected_point_id)
    embedding_inputs = {
        field: business[field]
        for field in EMBEDDING_INPUT_FIELDS
        if isinstance(business.get(field), str) and str(business[field]).strip()
    }
    replay_payload = {
        **embedding_inputs,
        "qdrant_payload": effective_payload,
        "status": business.get("status"),
        "version": effective_payload["version"],
    }
    processed_at = event.processed_at
    if not isinstance(processed_at, datetime):
        raise QdrantRebuildError("processed Qdrant source event has no completion time")
    item = {
        "collection": collection,
        "embedding_inputs_sha256": _sha256(embedding_inputs),
        "expected_point_id": expected_point_id,
        "original_trace_id": trace_id,
        "source_business_payload_sha256": event.payload["business_payload_sha256"],
        "source_event_id": event.event_id,
        "source_event_type": event.event_type,
        "source_qdrant_payload_sha256": _sha256(effective_payload),
    }
    return RebuildCandidate(
        event=event,
        replay_payload=replay_payload,
        plan_item=item,
        processed_at=processed_at,
    )


def _authority_cutoff(session: Session, tenant_id: str, project_id: str) -> int:
    value = session.scalar(
        select(func.max(OutboxEvent.event_id)).where(
            OutboxEvent.tenant_id == tenant_id,
            OutboxEvent.project_id == project_id,
            OutboxEvent.aggregate_type != REBUILD_AGGREGATE_TYPE,
        )
    )
    return int(value or 0)


def _assert_scope_exists(session: Session, tenant_id: str, project_id: str) -> None:
    tenant = session.get(Tenant, tenant_id)
    project = session.get(Project, project_id)
    if tenant is None or project is None or project.tenant_id != tenant_id:
        raise QdrantRebuildError("tenant/project scope does not exist")


def _load_candidates(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    cutoff_event_id: int,
) -> list[RebuildCandidate]:
    events = list(
        session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.tenant_id == tenant_id,
                OutboxEvent.project_id == project_id,
                OutboxEvent.event_id <= cutoff_event_id,
                OutboxEvent.event_type.in_(tuple(SUPPORTED_EVENT_COLLECTIONS)),
                OutboxEvent.aggregate_type != REBUILD_AGGREGATE_TYPE,
                OutboxEvent.status == "processed",
                OutboxEvent.delivery_state == "confirmed",
            )
            .order_by(OutboxEvent.event_id)
        )
    )
    qdrant_receipt_event_types = set(
        session.scalars(
            select(OutboxEvent.event_type)
            .join(
                OutboxDeliveryAttempt,
                OutboxDeliveryAttempt.event_id == OutboxEvent.event_id,
            )
            .where(
                OutboxEvent.tenant_id == tenant_id,
                OutboxEvent.project_id == project_id,
                OutboxEvent.event_id <= cutoff_event_id,
                OutboxEvent.aggregate_type != REBUILD_AGGREGATE_TYPE,
                OutboxEvent.status == "processed",
                OutboxEvent.delivery_state == "confirmed",
                OutboxDeliveryAttempt.status == "succeeded",
                OutboxDeliveryAttempt.adapter == "qdrant",
            )
        )
    )
    unsupported_event_types = sorted(qdrant_receipt_event_types - set(SUPPORTED_EVENT_COLLECTIONS))
    if unsupported_event_types:
        raise QdrantRebuildError(
            "confirmed Qdrant receipts contain an event type outside rebuild policy"
        )
    if len(events) > MAX_PLAN_ITEMS:
        raise QdrantRebuildError("Qdrant rebuild plan exceeds the item limit")
    event_ids = [event.event_id for event in events]
    attempts_by_event: dict[int, list[OutboxDeliveryAttempt]] = {
        event_id: [] for event_id in event_ids
    }
    if event_ids:
        attempts = session.scalars(
            select(OutboxDeliveryAttempt)
            .where(OutboxDeliveryAttempt.event_id.in_(event_ids))
            .order_by(
                OutboxDeliveryAttempt.event_id,
                OutboxDeliveryAttempt.lease_generation,
            )
        )
        for attempt in attempts:
            attempts_by_event[attempt.event_id].append(attempt)

    latest_by_point: dict[str, RebuildCandidate] = {}
    for event in events:
        candidate = _candidate(event, attempts_by_event[event.event_id])
        if candidate is None:
            continue
        point_id = candidate.plan_item["expected_point_id"]
        prior = latest_by_point.get(point_id)
        if prior is None or (candidate.processed_at, event.event_id) > (
            prior.processed_at,
            prior.event.event_id,
        ):
            latest_by_point[point_id] = candidate
    return sorted(
        latest_by_point.values(),
        key=lambda item: (
            item.plan_item["collection"],
            item.plan_item["expected_point_id"],
        ),
    )


def build_plan(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    cutoff_event_id: int | None = None,
) -> tuple[dict[str, Any], list[RebuildCandidate]]:
    tenant_id = _validate_scope(tenant_id, label="tenant_id")
    project_id = _validate_scope(project_id, label="project_id")
    _assert_scope_exists(session, tenant_id, project_id)
    cutoff = (
        _authority_cutoff(session, tenant_id, project_id)
        if cutoff_event_id is None
        else cutoff_event_id
    )
    if isinstance(cutoff, bool) or not isinstance(cutoff, int) or cutoff < 0:
        raise QdrantRebuildError("cutoff_event_id is invalid")
    candidates = _load_candidates(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        cutoff_event_id=cutoff,
    )
    collections: dict[str, int] = {}
    for candidate in candidates:
        name = str(candidate.plan_item["collection"])
        collections[name] = collections.get(name, 0) + 1
    plan = {
        "schema_version": PLAN_SCHEMA,
        "authority": "mysql-confirmed-qdrant-outbox",
        "object_reference_policy": (
            "minio-objects-verified-by-restore-runbook-not-read-by-this-command"
        ),
        "scope": {"tenant_id": tenant_id, "project_id": project_id},
        "cutoff_event_id": cutoff,
        "collections": collections,
        "point_count": len(candidates),
        "items": [candidate.plan_item for candidate in candidates],
    }
    if len(_canonical(plan)) > MAX_PLAN_BYTES:
        raise QdrantRebuildError("Qdrant rebuild plan exceeds the byte budget")
    return plan, candidates


def _load_plan(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_PLAN_BYTES:
        raise QdrantRebuildError("Qdrant rebuild plan size is invalid")
    try:
        plan = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QdrantRebuildError("Qdrant rebuild plan is not valid JSON") from exc
    if (
        not isinstance(plan, dict)
        or set(plan)
        != {
            "schema_version",
            "authority",
            "object_reference_policy",
            "scope",
            "cutoff_event_id",
            "collections",
            "point_count",
            "items",
        }
        or plan.get("schema_version") != PLAN_SCHEMA
        or plan.get("authority") != "mysql-confirmed-qdrant-outbox"
        or plan.get("object_reference_policy")
        != "minio-objects-verified-by-restore-runbook-not-read-by-this-command"
    ):
        raise QdrantRebuildError("Qdrant rebuild plan schema is invalid")
    if len(_canonical(plan)) > MAX_PLAN_BYTES:
        raise QdrantRebuildError("Qdrant rebuild plan exceeds the byte budget")
    return plan


def _plan_scope(plan: Mapping[str, Any]) -> tuple[str, str, int]:
    scope = plan.get("scope")
    cutoff = plan.get("cutoff_event_id")
    if (
        not isinstance(scope, dict)
        or set(scope) != {"tenant_id", "project_id"}
        or isinstance(cutoff, bool)
        or not isinstance(cutoff, int)
        or cutoff < 0
    ):
        raise QdrantRebuildError("Qdrant rebuild plan scope is invalid")
    return (
        _validate_scope(str(scope.get("tenant_id") or ""), label="tenant_id"),
        _validate_scope(str(scope.get("project_id") or ""), label="project_id"),
        cutoff,
    )


def _assert_plan_matches_authority(
    session: Session,
    plan: dict[str, Any],
) -> tuple[list[RebuildCandidate], str]:
    tenant_id, project_id, cutoff = _plan_scope(plan)
    current_cutoff = _authority_cutoff(session, tenant_id, project_id)
    if current_cutoff != cutoff:
        raise QdrantRebuildError("MySQL authority changed after the rebuild plan was created")
    authoritative, candidates = build_plan(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        cutoff_event_id=cutoff,
    )
    if _canonical(authoritative) != _canonical(plan):
        raise QdrantRebuildError("Qdrant rebuild plan does not match live MySQL authority")
    return candidates, _sha256(plan)


def enqueue_plan(
    session: Session,
    plan: dict[str, Any],
    *,
    confirmation_sha256: str,
) -> dict[str, Any]:
    candidates, plan_sha256 = _assert_plan_matches_authority(session, plan)
    if not SHA256_RE.fullmatch(confirmation_sha256) or confirmation_sha256 != plan_sha256:
        raise QdrantRebuildError("confirmation must exactly equal the rebuild plan SHA-256")
    tenant_id, project_id, cutoff = _plan_scope(plan)
    rebuild_trace_id = f"trace_qdrant_rebuild_{plan_sha256[:32]}"
    event_ids: list[int] = []
    for candidate in candidates:
        source_trace_id = str(candidate.plan_item["original_trace_id"])
        ctx = RequestContext(
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=REBUILD_ACTOR,
            roles=("system",),
            request_id=f"qdrant-rebuild-{plan_sha256[:24]}",
            trace_id=source_trace_id,
            idempotency_key=f"qdrant-rebuild:{plan_sha256[:32]}",
            parent_trace_id=rebuild_trace_id,
            correlation_id=rebuild_trace_id,
            actor_kind="service",
        )
        source_event_id = int(candidate.plan_item["source_event_id"])
        event = enqueue_event(
            session,
            ctx,
            event_type=str(candidate.plan_item["source_event_type"]),
            aggregate_type=REBUILD_AGGREGATE_TYPE,
            aggregate_id=f"qdrant_rebuild_{plan_sha256[:16]}_{source_event_id}",
            payload={
                **candidate.replay_payload,
                "rebuild_plan_sha256": plan_sha256,
                "rebuild_trace_id": rebuild_trace_id,
                "rebuild_source_event_id": source_event_id,
                "rebuild_source_business_payload_sha256": candidate.plan_item[
                    "source_business_payload_sha256"
                ],
                "expected_point_id": candidate.plan_item["expected_point_id"],
            },
        )
        event_ids.append(event.event_id)

    existing_audit = session.scalar(
        select(AuditLog).where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.project_id == project_id,
            AuditLog.action == "qdrant.rebuild.enqueued",
            AuditLog.object_id == plan_sha256,
        )
    )
    if existing_audit is None:
        audit_ctx = RequestContext(
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=REBUILD_ACTOR,
            roles=("system",),
            request_id=f"qdrant-rebuild-{plan_sha256[:24]}",
            trace_id=rebuild_trace_id,
            idempotency_key=f"qdrant-rebuild:{plan_sha256[:32]}",
            actor_kind="service",
        )
        record_audit(
            session,
            audit_ctx,
            action="qdrant.rebuild.enqueued",
            object_type="qdrant_rebuild_plan",
            object_id=plan_sha256,
            after={
                "cutoff_event_id": cutoff,
                "collections": plan["collections"],
                "point_count": len(candidates),
                "outbox_event_ids_sha256": _sha256(sorted(event_ids)),
            },
        )
    session.flush()
    return {
        "status": "enqueued",
        "plan_sha256": plan_sha256,
        "rebuild_trace_id": rebuild_trace_id,
        "point_count": len(candidates),
        "outbox_event_ids_sha256": _sha256(sorted(event_ids)),
    }


def verify_plan(session: Session, plan: dict[str, Any]) -> dict[str, Any]:
    candidates, plan_sha256 = _assert_plan_matches_authority(session, plan)
    tenant_id, project_id, cutoff = _plan_scope(plan)
    rebuild_trace_id = f"trace_qdrant_rebuild_{plan_sha256[:32]}"
    expected = {
        f"qdrant_rebuild_{plan_sha256[:16]}_{candidate.plan_item['source_event_id']}": candidate
        for candidate in candidates
    }
    events = (
        list(
            session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.tenant_id == tenant_id,
                    OutboxEvent.project_id == project_id,
                    OutboxEvent.aggregate_type == REBUILD_AGGREGATE_TYPE,
                    OutboxEvent.aggregate_id.in_(tuple(expected)),
                )
            )
        )
        if expected
        else []
    )
    if len(events) != len(expected):
        raise QdrantRebuildError("Qdrant rebuild Outbox event set is incomplete")
    event_ids = [event.event_id for event in events]
    attempts = (
        list(
            session.scalars(
                select(OutboxDeliveryAttempt).where(OutboxDeliveryAttempt.event_id.in_(event_ids))
            )
        )
        if event_ids
        else []
    )
    attempts_by_event: dict[int, list[OutboxDeliveryAttempt]] = {
        event_id: [] for event_id in event_ids
    }
    for attempt in attempts:
        attempts_by_event[attempt.event_id].append(attempt)
    for event in events:
        candidate = expected[event.aggregate_id]
        if event.status != "processed" or event.delivery_state != "confirmed":
            raise QdrantRebuildError("Qdrant rebuild Outbox delivery is not confirmed")
        payload = event.payload
        if (
            payload.get("rebuild_plan_sha256") != plan_sha256
            or payload.get("rebuild_trace_id") != rebuild_trace_id
            or payload.get("trace_id") != candidate.plan_item["original_trace_id"]
            or _sha256(payload.get("qdrant_payload"))
            != candidate.plan_item["source_qdrant_payload_sha256"]
        ):
            raise QdrantRebuildError("Qdrant rebuild Outbox payload drifted from its plan")
        _successful_receipt(
            attempts_by_event[event.event_id],
            expected_point_id=str(candidate.plan_item["expected_point_id"]),
        )
    return {
        "schema_version": EVIDENCE_SCHEMA,
        "status": "verified",
        "authority": "mysql-outbox-delivery-receipts",
        "scope": {"tenant_id": tenant_id, "project_id": project_id},
        "cutoff_event_id": cutoff,
        "plan_sha256": plan_sha256,
        "rebuild_trace_id": rebuild_trace_id,
        "collections": plan["collections"],
        "point_count": len(candidates),
        "outbox_event_ids_sha256": _sha256(sorted(event_ids)),
        "verified_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "next_gate": "production/scripts/finalize-restore.sh",
    }


def _read_stdin() -> bytes:
    raw = sys.stdin.buffer.read(MAX_PLAN_BYTES + 1)
    if len(raw) > MAX_PLAN_BYTES:
        raise QdrantRebuildError("Qdrant rebuild plan exceeds the byte budget")
    return raw


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--tenant-id", required=True)
    plan.add_argument("--project-id", required=True)
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("--confirm-sha256", required=True)
    commands.add_parser("verify")
    return root


def main() -> int:
    try:
        args = parser().parse_args()
        if args.command == "plan":
            with SessionLocal() as session:
                plan, _candidates = build_plan(
                    session,
                    tenant_id=args.tenant_id,
                    project_id=args.project_id,
                )
            payload = _canonical(plan)
            sys.stdout.buffer.write(payload)
            print(
                f"confirm with --confirm-sha256 {hashlib.sha256(payload).hexdigest()}",
                file=sys.stderr,
            )
            return 0
        plan = _load_plan(_read_stdin())
        if args.command == "enqueue":
            with SessionLocal.begin() as session:
                result = enqueue_plan(
                    session,
                    plan,
                    confirmation_sha256=args.confirm_sha256,
                )
        else:
            with SessionLocal() as session:
                result = verify_plan(session, plan)
        sys.stdout.buffer.write(_canonical(result))
        return 0
    except QdrantRebuildError as exc:
        print(f"qdrant rebuild error: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError:
        print("qdrant rebuild error: database operation failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
