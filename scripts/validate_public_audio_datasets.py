#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
DEFAULT_REGISTRY = ROOT / "doc/backend-spec/public-audio-datasets-v0.1.json"
sys.path.insert(0, str(BACKEND))

from app.domain.evaluation.public_dataset_registry import (  # noqa: E402
    load_public_audio_dataset_registry,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the metadata-only public audio evaluation registry."
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    registry = load_public_audio_dataset_registry(args.registry)
    pending = [
        f"{dataset.dataset_id}:{split.split_id}"
        for dataset in registry.datasets
        for split in dataset.splits
        if split.integrity_status != "verified"
    ]
    result = {
        "status": "ok",
        "schema_version": registry.schema_version,
        "dataset_count": len(registry.datasets),
        "pending_integrity_locks": pending,
    }
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "Public audio dataset registry ok: "
            f"{result['dataset_count']} dataset(s); "
            f"{len(pending)} split(s) remain fail-closed pending SHA-256 approval."
        )


if __name__ == "__main__":
    main()
