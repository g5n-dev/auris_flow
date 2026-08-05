export type ModuleProjectionStatus = "pending" | "synced" | "empty" | "degraded";
export type ModuleContentSource = "bff" | "mock" | "none";

// This registry is deliberately positive and exhaustive for modules whose
// rendered detail is actually bound to the current read response. Modules that
// still mix local facts (settings/evaluation/insights) stay absent and fail closed.
const AUTHORITATIVE_CONTENT_READERS = new Set([
  "home",
  "tenants",
  "projects",
  "knowledge",
  "canvas",
  "labels",
  "assets",
  "data"
]);

export function resolveModuleContentSource({
  moduleKey,
  projectionStatus,
  demoMode
}: {
  moduleKey: string;
  projectionStatus: ModuleProjectionStatus;
  demoMode: boolean;
}): ModuleContentSource {
  if (demoMode) {
    return projectionStatus === "synced" || projectionStatus === "degraded" ? "mock" : "none";
  }
  if (projectionStatus !== "synced") return "none";
  return AUTHORITATIVE_CONTENT_READERS.has(moduleKey) ? "bff" : "none";
}

export function resolveModuleDetailVisibility({
  projectionStatus,
  contentSource,
  demoMode
}: {
  moduleKey: string;
  projectionStatus: ModuleProjectionStatus;
  contentSource: ModuleContentSource;
  demoMode: boolean;
}): { renderDetails: boolean; detailsUnavailable: boolean } {
  const settled = projectionStatus === "synced"
    || projectionStatus === "degraded"
    || projectionStatus === "empty";
  const renderDetails = settled && contentSource !== "none";
  const detailsUnavailable = contentSource === "none" && (
    projectionStatus === "synced"
    || projectionStatus === "degraded"
    || (!demoMode && projectionStatus === "empty")
  );
  return { renderDetails, detailsUnavailable };
}
