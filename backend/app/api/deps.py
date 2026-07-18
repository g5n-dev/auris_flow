from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from app.core.context import RequestContext, request_context, signed_completion_context
from app.core.database import get_session

SessionDep = Annotated[Session, Depends(get_session)]
ContextDep = Annotated[RequestContext, Depends(request_context)]
SignedCompletionContextDep = Annotated[RequestContext, Depends(signed_completion_context)]


def pagination(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, str | int | None]:
    return {"cursor": cursor, "limit": limit}


PaginationDep = Annotated[dict[str, str | int | None], Depends(pagination)]
