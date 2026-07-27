from __future__ import annotations

from types import SimpleNamespace

from app.services import insight_closure_service
from app.services.public_run_projection_service import sanitize_public_run_string


def test_server_generated_insight_id_cannot_be_mistaken_for_a_phone(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        insight_closure_service.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="18448998983c" + ("a" * 20)),
    )

    generated = insight_closure_service._server_generated_id("insight_action")

    assert generated == "insight_action_1844_8998_983c"
    assert sanitize_public_run_string(generated, field_name="action_id") == generated
