from __future__ import annotations

from urllib.error import URLError

import pytest

from app.core.errors import ApiError
from app.services import knowledge_recall_service


def test_real_qdrant_recall_redacts_downstream_exception_details(monkeypatch) -> None:
    remote_detail = "https://qdrant.internal:6333/internal/credential/must-not-leak"

    class FailingQdrantClient:
        def search_index_payload(self, *_args, **_kwargs):
            raise URLError(remote_detail)

    monkeypatch.setattr(
        knowledge_recall_service,
        "configured_real_qdrant_client",
        lambda: FailingQdrantClient(),
    )

    with pytest.raises(ApiError) as raised:
        knowledge_recall_service.recall_from_real_qdrant(
            {"collection": "knowledge_chunks"},
            query="报价冲突",
            top_k=3,
        )

    error = raised.value
    assert error.code == "KNOWLEDGE_RECALL_FAILED"
    assert error.status_code == 502
    assert error.retryable is True
    assert error.details == [{"code": "URLError"}]
    assert remote_detail not in str(error.details)
