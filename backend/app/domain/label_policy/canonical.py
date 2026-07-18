from __future__ import annotations

import hashlib
import json
from typing import Any

from app.schemas.label_policy import LabelPolicyDSL


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _canonical_scalar(value: object) -> str:
    return canonical_json_bytes(value).decode("ascii")


def canonicalize_expression(expression: dict[str, Any]) -> dict[str, Any]:
    op = expression["op"]
    normalized = dict(expression)
    if op in {"all", "any"}:
        items = [canonicalize_expression(item) for item in expression["items"]]
        normalized["items"] = sorted(
            items,
            key=lambda item: canonical_json_bytes(item),
        )
    elif op == "not":
        normalized["item"] = canonicalize_expression(expression["item"])
    elif op in {"in", "not_in"}:
        normalized["values"] = sorted(expression["values"], key=_canonical_scalar)
    return normalized


def canonicalize_policy(policy: LabelPolicyDSL) -> dict[str, Any]:
    raw = policy.model_dump(mode="json", exclude_none=False)
    raw["thresholds"] = sorted(raw["thresholds"], key=lambda item: item["key"])
    canonical_rules: list[dict[str, Any]] = []
    for rule in raw["rules"]:
        canonical_rule = dict(rule)
        canonical_rule["when"] = canonicalize_expression(rule["when"])
        canonical_rules.append(canonical_rule)
    raw["rules"] = sorted(canonical_rules, key=lambda item: item["rule_id"])
    return raw
