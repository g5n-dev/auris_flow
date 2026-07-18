import {
  Bell,
  BrainCircuit,
  Database,
  GitBranch,
  Headphones,
  Link2,
  RotateCcw,
  ShieldCheck,
  UserCheck
} from "lucide-react";
import type { ComponentType } from "react";

import staticCatalog from "../../modules/staticCatalog";
import { baseDagsterBindings, canvasIntents } from "./catalog";
import type {
  AssetOutputContract,
  CanvasNode,
  CanvasNodeDraft,
  CanvasNodeTemplate,
  DagsterBinding
} from "./types";

export const slugifyDagsterName = (value: string) =>
  value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, "_")
    .replace(/^_+|_+$/g, "") || "custom_node";

export const dagsterBindingForNode = (node: CanvasNode, intent: (typeof canvasIntents)[number]): DagsterBinding => {
  if (node.dagsterBinding) return { ...node.dagsterBinding, partition: node.dagsterBinding.partition || intent.scope };
  const base = baseDagsterBindings[node.id];
  if (base) return { ...base, partition: intent.scope };
  const slug = slugifyDagsterName(node.name);
  const definition = node.role.includes("输出")
    ? "Asset"
    : node.role.includes("输入")
      ? "SourceAsset"
      : node.role.includes("控制")
        ? "GraphNode"
        : "Op";
  return {
    definition,
    op: `${definition === "SourceAsset" ? "load" : definition === "Asset" ? "materialize" : "run"}_${slug}`,
    assetKey: `auris/custom/${slug}`,
    ioManager: definition === "SourceAsset" ? "input_binding_io_manager" : "task_version_io_manager",
    partition: intent.scope,
    deps: [intent.taskId]
  };
};

export const canvasNodeSize = (nodeId: string) => {
  if (nodeId === "ai") return { width: 124, height: 126 };
  if (nodeId === "entityMap") return { width: 220, height: 132 };
  if (nodeId === "dagster") return { width: 246, height: 132 };
  return { width: 250, height: 152 };
};

export const canvasStageBounds = {
  width: 1510,
  height: 820,
  padding: 16,
  bottomPadding: 82
};

export const canvasTemplateIconMap = {
  Bell,
  BrainCircuit,
  Database,
  GitBranch,
  Headphones,
  Link2,
  RotateCcw,
  ShieldCheck,
  UserCheck
} satisfies Record<string, ComponentType<{ size?: number }>>;

export type CanvasNodeTemplateCatalogItem = Omit<CanvasNodeTemplate, "node"> & {
  node: Omit<CanvasNodeTemplate["node"], "icon"> & { icon: keyof typeof canvasTemplateIconMap };
};

export const canvasNodeTemplates: CanvasNodeTemplate[] = ((staticCatalog as { canvasNodeTemplates: CanvasNodeTemplateCatalogItem[] }).canvasNodeTemplates as CanvasNodeTemplateCatalogItem[]).map((template) => ({
  ...template,
  node: {
    ...template.node,
    icon: canvasTemplateIconMap[template.node.icon] ?? Database
  }
}));

export const dagsterDefinitionForTemplate = (template: CanvasNodeTemplate) =>
  template.dagsterDefinition ??
  (template.category === "平台数据同步抽取"
    ? "SourceAsset"
    : template.category === "平台处理结果推送"
      ? "Asset"
      : template.category === "人工与控制"
        ? "GraphNode"
        : "Op");

export const nodeWriteModeForTemplate = (template: CanvasNodeTemplate) => {
  const definition = dagsterDefinitionForTemplate(template);
  if (template.category === "平台数据同步抽取") return `PlatformSyncBinding + ${definition}`;
  if (template.category === "平台处理结果推送") return `PlatformPushBinding + ${definition}`;
  if (template.category === "人工与控制") return `ControlNode + ${definition}`;
  return `StepConfig + ${definition}`;
};

