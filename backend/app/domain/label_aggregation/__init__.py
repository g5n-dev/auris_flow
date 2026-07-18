"""Deterministic, persistence-free label aggregation domain."""

from app.domain.label_aggregation.engine import LabelAggregationEngine, normalize_text
from app.domain.label_aggregation.types import (
    AggregateDecision,
    AggregationBatch,
    AggregationExplanation,
    AggregationMode,
    AggregationPolicy,
    LabelAggregate,
    LabelDefinition,
    LabelKind,
    LabelObservation,
    ObservationContribution,
    RiskLevel,
    SourceType,
    SourceWeight,
    TimeSpan,
    UnknownLabelSuggestion,
)

__all__ = [
    "AggregateDecision",
    "AggregationBatch",
    "AggregationExplanation",
    "AggregationMode",
    "AggregationPolicy",
    "LabelAggregate",
    "LabelAggregationEngine",
    "LabelDefinition",
    "LabelKind",
    "LabelObservation",
    "ObservationContribution",
    "RiskLevel",
    "SourceType",
    "SourceWeight",
    "TimeSpan",
    "UnknownLabelSuggestion",
    "normalize_text",
]
