
import type listeningFixtureSchema from "./data/listening-fixtures.json";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";

const listeningFixture = await loadJsonFixture<typeof listeningFixtureSchema>(
  new URL("./data/listening-fixtures.json", import.meta.url),
  "调听 fixture"
);
export type StitchedWavSlice = {
  id: string;
  label: string;
  file: string;
  sourceStart: number;
  sourceEnd: number;
  conversationStart: number;
  conversationEnd: number;
  wallStart: string;
  wallEnd: string;
  reason: string;
  references: string[];
  boundary: string[];
  operationHint: string;
  confidence: number;
};

export type BoundaryExtensionDecision = "idle" | "preview" | "merged" | "split";

export type BoundaryExtensionCandidate = {
  id: string;
  direction: "previous" | "next";
  label: string;
  file: string;
  sourceKind: string;
  wallStart: string;
  wallEnd: string;
  candidateRange: string;
  previewStart: string;
  previewEnd: string;
  recommendation: string;
  evidence: string[];
  confidence: number;
};

export type BoundaryPreviewClip = "before" | "source" | "after";

export type BoundaryPreviewSource = {
  kind: "slice" | "extension";
  id: string;
  label: string;
  file: string;
  start: number;
  end: number;
  meta: string;
};

export type BoundaryPreviewState = {
  kind: BoundaryPreviewSource["kind"];
  id: string;
  clip: BoundaryPreviewClip;
  label: string;
  windowText: string;
  playing: boolean;
};

export type BoundaryExtensionLock = {
  startClock: string;
  endClock: string;
};

export const stitchedWavSlices: StitchedWavSlice[] = (listeningFixture.boundaryFixtures.stitchedWavSlices as unknown as StitchedWavSlice[]);

export const boundaryExtensionCandidates: BoundaryExtensionCandidate[] = (listeningFixture.boundaryFixtures.boundaryExtensionCandidates as unknown as BoundaryExtensionCandidate[]);

export const extensionDecisionLabels: Record<BoundaryExtensionDecision, string> = {
  idle: "未拉取",
  preview: "试听中",
  merged: "待并入",
  split: "拆出新会话"
};
