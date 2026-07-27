from __future__ import annotations

import hashlib
import re

from app.core.redaction import PHONE_PATTERN
from app.services.audio_import_completion_service import _stable_id
from app.services.public_run_projection_service import public_run_projection


def test_audio_import_session_id_and_navigation_survive_public_projection() -> None:
    identity_part = "audio-import-public-id-297"
    raw_digest = hashlib.sha256(identity_part.encode()).hexdigest()[:32]
    assert PHONE_PATTERN.search(raw_digest) is not None

    audio_session_id = _stable_id("audio_session_import", identity_part)
    route = f"audio-sessions/{audio_session_id}"
    projected = public_run_projection(
        {
            "import_batch": {"audio_session_ids": [audio_session_id]},
            "next_actions": [
                {
                    "key": "view_audio_session",
                    "label": "查看新会话",
                    "route": route,
                }
            ],
        }
    )

    assert re.fullmatch(r"audio_session_import_[a-p]{32}", audio_session_id)
    assert PHONE_PATTERN.search(audio_session_id) is None
    assert projected["import_batch"]["audio_session_ids"] == [audio_session_id]
    assert projected["next_actions"][0]["route"] == route
