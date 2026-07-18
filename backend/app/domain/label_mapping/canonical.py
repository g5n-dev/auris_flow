from __future__ import annotations

import hashlib
import json
from typing import Any

from app.domain.label_mapping.types import LabelItemSnapshot


class CanonicalJsonError(ValueError):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CanonicalJsonError("mapping content must be finite canonical JSON") from exc
    return encoded.encode("utf-8")


def sha256_document(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def label_item_semantic_document(item: LabelItemSnapshot) -> dict[str, object]:
    return {
        "aggregation_rule": item.aggregation_rule,
        "label_id": item.label_id,
        "mutual_exclusion_group": item.mutual_exclusion_group,
        "parent_ids": sorted(item.parent_ids),
        "risk_level": item.risk_level,
        "schema_version": "auris.label-item-semantic/1",
        "status": item.status,
        "value_type": item.value_type,
    }


def label_item_definition_document(item: LabelItemSnapshot) -> dict[str, object]:
    return {
        "aggregation_rule": item.aggregation_rule,
        "aliases": list(item.aliases),
        "canonical_name": item.canonical_name,
        "label_id": item.label_id,
        "mutual_exclusion_group": item.mutual_exclusion_group,
        "parent_ids": list(item.parent_ids),
        "risk_level": item.risk_level,
        "status": item.status,
        "value_type": item.value_type,
    }


def label_item_display_document(item: LabelItemSnapshot) -> dict[str, object]:
    return {
        "aliases": sorted(item.aliases),
        "canonical_name": item.canonical_name,
    }


def label_item_semantic_sha256(item: LabelItemSnapshot) -> str:
    return sha256_document(label_item_semantic_document(item))


def label_item_definition_sha256(item: LabelItemSnapshot) -> str:
    return sha256_document(label_item_definition_document(item))


def label_item_display_sha256(item: LabelItemSnapshot) -> str:
    return sha256_document(label_item_display_document(item))
