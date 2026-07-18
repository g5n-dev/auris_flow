import type { LabelTrackKey } from "../../../shared/fixtures/labelLayers";
import type { EvidenceModuleKey, EvidencePageConfig, ListeningDeviceKey, MarkState, PanelTab } from "../types";
import type { TrackRegion } from "../model/trackLayout";
import type listeningFixtureSchema from "./data/listening-fixtures.json";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";

const listeningFixture = await loadJsonFixture<typeof listeningFixtureSchema>(
  new URL("./data/listening-fixtures.json", import.meta.url),
  "调听 fixture"
);


export const lanes = listeningFixture.evidenceFixtures.lanes;

export const listeningDeviceBadges: Array<{
  key: ListeningDeviceKey;
  name: string;
  role: string;
  color: string;
  battery: string;
  count: string;
  src: string;
  panel: PanelTab;
  mark: MarkState;
  summary: string;
  rowTerms: string[];
  evidenceItems: Array<[string, string, string]>;
}> = (listeningFixture.evidenceFixtures.listeningDeviceBadges as unknown as Array<{
  key: ListeningDeviceKey;
  name: string;
  role: string;
  color: string;
  battery: string;
  count: string;
  src: string;
  panel: PanelTab;
  mark: MarkState;
  summary: string;
  rowTerms: string[];
  evidenceItems: Array<[string, string, string]>;
}>);

export const defaultEvidencePageConfig: EvidencePageConfig = (listeningFixture.evidenceFixtures.defaultEvidencePageConfig as unknown as EvidencePageConfig);

export const evidenceConfigPresets: Array<{ key: string; label: string; description: string; config: EvidencePageConfig }> = (listeningFixture.evidenceFixtures.evidenceConfigPresets as unknown as Array<{ key: string; label: string; description: string; config: EvidencePageConfig }>);

export const evidenceModuleLabels: Record<EvidenceModuleKey, { label: string; description: string }> = (listeningFixture.evidenceFixtures.evidenceModuleLabels as unknown as Record<EvidenceModuleKey, { label: string; description: string }>);

export const asrRows = listeningFixture.evidenceFixtures.asrRows;

export const docs = listeningFixture.evidenceFixtures.docs;

export const documentEventRegions: TrackRegion[] = (listeningFixture.evidenceFixtures.documentEventRegions as unknown as TrackRegion[]);

export const mismatches = listeningFixture.evidenceFixtures.mismatches;

export type ReviewQueueMockItem = {
  label: string;
  count: number;
  tone: "danger" | "warn" | "low";
  hint: string;
  sampleId: string;
  dataAssetId: string;
  assetKey: string;
  api: string;
  dagsterAsset: string;
  refreshJob: string;
  linkedViews: string[];
};

export const reviewQueueMockData: ReviewQueueMockItem[] = (listeningFixture.evidenceFixtures.reviewQueueMockData as unknown as ReviewQueueMockItem[]);

export const quickChips = listeningFixture.evidenceFixtures.quickChips;

export const labelTrackMeta: Array<{ key: LabelTrackKey; label: string; color: string }> = (listeningFixture.evidenceFixtures.labelTrackMeta as unknown as Array<{ key: LabelTrackKey; label: string; color: string }>);

export const eventTrackBindings: Record<string, LabelTrackKey[]> = (listeningFixture.evidenceFixtures.eventTrackBindings as unknown as Record<string, LabelTrackKey[]>);

export const minimapTrackFilterKeys: LabelTrackKey[] = ["entity", "intent", "qa", "doc", "cross", "agent"];

export const minimapVoiceAssociationKeys: LabelTrackKey[] = ["asr", "entity", "intent", "qa", "cross", "agent"];

export const trackSegments = listeningFixture.evidenceFixtures.trackSegments;

export const annotationIslands = listeningFixture.evidenceFixtures.annotationIslands;

export const simpleWaveBars = Array.from({ length: 132 }, (_, i) => {
  const level = 12 + ((i * 23) % 76);
  const role = i % 17 > 10 ? "customer" : i % 11 < 2 ? "silence" : "agent";
  return { level, role };
});

export const segmentBars = Array.from({ length: 92 }, (_, i) => {
  const levels = [18, 32, 56, 74, 42, 26, 66, 38, 82, 24];
  return levels[(i * 7) % levels.length] + ((i % 5) * 4);
});

export const matrixTimes = listeningFixture.evidenceFixtures.matrixTimes;
