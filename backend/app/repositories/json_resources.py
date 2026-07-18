from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models import JsonResource


class JsonResourceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list(
        self,
        *,
        tenant_id: str,
        project_id: str,
        collection: str,
        status: str | None = None,
        cursor: int = 0,
        limit: int = 50,
        read_scope: ColumnElement[bool] | None = None,
        predicates: tuple[ColumnElement[bool], ...] = (),
    ) -> list[JsonResource]:
        stmt = select(JsonResource).where(
            JsonResource.collection == collection,
            JsonResource.tenant_id == tenant_id,
            JsonResource.project_id == project_id,
        )
        if status:
            stmt = stmt.where(JsonResource.status == status)
        if read_scope is not None:
            stmt = stmt.where(read_scope)
        if predicates:
            stmt = stmt.where(*predicates)
        if cursor:
            stmt = stmt.where(JsonResource.id > cursor)
        stmt = stmt.order_by(JsonResource.id).limit(limit)
        return list(self.session.scalars(stmt))

    def count(
        self,
        *,
        tenant_id: str,
        project_id: str,
        collection: str,
        status: str | None = None,
        read_scope: ColumnElement[bool] | None = None,
        predicates: tuple[ColumnElement[bool], ...] = (),
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(JsonResource)
            .where(
                JsonResource.collection == collection,
                JsonResource.tenant_id == tenant_id,
                JsonResource.project_id == project_id,
            )
        )
        if status:
            stmt = stmt.where(JsonResource.status == status)
        if read_scope is not None:
            stmt = stmt.where(read_scope)
        if predicates:
            stmt = stmt.where(*predicates)
        return int(self.session.scalar(stmt) or 0)

    def status_counts(
        self,
        *,
        tenant_id: str,
        project_id: str,
        collection: str,
        read_scope: ColumnElement[bool] | None = None,
        predicates: tuple[ColumnElement[bool], ...] = (),
    ) -> dict[str, int]:
        stmt = (
            select(JsonResource.status, func.count())
            .where(
                JsonResource.collection == collection,
                JsonResource.tenant_id == tenant_id,
                JsonResource.project_id == project_id,
            )
            .group_by(JsonResource.status)
        )
        if read_scope is not None:
            stmt = stmt.where(read_scope)
        if predicates:
            stmt = stmt.where(*predicates)
        return {
            str(status or "unknown"): int(count)
            for status, count in self.session.execute(stmt).all()
        }

    def find(
        self,
        *,
        tenant_id: str,
        project_id: str,
        collection: str,
        resource_key: str,
    ) -> JsonResource | None:
        return self.session.scalar(
            select(JsonResource).where(
                JsonResource.collection == collection,
                JsonResource.resource_key == resource_key,
                JsonResource.tenant_id == tenant_id,
                JsonResource.project_id == project_id,
            )
        )
