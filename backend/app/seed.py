from __future__ import annotations

import sys

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.logging import configure_logging, get_logger, log_event
from app.services.resource_service import load_seed_file, seed_database

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger("seed")


def main() -> None:
    profile = sys.argv[1] if len(sys.argv) > 1 else "local_demo"
    if profile != "local_demo":
        raise SystemExit(f"unsupported seed profile: {profile}")
    log_event(logger, "seed.start", profile=profile)
    seed = load_seed_file()
    with SessionLocal() as session:
        seed_database(session, seed)
    log_event(logger, "seed.complete", profile=profile)


if __name__ == "__main__":
    main()
