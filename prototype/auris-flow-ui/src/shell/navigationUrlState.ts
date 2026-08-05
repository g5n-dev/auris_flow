import type {
  DeepLinkObjectKind,
  ModuleDeepLink,
  ModuleKey
} from "../shared/contracts/navigation";

const MODULE_KEYS = new Set<ModuleKey>([
  "home",
  "tenants",
  "projects",
  "canvas",
  "data",
  "knowledge",
  "listening",
  "labels",
  "insights",
  "evaluation",
  "assets",
  "settings"
]);

const OBJECT_KINDS = new Set<DeepLinkObjectKind>([
  "module",
  "audioSession",
  "reviewSample",
  "dataAsset",
  "asset",
  "evidence",
  "knowledge",
  "labelIntent",
  "labelCandidate",
  "labelReview",
  "evaluationBadcase",
  "evaluationCase",
  "evaluationDataset",
  "evaluationCapability",
  "canvasNode",
  "taskVersion",
  "insightFact",
  "setting"
]);

const URL_KEYS = [
  "module",
  "tab",
  "object_kind",
  "object_id",
  "audio_session_id",
  "review_task_id",
  "root_trace_id",
  "return_to"
] as const;

export type RestoredNavigationState = {
  module: ModuleKey;
  target: ModuleDeepLink | null;
};

function moduleKey(value: string | null): ModuleKey | null {
  return value && MODULE_KEYS.has(value as ModuleKey)
    ? value as ModuleKey
    : null;
}

export function restoredNavigationState(): RestoredNavigationState {
  if (typeof window === "undefined") return { module: "home", target: null };
  const query = new URLSearchParams(window.location.search);
  const audioSessionId = query.get("audio_session_id")?.trim() || "";
  const restoredModule = audioSessionId
    ? "listening"
    : moduleKey(query.get("module")) ?? "home";
  const objectId = query.get("object_id")?.trim() || audioSessionId;
  const rawObjectKind = query.get("object_kind")?.trim();
  const objectKind = audioSessionId
    ? "audioSession"
    : rawObjectKind && OBJECT_KINDS.has(rawObjectKind as DeepLinkObjectKind)
      ? rawObjectKind as DeepLinkObjectKind
      : undefined;
  if (!objectId && !objectKind) {
    return { module: restoredModule, target: null };
  }
  const reviewTaskId = query.get("review_task_id")?.trim() || undefined;
  const rootTraceId = query.get("root_trace_id")?.trim() || undefined;
  const returnTo = moduleKey(query.get("return_to"));
  return {
    module: restoredModule,
    target: {
      module: restoredModule,
      tab: query.get("tab")?.trim() || undefined,
      objectKind,
      objectId: objectId || undefined,
      audioSessionId: audioSessionId || undefined,
      reviewTaskId,
      rootTraceId,
      title: audioSessionId ? `音频会话 ${audioSessionId}` : undefined,
      detail: "从可刷新链接恢复",
      focusMode: reviewTaskId ? "evidence" : "detail",
      origin: returnTo
        ? { label: "返回来源页面", module: returnTo }
        : undefined
    }
  };
}

export function writeNavigationState(
  module: ModuleKey,
  target: ModuleDeepLink | null
) {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  for (const key of URL_KEYS) url.searchParams.delete(key);
  url.searchParams.set("module", module);
  if (target) {
    if (target.tab) url.searchParams.set("tab", target.tab);
    if (target.objectKind) url.searchParams.set("object_kind", target.objectKind);
    if (target.objectId) url.searchParams.set("object_id", target.objectId);
    const audioSessionId =
      target.module === "listening"
        ? target.audioSessionId
          ?? (target.objectKind === "audioSession" ? target.objectId : undefined)
        : undefined;
    if (audioSessionId) url.searchParams.set("audio_session_id", audioSessionId);
    if (target.reviewTaskId) url.searchParams.set("review_task_id", target.reviewTaskId);
    if (target.rootTraceId) url.searchParams.set("root_trace_id", target.rootTraceId);
    if (target.origin?.module) url.searchParams.set("return_to", target.origin.module);
  }
  const next = `${url.pathname}${url.search}${url.hash}`;
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (next !== current) window.history.pushState({}, "", next);
}
