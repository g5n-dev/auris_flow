export type HotwordPackVersionView = {
  id: string;
  packId: string;
  version: string;
  baselineVersionId: string | null;
  status: string;
  resourceVersion: number;
  buildRunId: string | null;
  evalRunId: string | null;
  evalLocked: boolean;
  modelApprovedBy: string | null;
  providerArtifactRef: string | null;
  compiledProvider: string | null;
  rootTraceId: string | null;
  publishRunId: string | null;
  taskVersionId: string | null;
  items: HotwordPackVersionItemView[];
};

export type HotwordPackVersionItemView = {
  id: string;
  canonicalTerm: string;
  normalizedTerm: string;
  aliases: string[];
  category: string;
  weight: number;
  sourceBadcaseId: string | null;
  resourceVersion: number;
};

export const HOTWORD_PACK_DOMAIN = "auto-sales";

export const normalizeHotwordForComparison = (value: string) =>
  value
    .normalize("NFKC")
    .trim()
    .replace(/^[\s\p{P}]+|[\s\p{P}]+$/gu, "")
    .toLocaleLowerCase();

export const nextHotwordVersionLabel = (value: string) => {
  const match = value.match(/^(.*?)(\d+)$/);
  return match ? `${match[1]}${Number(match[2]) + 1}` : `${value}.1`;
};

export const hotwordVersionItemView = (
  raw: Record<string, unknown>
): HotwordPackVersionItemView | null => {
  const id = typeof raw.item_id === "string" ? raw.item_id : typeof raw.id === "string" ? raw.id : null;
  const canonicalTerm = typeof raw.canonical_term === "string" ? raw.canonical_term : null;
  const resourceVersion = typeof raw.resource_version === "number" ? raw.resource_version : null;
  if (!id || !canonicalTerm || resourceVersion === null) return null;
  return {
    id,
    canonicalTerm,
    normalizedTerm: typeof raw.normalized_term === "string"
      ? raw.normalized_term
      : normalizeHotwordForComparison(canonicalTerm),
    aliases: Array.isArray(raw.aliases) ? raw.aliases.filter((alias): alias is string => typeof alias === "string") : [],
    category: typeof raw.category === "string" ? raw.category : "domain-term",
    weight: typeof raw.weight === "number" ? raw.weight : 0,
    sourceBadcaseId: typeof raw.source_badcase_id === "string" ? raw.source_badcase_id : null,
    resourceVersion
  };
};

export const hotwordVersionView = (
  raw: Record<string, unknown>
): HotwordPackVersionView | null => {
  const id = typeof raw.version_id === "string" ? raw.version_id : typeof raw.id === "string" ? raw.id : null;
  const packId = typeof raw.pack_id === "string" ? raw.pack_id : null;
  const resourceVersion = typeof raw.resource_version === "number" ? raw.resource_version : null;
  if (!id || !packId || resourceVersion === null) return null;
  const rawItems = Array.isArray(raw.items) ? raw.items : [];
  return {
    id,
    packId,
    version: typeof raw.version === "string" ? raw.version : id,
    baselineVersionId: typeof raw.baseline_version_id === "string" ? raw.baseline_version_id : null,
    status: typeof raw.status === "string" ? raw.status : "draft",
    resourceVersion,
    buildRunId: typeof raw.build_run_id === "string" ? raw.build_run_id : null,
    evalRunId: typeof raw.eval_run_id === "string" ? raw.eval_run_id : null,
    evalLocked: raw.eval_locked === true,
    modelApprovedBy: typeof raw.model_approved_by === "string" ? raw.model_approved_by : null,
    providerArtifactRef: typeof raw.provider_artifact_ref === "string" ? raw.provider_artifact_ref : null,
    compiledProvider: typeof raw.compiled_provider === "string" ? raw.compiled_provider : null,
    rootTraceId: typeof raw.root_trace_id === "string" ? raw.root_trace_id : null,
    publishRunId: typeof raw.publish_run_id === "string" ? raw.publish_run_id : null,
    taskVersionId: typeof raw.task_version_id === "string" ? raw.task_version_id : null,
    items: rawItems
      .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item))
      .map((item) => hotwordVersionItemView(item))
      .filter((item): item is HotwordPackVersionItemView => item !== null)
  };
};
