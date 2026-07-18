import type { ControlledExperimentVariantDimension } from "../../../api/client";

export const experimentDimensionFields: Record<
  Exclude<ControlledExperimentVariantDimension, "bundle">,
  string[]
> = {
  workflow: ["canvas_variant", "canvas_version_id", "workflow_version", "workflow_compiler_version", "task_definition_ref", "execution_plan_sha256", "graph", "execution"],
  model: ["model_version", "model_service_ref", "model_bindings", "audio_intelligence", "hotword_pack_version_id", "hotword_binding_mode"],
  prompt: ["prompt_version_id", "prompt_version", "prompt_bindings"],
  label_policy: ["policy_version_id", "label_policy_version_id", "aggregation_policy_version_id", "calibration_policy_version_id", "threshold_policy_version_id", "label_policy_bindings"]
};

export function taskVersionId(version: Record<string, unknown> | undefined): string {
  return String(version?.task_version_id ?? version?.id ?? "");
}

export function taskVersionDimensionDocument(version: Record<string, unknown>, fields: string[]) {
  return Object.fromEntries(fields.filter((field) => version[field] !== undefined).map((field) => [field, version[field]]));
}
