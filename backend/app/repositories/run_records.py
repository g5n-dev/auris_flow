from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import RunRecord


class RunRecordRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, run_id: str) -> RunRecord | None:
        """Load by global identity so a service can reject cross-scope collisions."""

        return self.session.get(RunRecord, run_id)

    def list(
        self,
        *,
        tenant_id: str,
        project_id: str,
        run_type: str | None = None,
        status: str | None = None,
        cursor_created_at: datetime | None = None,
        cursor_run_id: str | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        stmt = select(RunRecord).where(
            RunRecord.tenant_id == tenant_id,
            RunRecord.project_id == project_id,
        )
        if run_type:
            stmt = stmt.where(RunRecord.run_type == run_type)
        if status:
            stmt = stmt.where(RunRecord.status == status)
        if cursor_created_at and cursor_run_id:
            stmt = stmt.where(
                or_(
                    RunRecord.created_at < cursor_created_at,
                    and_(
                        RunRecord.created_at == cursor_created_at,
                        RunRecord.run_id < cursor_run_id,
                    ),
                )
            )
        stmt = stmt.order_by(RunRecord.created_at.desc(), RunRecord.run_id.desc()).limit(limit)
        return list(self.session.scalars(stmt))

    def count(
        self,
        *,
        tenant_id: str,
        project_id: str,
        run_type: str | None = None,
        status: str | None = None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(RunRecord)
            .where(
                RunRecord.tenant_id == tenant_id,
                RunRecord.project_id == project_id,
            )
        )
        if run_type:
            stmt = stmt.where(RunRecord.run_type == run_type)
        if status:
            stmt = stmt.where(RunRecord.status == status)
        return int(self.session.scalar(stmt) or 0)
