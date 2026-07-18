export type ModuleMetric = {
  label: string;
  value: string;
  delta: string;
  tone: "teal" | "green" | "amber" | "red" | "violet" | "blue";
};

export type ModuleConfig = {
  eyebrow: string;
  title: string;
  scope: string;
  tabs: Array<{ id: string; label: string }>;
  metrics: ModuleMetric[];
};

export type ProjectionMetricSource = "pending" | "bff" | "bff-empty" | "mock";

export type ProjectionDisplayState = "pending" | "synced" | "empty" | "degraded";
