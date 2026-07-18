export type LabelArtifactLifecycleStatus =
  | "draft"
  | "candidate"
  | "validated"
  | "locked"
  | "evaluating"
  | "gate_blocked"
  | "review_required"
  | "approved"
  | "published"
  | "deprecated"
  | "archived";

export type LabelProductionActivation =
  | { state: "active"; generation: number; deploymentId: string | null }
  | { state: "inactive" | "unavailable" | "ambiguous"; generation: null; deploymentId: null };

export type LabelReplacementBinding =
  | { state: "mapped"; labelVersionId: string; mappingBundleId: string }
  | { state: "none" | "unavailable" | "incomplete"; labelVersionId: null; mappingBundleId: null };

export type LabelLifecycleSummary = {
  labelVersionId: string | null;
  status: LabelArtifactLifecycleStatus | null;
  publishedAt: string | null;
  deprecatedAt: string | null;
  archivedAt: string | null;
  deprecationReason: string | null;
  productionActivation: LabelProductionActivation;
  replacement: LabelReplacementBinding;
  issues: string[];
};

const artifactStatuses = new Set<LabelArtifactLifecycleStatus>([
  "draft",
  "candidate",
  "validated",
  "locked",
  "evaluating",
  "gate_blocked",
  "review_required",
  "approved",
  "published",
  "deprecated",
  "archived"
]);

function isRecordValue(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function nonEmptyString(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function lifecycleTime(value: unknown) {
  const normalized = nonEmptyString(value);
  return normalized && Number.isFinite(Date.parse(normalized)) ? normalized : null;
}

function productionActivation(resource: Record<string, unknown>): LabelProductionActivation {
  if (!Array.isArray(resource.environment_activations)) {
    return { state: "unavailable", generation: null, deploymentId: null };
  }
  const productionHeads = resource.environment_activations.filter(
    (candidate) =>
      isRecordValue(candidate)
      && ["prod", "production"].includes(String(candidate.environment).toLowerCase())
      && candidate.status === "active"
  );
  if (productionHeads.length === 0) {
    return { state: "inactive", generation: null, deploymentId: null };
  }
  if (productionHeads.length !== 1) {
    return { state: "ambiguous", generation: null, deploymentId: null };
  }
  const [head] = productionHeads;
  const generation = head.generation;
  if (!Number.isInteger(generation) || Number(generation) < 1) {
    return { state: "unavailable", generation: null, deploymentId: null };
  }
  return {
    state: "active",
    generation: Number(generation),
    deploymentId: nonEmptyString(head.active_deployment_id)
  };
}

function replacementBinding(resource: Record<string, unknown>): LabelReplacementBinding {
  if (!("replacement" in resource)) {
    return { state: "unavailable", labelVersionId: null, mappingBundleId: null };
  }
  if (resource.replacement === null) {
    return { state: "none", labelVersionId: null, mappingBundleId: null };
  }
  if (!isRecordValue(resource.replacement)) {
    return { state: "incomplete", labelVersionId: null, mappingBundleId: null };
  }
  const labelVersionId = nonEmptyString(resource.replacement.label_version_id);
  const mappingBundleId = nonEmptyString(resource.replacement.mapping_bundle_id);
  if (!labelVersionId || !mappingBundleId) {
    return { state: "incomplete", labelVersionId: null, mappingBundleId: null };
  }
  return { state: "mapped", labelVersionId, mappingBundleId };
}

export function parseLabelLifecycleSummary(value: unknown): LabelLifecycleSummary {
  const resource = isRecordValue(value) ? value : {};
  const artifactLifecycle = isRecordValue(resource.artifact_lifecycle)
    ? resource.artifact_lifecycle
    : null;
  const rawStatus = artifactLifecycle?.status;
  const status = typeof rawStatus === "string" && artifactStatuses.has(rawStatus as LabelArtifactLifecycleStatus)
    ? rawStatus as LabelArtifactLifecycleStatus
    : null;
  const activation = productionActivation(resource);
  const replacement = replacementBinding(resource);
  const issues: string[] = [];

  if (!artifactLifecycle) issues.push("生命周期字段未返回");
  else if (!status) issues.push("生命周期状态不可识别");
  if (activation.state === "unavailable") issues.push("production 激活信息未返回");
  if (activation.state === "ambiguous") issues.push("production 激活指针不唯一");
  if (replacement.state === "unavailable") issues.push("替代关系字段未返回");
  if (replacement.state === "incomplete") issues.push("替代版本与映射包绑定不完整");

  return {
    labelVersionId: nonEmptyString(resource.label_version_id) ?? nonEmptyString(resource.id),
    status,
    publishedAt: lifecycleTime(artifactLifecycle?.published_at),
    deprecatedAt: lifecycleTime(artifactLifecycle?.deprecated_at),
    archivedAt: lifecycleTime(artifactLifecycle?.archived_at),
    deprecationReason: nonEmptyString(artifactLifecycle?.deprecation_reason),
    productionActivation: activation,
    replacement,
    issues
  };
}
