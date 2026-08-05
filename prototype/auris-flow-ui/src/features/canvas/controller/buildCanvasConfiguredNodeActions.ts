import type { CanvasActionScope } from "./canvasActionScope";
import { dagsterDefinitionForTemplate, slugifyDagsterName } from "../nodeTemplates";
import type { AddableCanvasNode } from "../types";

export function buildCanvasConfiguredNodeActions(scope: CanvasActionScope) {
  const {
    activeIntent,
    activeIntentKey,
    addedNodes,
    demoMode,
    displayExecutionDefinition,
    draftOutputContract,
    markTaskDraftDirty,
    nodeDraft,
    pushRunHistory,
    selectedTemplate,
    setAddedNodes,
    setCanvasNotice,
    setDrawerTab,
    setExecutionState,
    setNodeLibraryOpen,
    setSelectedNodeId
  } = scope;

  const addConfiguredNode = () => {
    const template = selectedTemplate;
    if (!demoMode && template.productionDisabled) {
      setCanvasNotice({
        status: "error",
        title: "生产节点已阻断",
        detail: template.disabledReason ?? "该兼容节点没有专用生产执行契约，不能加入生产任务版本。"
      });
      return;
    }
    const sameTemplateCount = addedNodes.filter((node) => node.id.startsWith(template.key)).length + 1;
    const nextIndex = addedNodes.length;
    const nodeId = `${template.key}-${Date.now()}`;
    const cleanName = nodeDraft.name.trim() || template.node.name;
    const nodeName = sameTemplateCount > 1 ? `${cleanName} ${sameTemplateCount}` : cleanName;
    const nodeRole = nodeDraft.role.trim() || template.node.role;
    const dataKey = nodeDraft.sourceId.trim() || nodeDraft.dataKey.trim() || template.node.metaB;
    const endpointValue = nodeDraft.endpoint.trim() || template.endpoint || template.node.metaB;
    const httpMethod = nodeDraft.httpMethod.trim() || template.method || template.node.metaA;
    const inputValue = nodeDraft.input.trim() || activeIntent.taskId;
    const outputValue = nodeDraft.output.trim() || activeIntent.output;
    const dagsterOp = nodeDraft.dagsterOp.trim() || `run_${slugifyDagsterName(nodeName)}`;
    const dagsterAsset = nodeDraft.dagsterAsset.trim() || `auris/task/${activeIntent.taskId}/${slugifyDagsterName(nodeName)}`;
    const ioManager = nodeDraft.ioManager.trim() || "task_version_io_manager";
    const dagsterDefinition = dagsterDefinitionForTemplate(template);
    const dagsterDeps = inputValue === "none" ? [] : [inputValue];
    const outputContract = draftOutputContract;
    const adapterFields: Array<[string, string]> = template.adapterKind
      ? [
          ["适配类型", template.adapterKind],
          ["HTTP", `${httpMethod} ${endpointValue}${nodeDraft.queryParams.trim() ? `?${nodeDraft.queryParams.trim()}` : ""}`],
          ["认证方式", template.authMode ?? "继承任务认证"],
          ["数据修改位置", "节点库 / 数据配置 / 演示数据"],
          ["Source ID", dataKey],
          ["资源类型", nodeDraft.resourceType],
          ["输出资产", outputContract.assetKey],
          ["资产分区", outputContract.partition],
          ["聚合键", outputContract.aggregateKeys.join(" / ")],
          ["字段映射", nodeDraft.fieldMapping],
          ["写入策略", nodeDraft.writePolicy],
          ["执行定义", displayExecutionDefinition(dagsterDefinition)],
          ["依赖提示", template.depsHint ?? inputValue]
        ]
      : [];
    const nextNode: AddableCanvasNode = {
      id: nodeId,
      name: nodeName,
      icon: template.node.icon,
      x: template.x + (nextIndex % 3) * 34,
      y: template.y + Math.floor(nextIndex / 3) * 46,
      status: template.node.status,
      metaA: template.node.metaA,
      metaB: endpointValue,
      role: nodeRole,
      confidence: template.node.confidence,
      intentKeys: Array.from(new Set([...template.node.intentKeys, activeIntentKey])),
      tags: template.node.tags,
      dagsterBinding: {
        definition: dagsterDefinition,
        op: dagsterOp,
        assetKey: dagsterAsset,
        ioManager,
        partition: activeIntent.scope,
        deps: dagsterDeps
      },
      context: {
        ...template.context,
        relation: `当前任务新增「${nodeName}」，通过 ${httpMethod} ${endpointValue} 读取 ${dataKey}，输入 ${inputValue}，输出资产 ${outputValue}。`,
        version: sameTemplateCount > 1 ? `${template.context.version} #${sameTemplateCount}` : template.context.version,
        fields: [
          ["节点名称", nodeName],
          ["配置类型", template.category],
          ["数据修改位置", "节点库 / 数据配置 / 演示数据"],
          ["引用/数据 Key", dataKey],
          ...adapterFields,
          ["输入", inputValue],
          ["输出资产", outputValue],
          ["聚合键", outputContract.aggregateKeys.join(" / ")],
          ["物化语义", outputContract.materialization],
          ["底层 Op", dagsterOp],
          ["Asset Key", dagsterAsset],
          ["IO Manager", ioManager]
        ]
      }
    };
    setAddedNodes((current) => [...current, nextNode]);
    setSelectedNodeId(nodeId);
    setDrawerTab("overview");
    setNodeLibraryOpen(false);
    setExecutionState("idle");
    markTaskDraftDirty();
    setCanvasNotice({
      status: "success",
      title: "节点已加入当前任务草稿",
      detail: `${nodeName} 已写入当前编排版本，发布前需要保存草稿并通过兼容性校验。`
    });
    pushRunHistory(`FlowNode · ${nodeName}`, "草稿已更新");
  };

  return { addConfiguredNode };
}

export type CanvasConfiguredNodeActions = ReturnType<typeof buildCanvasConfiguredNodeActions>;
