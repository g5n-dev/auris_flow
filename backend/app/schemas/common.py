from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.errors import ApiError

T = TypeVar("T")
ModelT = TypeVar("ModelT", bound=BaseModel)


class ApiMeta(BaseModel):
    trace_id: str
    request_id: str
    total: int | None = None
    limit: int | None = None
    next_cursor: str | None = None


class ApiEnvelope(BaseModel, Generic[T]):
    data: T
    meta: ApiMeta
    links: dict[str, str] | None = None


class ApiErrorDetail(BaseModel):
    field: str | None = None
    message: str
    code: str | None = None


class ApiErrorBody(BaseModel):
    code: str
    message: str
    details: list[ApiErrorDetail] = Field(default_factory=list)
    status: int
    retryable: bool = False
    trace_id: str
    idempotency_key: str | None = None


class ApiErrorEnvelope(BaseModel):
    error: ApiErrorBody


class FlexiblePayload(BaseModel):
    model_config = ConfigDict(extra="allow")


def parse_payload(model: type[ModelT], payload: dict[str, Any]) -> ModelT:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ApiError(
            "VALIDATION_ERROR",
            "请求参数校验失败",
            422,
            details=[
                {
                    "field": ".".join(str(part) for part in error["loc"]),
                    "message": str(error["msg"]),
                    "code": str(error["type"]),
                }
                for error in exc.errors()
            ],
        ) from exc
