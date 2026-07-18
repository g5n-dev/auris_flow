import type { WorkspaceProjectSceneBinding } from "../shared/contracts/moduleWorkspaceGateway";
import type { ModuleKey } from "../shared/contracts/navigation";
import type { ModuleConfig, ModuleMetric } from "../shared/contracts/modules";

export const sceneMetricTones: ModuleMetric["tone"][] = ["blue", "green", "amber", "violet", "teal", "red"];

export function sceneAwareModuleConfig(
  baseConfig: ModuleConfig,
  moduleKey: Exclude<ModuleKey, "listening">,
  binding: WorkspaceProjectSceneBinding | null
): ModuleConfig {
  const manifest = binding?.version.manifest;
  if (!manifest) return baseConfig;

  const objectNames = [...manifest.entities, ...manifest.events, ...manifest.document_types]
    .map((item) => item.display_name);
  const scopeByModule: Partial<Record<Exclude<ModuleKey, "listening">, string>> = {
    home: `${manifest.display_name}的运行、异常、人工任务与最近资产`,
    projects: "场景版本、依赖闭包、发布门禁与生产绑定",
    canvas: `${manifest.task_type_refs.join("、") || "待配置任务类型"}的输入、处理、输出与调度`,
    data: `${objectNames.slice(0, 6).join("、") || "场景对象"}的数据契约与关联`,
    knowledge: `${manifest.knowledge_index_refs.join("、") || "待配置知识索引"}的来源、索引、质量与效果`,
    labels: `${manifest.label_version_refs.join("、")}的体系、候选、冲突与发布`,
    insights: `${manifest.metrics.map((item) => item.display_name).join("、")}的计算、证据与回写`,
    evaluation: `${manifest.eval_dataset_version_refs.join("、")}的三层评测与发布门禁`,
    assets: `${manifest.data_contract_refs.join("、")}的资产、血缘、物化与导出`,
    settings: `${manifest.display_name}的模型服务、隐私、留存与权限策略`
  };

  let metrics = baseConfig.metrics;
  if (moduleKey === "insights") {
    metrics = manifest.metrics.slice(0, 4).map((metric, index) => ({
      label: metric.display_name,
      value: "—",
      delta: `${metric.metric_key} · ${metric.unit}`,
      tone: sceneMetricTones[index % sceneMetricTones.length]
    }));
  } else if (moduleKey === "evaluation") {
    metrics = manifest.release_requirements.slice(0, 4).map((requirement, index) => ({
      label: manifest.metrics.find((metric) => metric.metric_key === requirement.metric_key)?.display_name ?? requirement.metric_key,
      value: `${(requirement.threshold_ppm / 10_000).toFixed(1)}%`,
      delta: `${requirement.gate_kind} · ${requirement.operator}`,
      tone: sceneMetricTones[index % sceneMetricTones.length]
    }));
  } else if (moduleKey === "knowledge") {
    metrics = [
      { label: "知识索引", value: String(manifest.knowledge_index_refs.length), delta: "SceneProfile 锁定引用", tone: "blue" },
      { label: "业务对象", value: String(objectNames.length), delta: "实体 / 事件 / 文档", tone: "green" },
      { label: "评测门禁", value: String(manifest.release_requirements.length), delta: "发布前必须验证", tone: "amber" },
      { label: "权威快照", value: binding.manifest_sha256.slice(0, 8), delta: binding.version.version, tone: "violet" }
    ];
  }

  return {
    ...baseConfig,
    eyebrow: `${manifest.display_name} / ${baseConfig.title}`,
    scope: scopeByModule[moduleKey] ?? baseConfig.scope,
    metrics
  };
}
