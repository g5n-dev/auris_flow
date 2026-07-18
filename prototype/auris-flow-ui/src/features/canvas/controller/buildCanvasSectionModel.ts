import type { CanvasModuleProps } from "../types";
import type { CanvasState } from "./useCanvasState";
import type { CanvasPrimitiveActions } from "./buildCanvasPrimitiveActions";
import type { CanvasRecoveryModel } from "./useCanvasRecovery";
import { canvasSectionDescriptors } from "../fixtures/viewDescriptors";
import { assetContractForTemplate, splitDraftList } from "../nodeTemplates";
import type { AssetOutputContract, MappingSuggestionState, TaskSectionMeta } from "../types";

export function buildCanvasSectionModel(scope: CanvasModuleProps & CanvasState & CanvasPrimitiveActions & CanvasRecoveryModel) {
  const { activeIntent, activeIntentKey, activeTab, contextPartitionKey, contextProjectId, contextTenantId, dagsterRunDraft, mappingConfidenceThreshold, mappingSuggestionsByIntent, nodeDraft, selectedCanvasVariant, selectedMappingId, selectedTaskType, selectedTemplate } = scope;
  const selectedOutputContract = assetContractForTemplate(selectedTemplate, activeIntent);
  const draftAggregateKeys = splitDraftList(nodeDraft.aggregateKeys);
  const draftApiPath = `${nodeDraft.httpMethod || selectedTemplate.method || selectedTemplate.node.metaA} ${nodeDraft.endpoint || selectedTemplate.endpoint || selectedTemplate.node.metaB}${nodeDraft.queryParams.trim() ? `?${nodeDraft.queryParams.trim()}` : ""}`;
  const draftOutputContract: AssetOutputContract = {
    ...selectedOutputContract,
    api: draftApiPath,
    partition: nodeDraft.partitionRule || selectedOutputContract.partition,
    aggregateKeys: draftAggregateKeys.length ? draftAggregateKeys : selectedOutputContract.aggregateKeys,
    schema: nodeDraft.fieldMapping.trim()
      ? nodeDraft.fieldMapping.split("\n").map((row): [string, string] => {
          const [group, ...fields] = row.split(":");
          return [group.trim() || "field", fields.join(":").trim() || "待配置"];
        })
      : selectedOutputContract.schema
  };

  const sectionMeta: Record<string, TaskSectionMeta> = Object.fromEntries(
    Object.entries(canvasSectionDescriptors).map(([key, descriptor]) => [key, { ...descriptor }])
  );
  const activeSection = sectionMeta[activeTab] ?? sectionMeta.flow;
  const isFlowTab = activeTab === "flow";
  const activePartitionKey = activeIntentKey === "review" ? contextPartitionKey : activeIntent.scope;
  const activeRunKey = `${selectedTaskType.key}:${selectedCanvasVariant.key}:${contextTenantId || "unbound"}:${contextProjectId || "unbound"}:${dagsterRunDraft.partitionKey || activePartitionKey}`;
  const activeMappingSuggestions = mappingSuggestionsByIntent[activeIntentKey] ?? [];
  const selectedMapping = activeMappingSuggestions.find((item) => item.id === selectedMappingId) ?? activeMappingSuggestions[0];
  const appliedMappingCount = activeMappingSuggestions.filter((item) => item.state === "applied").length;
  const confirmedMappingCount = activeMappingSuggestions.filter((item) => item.state === "confirmed").length;
  const pendingMappingCount = activeMappingSuggestions.filter((item) => item.state === "pending").length;
  const rejectedMappingCount = activeMappingSuggestions.filter((item) => item.state === "rejected").length;
  const mappingTotal = activeMappingSuggestions.length || 1;
  const mappingCompletionPct = Math.round((appliedMappingCount / mappingTotal) * 100);
  const mappingStateLabel: Record<MappingSuggestionState, string> = {
    pending: "待确认",
    confirmed: "已确认",
    applied: "已应用",
    rejected: "已拒绝"
  };
  const trustedMappingCount = activeMappingSuggestions.filter(
    (item) => item.confidence >= mappingConfidenceThreshold && (item.state === "pending" || item.state === "confirmed")
  ).length;

  return {
    selectedOutputContract,
    draftAggregateKeys,
    draftApiPath,
    draftOutputContract,
    sectionMeta,
    activeSection,
    isFlowTab,
    activePartitionKey,
    activeRunKey,
    activeMappingSuggestions,
    selectedMapping,
    appliedMappingCount,
    confirmedMappingCount,
    pendingMappingCount,
    rejectedMappingCount,
    mappingTotal,
    mappingCompletionPct,
    mappingStateLabel,
    trustedMappingCount
  };
}

export type CanvasSectionModel = ReturnType<typeof buildCanvasSectionModel>;
