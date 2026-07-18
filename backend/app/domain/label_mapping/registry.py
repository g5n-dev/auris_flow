from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MetricCompatibilityRule:
    metric_family: str
    allowed_grains: frozenset[str]
    allowed_lineage_keys: frozenset[str]
    allowed_reducers: frozenset[str]

    def supports(self, *, grain: str, lineage_key: str, reducer: str) -> bool:
        return (
            grain in self.allowed_grains
            and lineage_key in self.allowed_lineage_keys
            and reducer in self.allowed_reducers
        )


@dataclass(frozen=True, slots=True)
class MetricCompatibilityRegistry:
    version: str
    rules: tuple[MetricCompatibilityRule, ...]

    def rule_for(self, metric_family: str) -> MetricCompatibilityRule | None:
        return next(
            (rule for rule in self.rules if rule.metric_family == metric_family),
            None,
        )


DEFAULT_METRIC_COMPATIBILITY_REGISTRY = MetricCompatibilityRegistry(
    version="label-mapping-metric-registry/1.0.0",
    rules=(
        MetricCompatibilityRule(
            metric_family="presence",
            allowed_grains=frozenset({"business-event", "fact"}),
            allowed_lineage_keys=frozenset({"event_id", "fact_logical_key"}),
            allowed_reducers=frozenset({"presence-any"}),
        ),
        MetricCompatibilityRule(
            metric_family="distinct-count",
            allowed_grains=frozenset({"business-event", "fact"}),
            allowed_lineage_keys=frozenset({"event_id", "fact_logical_key"}),
            allowed_reducers=frozenset({"distinct-lineage", "presence-any"}),
        ),
    ),
)