export const assetContractForTemplate = (template: CanvasNodeTemplate, intent: (typeof canvasIntents)[number]): AssetOutputContract => {
  if (template.outputContract) return template.outputContract;
  const slug = slugifyDagsterName(template.node.name);
  const definition = dagsterDefinitionForTemplate(template);
  const assetKey = definition === "SourceAsset" ? `auris/sources/${slug}` : `auris/task/${intent.taskId}/${slug}`;
  const sourceInput = template.depsHint ?? template.context.fields.find(([label]) => label.includes("输入") || label.includes("引用"))?.[1] ?? intent.taskId;
  return {
    assetKey,
    displayName: template.context.fields.find(([label]) => label.includes("输出资产"))?.[1] ?? template.title,
    kind: definition,
    description: template.context.relation,
    api: template.endpoint ? `${template.method ?? template.node.metaA} ${template.endpoint}` : `${definition} / ${template.node.metaB}`,
    partition: intent.scope,
    materialization: definition === "SourceAsset" ? "SourceAssetObservation" : "AssetMaterialization",
    upstream: sourceInput === "none" ? [] : [sourceInput],
    aggregateKeys: ["tenant_id", "project_id", "partition_key", "trace_id"],
    schema: template.outputSchema?.map((field): [string, string] => [field, "字段归属当前资产契约，不作为扁平输出配置"]) ?? [["asset_ref", assetKey]]
  };
};

export const sourceResourceDefaultForTemplate = (template: CanvasNodeTemplate) => {
  if (template.key === "rest-source-adapter") return "tenant|employee|store";
  if (template.key === "audio-url-adapter") return "recording_url";
  if (template.key === "event-source-adapter") return "authenticated_event";
  if (template.key === "aizj-sync-job") return "sync_batch";
  if (template.key === "platform-login-adapter") return "platform_session";
  return slugifyDagsterName(template.node.name);
};

export const mockPayloadForTemplate = (template: CanvasNodeTemplate, outputContract: AssetOutputContract) =>
  JSON.stringify(
    {
      source_id: sourceResourceDefaultForTemplate(template),
      asset_key: outputContract.assetKey,
      cursor: "updated_at:2025-05-26T12:31:08+08:00",
      records: [
        {
          resource_id: "store_aurora_center",
          tenant_id: "aurora_auto",
          store_id: "aurora-center",
          employee_id: "emp_1001",
          display_name: "极光中心店 / 销售A",
          source_updated_at: "2025-05-26T12:31:08+08:00",
          raw_payload_ref: "demo://platform/raw/tenant-store-employee"
        }
      ]
    },
    null,
    2
  );

export const splitDraftList = (value: string) =>
  value
    .split(/[,\n/×]+/)
    .map((item) => item.trim())
    .filter(Boolean);

export const defaultDraftForTemplate = (template: CanvasNodeTemplate, intent: (typeof canvasIntents)[number]): CanvasNodeDraft => {
  const slug = slugifyDagsterName(template.node.name);
  const categoryPrefix =
    template.defaultOpPrefix ??
    (template.category === "平台数据同步抽取"
      ? "sync"
      : template.category === "平台处理结果推送"
        ? "materialize"
        : template.category === "人工与控制"
          ? "route"
          : "run");
  const outputContract = assetContractForTemplate(template, intent);
  const firstOutput = outputContract.assetKey;
  const firstInput =
    template.depsHint ?? template.context.fields.find(([label]) => label.includes("输入") || label.includes("引用") || label.includes("规则"))?.[1] ?? intent.taskId;
  const definition = dagsterDefinitionForTemplate(template);
  const endpoint = template.endpoint ?? template.node.metaB;
  const httpMethod = template.method ?? template.node.metaA;
  const resourceType = sourceResourceDefaultForTemplate(template);
  return {
    name: template.node.name,
    dataKey: outputContract.assetKey,
    role: template.node.role,
    input: firstInput,
    output: firstOutput,
    httpMethod,
    endpoint,
    sourceId: `${resourceType}_source`,
    resourceType,
    queryParams: outputContract.api.includes("?") ? outputContract.api.split("?")[1] : `resource=${resourceType}`,
    partitionRule: outputContract.partition,
    aggregateKeys: outputContract.aggregateKeys.join(" / "),
    fieldMapping: outputContract.schema.map(([group, fields]) => `${group}: ${fields}`).join("\n"),
    mockPayload: mockPayloadForTemplate(template, outputContract),
    writePolicy: "保存到当前任务版本草稿，不修改全局数据源",
    dagsterOp: `${categoryPrefix}_${slug}`,
    dagsterAsset: outputContract.assetKey,
    ioManager: template.defaultIoManager ?? (template.category === "平台数据同步抽取" ? "platform_sync_io_manager" : "task_version_io_manager")
  };
};
