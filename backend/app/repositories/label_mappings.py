from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.models import (
    LabelMappingBundle,
    LabelMappingBundleMember,
    LabelMappingBundlePath,
    LabelMappingBundleSource,
    LabelMappingItem,
    LabelMappingItemTarget,
    LabelMappingVersion,
    LabelVersion,
    LabelVersionItem,
)


def get_label_versions(
    session: Session,
    ctx: RequestContext,
    label_version_ids: Iterable[str],
    *,
    for_update: bool = False,
) -> dict[str, LabelVersion]:
    ordered_ids = sorted(set(label_version_ids))
    statement = (
        select(LabelVersion)
        .where(
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
            LabelVersion.label_version_id.in_(ordered_ids),
        )
        .order_by(LabelVersion.label_version_id)
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return {row.label_version_id: row for row in session.scalars(statement)}


def list_label_version_items(
    session: Session,
    ctx: RequestContext,
    label_version_id: str,
    *,
    for_update: bool = False,
) -> list[LabelVersionItem]:
    statement = (
        select(LabelVersionItem)
        .where(
            LabelVersionItem.tenant_id == ctx.tenant_id,
            LabelVersionItem.project_id == ctx.project_id,
            LabelVersionItem.label_version_id == label_version_id,
        )
        .order_by(LabelVersionItem.label_id)
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return list(session.scalars(statement))


def get_mapping_version(
    session: Session,
    ctx: RequestContext,
    mapping_version_id: str,
    *,
    for_update: bool = False,
) -> LabelMappingVersion | None:
    statement = select(LabelMappingVersion).where(
        LabelMappingVersion.tenant_id == ctx.tenant_id,
        LabelMappingVersion.project_id == ctx.project_id,
        LabelMappingVersion.mapping_version_id == mapping_version_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.scalar(statement)


def find_mapping_by_content_sha256(
    session: Session,
    ctx: RequestContext,
    content_sha256: str,
    *,
    for_update: bool = False,
) -> LabelMappingVersion | None:
    statement = select(LabelMappingVersion).where(
        LabelMappingVersion.tenant_id == ctx.tenant_id,
        LabelMappingVersion.project_id == ctx.project_id,
        LabelMappingVersion.content_sha256 == content_sha256,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.scalar(statement)


def find_mapping_by_pair_and_version(
    session: Session,
    ctx: RequestContext,
    *,
    source_label_version_id: str,
    target_label_version_id: str,
    mapping_version: str,
    for_update: bool = False,
) -> LabelMappingVersion | None:
    statement = select(LabelMappingVersion).where(
        LabelMappingVersion.tenant_id == ctx.tenant_id,
        LabelMappingVersion.project_id == ctx.project_id,
        LabelMappingVersion.source_label_version_id == source_label_version_id,
        LabelMappingVersion.target_label_version_id == target_label_version_id,
        LabelMappingVersion.mapping_version == mapping_version,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.scalar(statement)


def list_mapping_items(
    session: Session,
    ctx: RequestContext,
    mapping_version_id: str,
    *,
    for_update: bool = False,
) -> list[LabelMappingItem]:
    statement = (
        select(LabelMappingItem)
        .where(
            LabelMappingItem.tenant_id == ctx.tenant_id,
            LabelMappingItem.project_id == ctx.project_id,
            LabelMappingItem.mapping_version_id == mapping_version_id,
        )
        .order_by(LabelMappingItem.source_label_id)
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return list(session.scalars(statement))


def list_mapping_targets(
    session: Session,
    ctx: RequestContext,
    mapping_version_id: str,
    *,
    for_update: bool = False,
) -> list[LabelMappingItemTarget]:
    statement = (
        select(LabelMappingItemTarget)
        .where(
            LabelMappingItemTarget.tenant_id == ctx.tenant_id,
            LabelMappingItemTarget.project_id == ctx.project_id,
            LabelMappingItemTarget.mapping_version_id == mapping_version_id,
        )
        .order_by(
            LabelMappingItemTarget.mapping_item_id,
            LabelMappingItemTarget.target_order,
        )
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return list(session.scalars(statement))


def get_mapping_versions(
    session: Session,
    ctx: RequestContext,
    mapping_version_ids: Iterable[str],
    *,
    for_update: bool = False,
) -> dict[str, LabelMappingVersion]:
    ordered_ids = sorted(set(mapping_version_ids))
    statement = (
        select(LabelMappingVersion)
        .where(
            LabelMappingVersion.tenant_id == ctx.tenant_id,
            LabelMappingVersion.project_id == ctx.project_id,
            LabelMappingVersion.mapping_version_id.in_(ordered_ids),
        )
        .order_by(LabelMappingVersion.mapping_version_id)
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return {row.mapping_version_id: row for row in session.scalars(statement)}


def find_bundle_by_manifest_sha256(
    session: Session,
    ctx: RequestContext,
    canonical_manifest_sha256: str,
    *,
    for_update: bool = False,
) -> LabelMappingBundle | None:
    statement = select(LabelMappingBundle).where(
        LabelMappingBundle.tenant_id == ctx.tenant_id,
        LabelMappingBundle.project_id == ctx.project_id,
        LabelMappingBundle.canonical_manifest_sha256 == canonical_manifest_sha256,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.scalar(statement)


def list_bundle_sources(
    session: Session,
    ctx: RequestContext,
    mapping_bundle_id: str,
) -> list[LabelMappingBundleSource]:
    return list(
        session.scalars(
            select(LabelMappingBundleSource)
            .where(
                LabelMappingBundleSource.tenant_id == ctx.tenant_id,
                LabelMappingBundleSource.project_id == ctx.project_id,
                LabelMappingBundleSource.mapping_bundle_id == mapping_bundle_id,
            )
            .order_by(LabelMappingBundleSource.source_order)
        )
    )


def list_bundle_members(
    session: Session,
    ctx: RequestContext,
    mapping_bundle_id: str,
) -> list[LabelMappingBundleMember]:
    return list(
        session.scalars(
            select(LabelMappingBundleMember)
            .where(
                LabelMappingBundleMember.tenant_id == ctx.tenant_id,
                LabelMappingBundleMember.project_id == ctx.project_id,
                LabelMappingBundleMember.mapping_bundle_id == mapping_bundle_id,
            )
            .order_by(LabelMappingBundleMember.edge_order)
        )
    )


def list_bundle_paths(
    session: Session,
    ctx: RequestContext,
    mapping_bundle_id: str,
) -> list[LabelMappingBundlePath]:
    return list(
        session.scalars(
            select(LabelMappingBundlePath)
            .where(
                LabelMappingBundlePath.tenant_id == ctx.tenant_id,
                LabelMappingBundlePath.project_id == ctx.project_id,
                LabelMappingBundlePath.mapping_bundle_id == mapping_bundle_id,
            )
            .order_by(
                LabelMappingBundlePath.source_label_version_id,
                LabelMappingBundlePath.source_label_id,
                LabelMappingBundlePath.metric_family,
            )
        )
    )
