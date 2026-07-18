from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.logging import get_logger, log_event
from app.models import JsonResource, LabelTaxonomy, LabelVersion, LabelVersionItem, RunRecord
from app.repositories.run_records import RunRecordRepository
from app.services.audit_service import record_audit
from app.services.outbox_service import enqueue_event

_ARTIFACT_STATUSES = frozenset(
    {
        "draft",
        "candidate",
        "validated",
        "locked",
        "evaluating",
        "gate_blocked",
        "review_required",
        "approved",
        "published",
        "deprecated",
        "archived",
    }
)
_LEGACY_STATUS_MAP = {"gray_releasing": "published"}
_REQUIRED_STRONG_FIELDS = (
    "artifact_status",
    "content_sha256",
    "semantic_version",
    "taxonomy_id",
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
logger = get_logger("label_lifecycle")


class LabelLifecycleDriftError(RuntimeError):
    """A legacy payload disagrees with an already materialized strong field."""


@dataclass(frozen=True)
class LabelLifecycleDerivation:
    values: dict[str, object]
    migration_required: tuple[str, ...]


@dataclass(frozen=True)
class LabelLifecycleApplyResult:
    changed_fields: tuple[str, ...]
    migration_required: tuple[str, ...]
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class LabelLifecycleShadowComparison:
    status: Literal["match", "migration-required", "drift"]
    mismatched_fields: tuple[str, ...]
    strong_missing_fields: tuple[str, ...]
    legacy_missing_fields: tuple[str, ...]


def _non_empty_string(value: object, *, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        return None
    return normalized


def _first_string(
    payload: dict[str, Any],
    *keys: str,
    max_length: int,
) -> str | None:
    for key in keys:
        normalized = _non_empty_string(payload.get(key), max_length=max_length)
        if normalized is not None:
            return normalized
    return None


def _sha256_value(payload: dict[str, Any], *keys: str) -> str | None:
    candidate = _first_string(payload, *keys, max_length=64)
    if candidate is None:
        return None
    normalized = candidate.lower()
    return normalized if _SHA256_PATTERN.fullmatch(normalized) else None


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def derive_label_version_lifecycle_fields(
    payload: dict[str, Any] | None,
) -> LabelLifecycleDerivation:
    """Extract only lifecycle values that are explicit or deterministically mapped."""

    source = payload if isinstance(payload, dict) else {}
    values: dict[str, object] = {}

    taxonomy_id = _first_string(source, "taxonomy_id", max_length=128)
    semantic_version = _first_string(
        source,
        "semantic_version",
        "version",
        max_length=64,
    )
    base_label_version_id = _first_string(
        source,
        "base_label_version_id",
        "parent_label_version_id",
        max_length=128,
    )
    replacement_label_version_id = _first_string(
        source,
        "replacement_label_version_id",
        max_length=128,
    )
    raw_status = _first_string(source, "artifact_status", "status", max_length=32)
    artifact_status = (
        raw_status if raw_status in _ARTIFACT_STATUSES else _LEGACY_STATUS_MAP.get(raw_status or "")
    )
    published_at = _parse_datetime(source.get("artifact_published_at", source.get("published_at")))
    deprecated_at = _parse_datetime(
        source.get("artifact_deprecated_at", source.get("deprecated_at"))
    )
    deprecation_reason = _first_string(source, "deprecation_reason", max_length=1024)
    content_sha256 = _sha256_value(source, "content_sha256", "manifest_sha256")

    for field_name, value in (
        ("taxonomy_id", taxonomy_id),
        ("semantic_version", semantic_version),
        ("base_label_version_id", base_label_version_id),
        ("artifact_status", artifact_status),
        ("artifact_published_at", published_at),
        ("artifact_deprecated_at", deprecated_at),
        ("deprecation_reason", deprecation_reason),
        ("replacement_label_version_id", replacement_label_version_id),
        ("content_sha256", content_sha256),
    ):
        if value is not None:
            values[field_name] = value

    missing = tuple(field for field in _REQUIRED_STRONG_FIELDS if field not in values)
    return LabelLifecycleDerivation(values=values, migration_required=missing)


def _comparable_value(value: object) -> object:
    if isinstance(value, datetime):
        return _parse_datetime(value)
    return value


def apply_label_version_lifecycle_fields(
    record: LabelVersion,
    payload: dict[str, Any] | None,
    *,
    conflict_policy: Literal["raise", "report"] = "raise",
) -> LabelLifecycleApplyResult:
    """Fill empty strong fields and surface drift without destructive overwrite."""

    if conflict_policy not in {"raise", "report"}:
        raise ValueError("conflict_policy must be 'raise' or 'report'")
    derived = derive_label_version_lifecycle_fields(payload)
    conflicts = tuple(
        field_name
        for field_name, incoming in derived.values.items()
        if (current := getattr(record, field_name)) is not None
        and _comparable_value(current) != _comparable_value(incoming)
    )
    if conflicts and conflict_policy == "raise":
        raise LabelLifecycleDriftError(
            "label lifecycle strong-field drift: " + ", ".join(conflicts)
        )

    changed: list[str] = []
    for field_name, incoming in derived.values.items():
        if field_name in conflicts:
            continue
        if getattr(record, field_name) is None:
            setattr(record, field_name, incoming)
            changed.append(field_name)
    missing = tuple(
        field_name for field_name in _REQUIRED_STRONG_FIELDS if getattr(record, field_name) is None
    )
    return LabelLifecycleApplyResult(
        changed_fields=tuple(changed),
        migration_required=missing,
        conflicts=conflicts,
    )


def transition_label_version_artifact(
    record: LabelVersion,
    target_status: str,
    *,
    occurred_at: datetime | None = None,
) -> None:
    """Apply an authorized lifecycle transition while protecting terminal artifacts."""

    if target_status not in _ARTIFACT_STATUSES:
        raise ValueError(f"unsupported label artifact status: {target_status}")
    current_status = record.artifact_status
    permitted_terminal_targets = {
        "published": {"published", "deprecated", "archived"},
        "deprecated": {"deprecated", "archived"},
        "archived": {"archived"},
    }
    if (
        current_status in permitted_terminal_targets
        and target_status not in permitted_terminal_targets[current_status]
    ):
        raise LabelLifecycleDriftError(
            f"label artifact transition {current_status} -> {target_status} is not allowed"
        )
    normalized_time = _parse_datetime(occurred_at or datetime.now(UTC))
    if target_status == "published":
        if (
            record.artifact_published_at is not None
            and occurred_at is not None
            and _comparable_value(record.artifact_published_at)
            != _comparable_value(normalized_time)
        ):
            raise LabelLifecycleDriftError("artifact_published_at is already immutable")
        if record.artifact_published_at is None:
            record.artifact_published_at = normalized_time
    elif target_status == "deprecated" and record.artifact_deprecated_at is None:
        record.artifact_deprecated_at = normalized_time
    record.artifact_status = target_status


def _serialized_lifecycle_fields(record: LabelVersion) -> dict[str, object]:
    result: dict[str, object] = {}
    for field_name in (
        "taxonomy_id",
        "semantic_version",
        "base_label_version_id",
        "artifact_status",
        "artifact_published_at",
        "artifact_deprecated_at",
        "deprecation_reason",
        "replacement_label_version_id",
        "content_sha256",
    ):
        value = getattr(record, field_name)
        if value is not None:
            result[field_name] = value.isoformat() if isinstance(value, datetime) else value
    return result


def compatible_label_version_data(
    record: LabelVersion,
    legacy_payload: dict[str, Any] | None,
    *,
    prefer_strong: bool = False,
) -> dict[str, Any]:
    """Return legacy-compatible JSON while strong fields expand alongside it."""

    result = dict(legacy_payload) if isinstance(legacy_payload, dict) else {}
    strong_values = _serialized_lifecycle_fields(record)
    if prefer_strong:
        result.update(strong_values)
    else:
        for field_name, value in strong_values.items():
            result.setdefault(field_name, value)
    result.setdefault("label_version_id", record.label_version_id)
    result.setdefault("resource_version", record.resource_version)
    result.setdefault("status", record.status)
    if record.trace_id is not None:
        result.setdefault("trace_id", record.trace_id)
    return result


def label_version_lifecycle_shadow_compare(
    record: LabelVersion,
    legacy_payload: dict[str, Any] | None,
) -> LabelLifecycleShadowComparison:
    derived = derive_label_version_lifecycle_fields(legacy_payload)
    mismatched = tuple(
        sorted(
            field_name
            for field_name, legacy_value in derived.values.items()
            if (strong_value := getattr(record, field_name)) is not None
            and _comparable_value(strong_value) != _comparable_value(legacy_value)
        )
    )
    strong_missing = tuple(
        field_name for field_name in _REQUIRED_STRONG_FIELDS if getattr(record, field_name) is None
    )
    legacy_missing = tuple(
        field_name for field_name in _REQUIRED_STRONG_FIELDS if field_name not in derived.values
    )
    status: Literal["match", "migration-required", "drift"]
    if mismatched:
        status = "drift"
    elif strong_missing or legacy_missing:
        status = "migration-required"
    else:
        status = "match"
    return LabelLifecycleShadowComparison(
        status=status,
        mismatched_fields=mismatched,
        strong_missing_fields=strong_missing,
        legacy_missing_fields=legacy_missing,
    )


def compatible_label_version_resources(
    session: Session,
    ctx: RequestContext,
    resources: list[dict[str, Any]],
    *,
    prefer_strong: bool = False,
) -> list[dict[str, Any]]:
    """Batch-enrich scoped legacy resources and emit payload/strong shadow evidence."""

    resource_ids = {
        candidate
        for resource in resources
        for candidate in (
            resource.get("label_version_id"),
            resource.get("id"),
        )
        if isinstance(candidate, str) and candidate
    }
    if not resource_ids:
        return [dict(resource) for resource in resources]
    records = session.scalars(
        select(LabelVersion).where(
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
            LabelVersion.label_version_id.in_(resource_ids),
        )
    ).all()
    by_id = {record.label_version_id: record for record in records}
    result: list[dict[str, Any]] = []
    for resource in resources:
        resource_id = resource.get("label_version_id") or resource.get("id")
        record = by_id.get(resource_id) if isinstance(resource_id, str) else None
        if record is None:
            result.append(dict(resource))
            continue
        comparison = label_version_lifecycle_shadow_compare(record, resource)
        if comparison.status != "match":
            log_event(
                logger,
                "label_lifecycle.shadow_compare",
                ctx=ctx,
                label_version_id=record.label_version_id,
                shadow_status=comparison.status,
                mismatched_fields=list(comparison.mismatched_fields),
                strong_missing_fields=list(comparison.strong_missing_fields),
                legacy_missing_fields=list(comparison.legacy_missing_fields),
            )
        result.append(
            compatible_label_version_data(
                record,
                resource,
                prefer_strong=prefer_strong,
            )
        )
    return result


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def label_taxonomy_content_sha256(data: dict[str, Any]) -> str:
    return _sha256(data)


def sync_label_taxonomy_projection(
    session: Session,
    ctx: RequestContext,
    resource_key: str,
    data: dict[str, Any],
    *,
    status: str | None = None,
    trace_id: str | None = None,
) -> LabelTaxonomy:
    """Materialize the strong taxonomy projection for one JSON resource write."""

    taxonomy_id = _first_string(data, "taxonomy_id", max_length=128) or _non_empty_string(
        resource_key,
        max_length=128,
    )
    name = _first_string(data, "name", max_length=255)
    if taxonomy_id is None or name is None:
        raise LabelLifecycleDriftError("taxonomy_id and name are required for strong projection")
    projected_status = status or _first_string(data, "status", max_length=32) or "active"
    if projected_status not in {"draft", "active", "inactive", "archived"}:
        raise LabelLifecycleDriftError(f"unsupported taxonomy status: {projected_status}")
    content_sha256 = label_taxonomy_content_sha256(data)
    taxonomy = session.get(LabelTaxonomy, taxonomy_id)
    if taxonomy is None:
        incoming_version = data.get("resource_version")
        resource_version = (
            incoming_version
            if isinstance(incoming_version, int)
            and not isinstance(incoming_version, bool)
            and incoming_version > 0
            else 1
        )
        taxonomy = LabelTaxonomy(
            taxonomy_id=taxonomy_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            name=name,
            description=_first_string(data, "description", max_length=2000),
            status=projected_status,
            resource_version=resource_version,
            content_sha256=content_sha256,
            trace_id=trace_id or ctx.trace_id,
            payload={"taxonomy_id": taxonomy_id, **data},
        )
        session.add(taxonomy)
        return taxonomy
    if taxonomy.tenant_id != ctx.tenant_id or taxonomy.project_id != ctx.project_id:
        raise LabelLifecycleDriftError("taxonomy_id is owned by another scope")
    taxonomy.name = name
    taxonomy.description = _first_string(data, "description", max_length=2000)
    taxonomy.status = projected_status
    taxonomy.resource_version += 1
    taxonomy.content_sha256 = content_sha256
    taxonomy.trace_id = trace_id or ctx.trace_id
    taxonomy.payload = {"taxonomy_id": taxonomy_id, **data}
    return taxonomy


def label_version_item_definition_sha256(item: LabelVersionItem) -> str:
    return _sha256(
        {
            "aggregation_rule": item.aggregation_rule,
            "aliases": item.aliases,
            "canonical_name": item.canonical_name,
            "label_id": item.label_id,
            "mutual_exclusion_group": item.mutual_exclusion_group,
            "parent_ids": item.parent_ids,
            "risk_level": item.risk_level,
            "status": item.status,
            "value_type": item.value_type,
        }
    )


def _materialize_taxonomies(
    session: Session,
    ctx: RequestContext,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    scoped_taxonomies = session.scalars(
        select(LabelTaxonomy).where(
            LabelTaxonomy.tenant_id == ctx.tenant_id,
            LabelTaxonomy.project_id == ctx.project_id,
        )
    ).all()
    by_id = {taxonomy.taxonomy_id: taxonomy for taxonomy in scoped_taxonomies}
    by_name = {taxonomy.name: taxonomy.taxonomy_id for taxonomy in scoped_taxonomies}
    by_hash = {taxonomy.content_sha256: taxonomy.taxonomy_id for taxonomy in scoped_taxonomies}
    resources = session.scalars(
        select(JsonResource)
        .where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "taxonomies",
        )
        .order_by(JsonResource.resource_key)
    ).all()
    for resource in resources:
        data = resource.data if isinstance(resource.data, dict) else {}
        taxonomy_id = _first_string(data, "taxonomy_id", max_length=128)
        resource_key = _non_empty_string(resource.resource_key, max_length=128)
        if taxonomy_id is None:
            taxonomy_id = resource_key
        name = _first_string(data, "name", max_length=255)
        if taxonomy_id is None or name is None:
            issues.append(
                {
                    "resource_key": resource.resource_key,
                    "resource_type": "label_taxonomy",
                    "missing_fields": [
                        field
                        for field, value in (("taxonomy_id", taxonomy_id), ("name", name))
                        if value is None
                    ],
                }
            )
            continue
        content_sha256 = _sha256(data)
        existing = by_id.get(taxonomy_id) or session.get(LabelTaxonomy, taxonomy_id)
        if existing is None:
            conflicting_fields = [
                field_name
                for field_name, owner_id in (
                    ("name", by_name.get(name)),
                    ("content_sha256", by_hash.get(content_sha256)),
                )
                if owner_id is not None and owner_id != taxonomy_id
            ]
            if conflicting_fields:
                issues.append(
                    {
                        "resource_key": resource.resource_key,
                        "resource_type": "label_taxonomy",
                        "conflicting_fields": conflicting_fields,
                    }
                )
                continue
            existing = LabelTaxonomy(
                taxonomy_id=taxonomy_id,
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                name=name,
                description=_first_string(data, "description", max_length=2000),
                status=(
                    _first_string(data, "status", max_length=32)
                    if data.get("status") in {"draft", "active", "inactive", "archived"}
                    else "active"
                ),
                resource_version=(
                    data["resource_version"]
                    if isinstance(data.get("resource_version"), int)
                    and not isinstance(data.get("resource_version"), bool)
                    and data["resource_version"] > 0
                    else 1
                ),
                content_sha256=content_sha256,
                trace_id=resource.trace_id or ctx.trace_id,
                payload=data,
            )
            session.add(existing)
            by_id[taxonomy_id] = existing
            by_name[name] = taxonomy_id
            by_hash[content_sha256] = taxonomy_id
            continue
        if existing.tenant_id != ctx.tenant_id or existing.project_id != ctx.project_id:
            issues.append(
                {
                    "resource_key": resource.resource_key,
                    "resource_type": "label_taxonomy",
                    "conflicting_fields": ["tenant_id", "project_id"],
                }
            )
        elif existing.content_sha256 != content_sha256:
            issues.append(
                {
                    "resource_key": resource.resource_key,
                    "resource_type": "label_taxonomy",
                    "conflicting_fields": ["content_sha256"],
                }
            )
    return issues


def _terminal_replay(run: RunRecord) -> dict[str, Any] | None:
    if run.status not in {"success", "blocked", "failed"}:
        return None
    return {**run.payload, "status": run.status, "replayed": True}


def _deduplicate_migration_issues(
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for issue in issues:
        identity = _canonical_json(issue)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(issue)
    return result


def run_label_lifecycle_backfill_batch(
    session: Session,
    ctx: RequestContext,
    *,
    run_id: str,
    batch_size: int = 200,
) -> dict[str, Any]:
    """Run one resumable, tenant-scoped expand/backfill batch."""

    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    run = RunRecordRepository(session).get_by_id(run_id)
    if run is not None:
        if run.tenant_id != ctx.tenant_id or run.project_id != ctx.project_id:
            raise LabelLifecycleDriftError("backfill run_id is owned by another scope")
        if run.run_type != "label_lifecycle_backfill":
            raise LabelLifecycleDriftError("backfill run_id has a different run_type")
        replay = _terminal_replay(run)
        if replay is not None:
            return replay
    else:
        run = RunRecord(
            run_id=run_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            run_type="label_lifecycle_backfill",
            status="running",
            run_key=f"label-lifecycle-backfill:{ctx.tenant_id}:{ctx.project_id}",
            partition_key=f"{ctx.tenant_id}:{ctx.project_id}",
            trace_id=ctx.trace_id,
            payload={},
        )
        session.add(run)

    previous = run.payload if isinstance(run.payload, dict) else {}
    cursor = previous.get("next_cursor")
    issues = [*previous.get("migration_required", [])]
    issues.extend(_materialize_taxonomies(session, ctx))
    session.flush()

    statement = (
        select(LabelVersion)
        .where(
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
        )
        .order_by(LabelVersion.label_version_id)
        .limit(batch_size + 1)
    )
    if isinstance(cursor, str) and cursor:
        statement = statement.where(LabelVersion.label_version_id > cursor)
    candidates = list(session.scalars(statement).all())
    has_more = len(candidates) > batch_size
    records = candidates[:batch_size]
    batch_updated = 0

    for record in records:
        lifecycle_payload = dict(record.payload) if isinstance(record.payload, dict) else {}
        derived = derive_label_version_lifecycle_fields(lifecycle_payload)
        unresolved_fields: list[str] = []
        derived_taxonomy_id = derived.values.get("taxonomy_id")
        if record.taxonomy_id is None and isinstance(derived_taxonomy_id, str):
            taxonomy = session.get(LabelTaxonomy, derived_taxonomy_id)
            if taxonomy is None or (
                taxonomy.tenant_id != ctx.tenant_id or taxonomy.project_id != ctx.project_id
            ):
                lifecycle_payload.pop("taxonomy_id", None)
                unresolved_fields.append("taxonomy_id")
        effective_taxonomy_id = record.taxonomy_id or (
            derived_taxonomy_id if "taxonomy_id" not in unresolved_fields else None
        )
        for field_name, aliases in (
            ("base_label_version_id", ("base_label_version_id", "parent_label_version_id")),
            ("replacement_label_version_id", ("replacement_label_version_id",)),
        ):
            target_id = derived.values.get(field_name)
            if getattr(record, field_name) is not None or not isinstance(target_id, str):
                continue
            target = session.get(LabelVersion, target_id)
            if target is None or (
                target.tenant_id != ctx.tenant_id
                or target.project_id != ctx.project_id
                or effective_taxonomy_id is None
                or target.taxonomy_id != effective_taxonomy_id
            ):
                for alias in aliases:
                    lifecycle_payload.pop(alias, None)
                unresolved_fields.append(field_name)
        try:
            with session.begin_nested():
                applied = apply_label_version_lifecycle_fields(
                    record,
                    lifecycle_payload,
                    conflict_policy="report",
                )
                item_changed = False
                items = session.scalars(
                    select(LabelVersionItem).where(
                        LabelVersionItem.tenant_id == ctx.tenant_id,
                        LabelVersionItem.project_id == ctx.project_id,
                        LabelVersionItem.label_version_id == record.label_version_id,
                    )
                ).all()
                for item in items:
                    derived_hash = label_version_item_definition_sha256(item)
                    if item.definition_sha256 is None:
                        item.definition_sha256 = derived_hash
                        item_changed = True
                    elif item.definition_sha256 != derived_hash:
                        applied = LabelLifecycleApplyResult(
                            changed_fields=applied.changed_fields,
                            migration_required=applied.migration_required,
                            conflicts=(*applied.conflicts, "label_item_definition_sha256"),
                        )
                session.flush()
        except IntegrityError:
            issues.append(
                {
                    "label_version_id": record.label_version_id,
                    "missing_fields": unresolved_fields,
                    "conflicting_fields": ["database_constraint"],
                    "reason_code": "STRONG_FIELD_CONSTRAINT_VIOLATION",
                }
            )
            continue
        if applied.changed_fields or item_changed:
            batch_updated += 1
        missing_fields = tuple(dict.fromkeys((*applied.migration_required, *unresolved_fields)))
        if missing_fields or applied.conflicts:
            issues.append(
                {
                    "label_version_id": record.label_version_id,
                    "missing_fields": list(missing_fields),
                    "conflicting_fields": list(applied.conflicts),
                }
            )

    issues = _deduplicate_migration_issues(issues)
    scanned_count = int(previous.get("scanned_count", 0)) + len(records)
    updated_count = int(previous.get("updated_count", 0)) + batch_updated
    batch_number = int(previous.get("batch_number", 0)) + 1
    next_cursor = records[-1].label_version_id if records else cursor
    complete = not has_more
    status = "blocked" if complete and issues else "success" if complete else "running"
    result = {
        "run_id": run_id,
        "status": status,
        "batch_number": batch_number,
        "batch_size": batch_size,
        "next_cursor": next_cursor,
        "scanned_count": scanned_count,
        "updated_count": updated_count,
        "migration_required_count": len(issues),
        "migration_required": issues,
        "ready_for_contract": complete and not issues,
        "trace_id": ctx.trace_id,
        "replayed": False,
    }
    run.status = status
    run.trace_id = ctx.trace_id
    run.payload = {key: value for key, value in result.items() if key != "replayed"}

    audit_summary = {
        "batch_number": batch_number,
        "scanned_count": scanned_count,
        "updated_count": updated_count,
        "migration_required_count": len(issues),
        "ready_for_contract": result["ready_for_contract"],
        "status": status,
    }
    record_audit(
        session,
        ctx,
        action="label_lifecycle.backfill_batch",
        object_type="label_lifecycle_backfill_run",
        object_id=run_id,
        result=status,
        after=audit_summary,
    )
    enqueue_event(
        session,
        ctx,
        event_type=f"label_lifecycle.backfill_{status}",
        aggregate_type="label_lifecycle_backfill_run",
        aggregate_id=run_id,
        payload={
            **audit_summary,
            "run_id": run_id,
            "resource_version": batch_number,
        },
    )
    session.flush()
    return result
