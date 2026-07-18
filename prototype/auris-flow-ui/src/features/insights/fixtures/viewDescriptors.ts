import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";
import type {
  InsightMetric,
  InsightNorthStar,
  InsightTabKey,
  InsightTone,
  InsightViewConfig
} from "../types";
import type insightFixtureSchema from "./data/insights-fixtures.json";

export type InsightMetricKey =
  | "effectiveReceptionRate"
  | "conversionProgress"
  | "objectionResolution"
  | "quoteConsistency"
  | "riskReverseScore"
  | "testDriveIntent"
  | "crosstalkRisk"
  | "tagAssetQuality"
  | "modelQuality";

export type InsightMetricDescriptor = Omit<
  InsightMetric,
  | "key"
  | "value"
  | "valueNumber"
  | "delta"
  | "tone"
  | "tags"
  | "rangeValues"
  | "evidenceCount"
  | "evidenceIds"
> & {
  key: InsightMetricKey;
  value: null;
  valueNumber: null;
  delta: string | null;
  tone: InsightTone | null;
  tags: readonly string[];
  rangeValues: null;
  evidenceCount: null;
  evidenceIds: null;
};

type NorthStarComponent = InsightNorthStar["components"][number];

export type NorthStarComponentKey = "effective" | "conversion" | "quote" | "risk";

export type NorthStarComponentDescriptor = Omit<
  NorthStarComponent,
  "key" | "value" | "contribution" | "tone"
> & {
  key: NorthStarComponentKey;
  value: null;
  contribution: null;
  tone: InsightTone | null;
};

export type InsightNorthStarDescriptor = Omit<
  InsightNorthStar,
  "value" | "delta" | "lift" | "drag" | "components"
> & {
  value: null;
  delta: null;
  lift: null;
  drag: null;
  components: readonly NorthStarComponentDescriptor[];
};

export type InsightViewDescriptor = Omit<InsightViewConfig, "metricKeys" | "chartIds"> & {
  metricKeys: readonly string[];
  chartIds: readonly string[];
};

const insightFixture = await loadJsonFixture<typeof insightFixtureSchema>(
  new URL("./data/insights-fixtures.json", import.meta.url),
  "业务洞察 fixture"
);

export function cloneFixtureDescriptor<T>(value: T): T {
  if (Array.isArray(value)) {
    return value.map((item) => cloneFixtureDescriptor(item)) as T;
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, cloneFixtureDescriptor(item)])
    ) as T;
  }
  return value;
}

export const metricDescriptors = insightFixture.metricDescriptors as unknown as readonly InsightMetricDescriptor[];
export const northStarDescriptor = insightFixture.northStarDescriptor as unknown as InsightNorthStarDescriptor;
export const viewDescriptors = insightFixture.views as unknown as Readonly<Record<InsightTabKey, InsightViewDescriptor>>;
export const chartDescriptors = insightFixture.chartDescriptors;
export const emptyProjectionViews = insightFixture.emptyProjectionViews;
