import { useState } from "react";
import type { MockMutationRecord, ModuleCommandMode } from "../shared/contracts/application";
import type { ModuleInteractionModel } from "../shared/contracts/moduleInteractions";
import type { ModuleWorkspaceGateway, WorkspaceProjectSceneBinding } from "../shared/contracts/moduleWorkspaceGateway";
import type { ModuleKey } from "../shared/contracts/navigation";
import type { ModuleConfig } from "../shared/contracts/modules";
import type { OperationStatus } from "../shared/contracts/operations";
import type { TopbarContextState } from "../shared/contracts/workspace";
import { backendRunStatusLabel, operationStatusFromBackendRun } from "../shared/runtime/backendRunStatus";
import { refreshBackendRunReceipt } from "./backendRunReceipt";
import { buildMockMutationRecord } from "./moduleWorkspaceCatalog";

type ModuleCommandsInput = {
  gateway: Pick<ModuleWorkspaceGateway, "createExportRun" | "createPlatformMutation" | "getBackendRun">;
  moduleKey: Exclude<ModuleKey, "listening">;
  activeTab: string;
  config: ModuleConfig;
  interaction: ModuleInteractionModel;
  topbarContext: TopbarContextState;
  selectedAssetKey: string;
  workspaceSceneBinding: WorkspaceProjectSceneBinding | null;
};

export function useModuleCommands({
  gateway,
  moduleKey,
  activeTab,
  config,
  interaction,
  topbarContext,
  selectedAssetKey,
  workspaceSceneBinding
}: ModuleCommandsInput) {
  const [commandMode, setCommandMode] = useState<ModuleCommandMode | null>(null);
  const [moduleQuery, setModuleQuery] = useState("");
  const [activeFilter, setActiveFilter] = useState(interaction.filters[0]?.label ?? "全部");
  const [commandFeedback, setCommandFeedback] = useState("");
  const [commandStatus, setCommandStatus] = useState<OperationStatus>("idle");
  const [exportReceipt, setExportReceipt] = useState("");
  const [mutationRecords, setMutationRecords] = useState<MockMutationRecord[]>([]);
  const activeSceneManifest = workspaceSceneBinding?.version.manifest ?? null;
  const activeFilterMeta = interaction.filters.find((filter) => filter.label === activeFilter) ?? interaction.filters[0];

  const toggleCommandMode = (mode: ModuleCommandMode) => {
    const isClosing = commandMode === mode;
    setCommandMode(isClosing ? null : mode);
    if (isClosing || mode !== "export") return;
    const generatedAt = new Date().toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    });
    const exportScope = activeFilterMeta?.result ?? "当前视图";
    const exportPayload = {
      target: "module_view",
      object_id: `${moduleKey}:${activeTab}:${activeFilterMeta?.label ?? "all"}`,
      format: "jsonl",
      source: "ui_global_command",
      module_key: moduleKey,
      module_title: config.title,
      export_name: interaction.exportName,
      active_tab: activeTab,
      filter: activeFilterMeta?.label ?? "全部",
      filter_result: exportScope,
      context: topbarContext
    };
    setCommandStatus("pending");
    setExportReceipt(`${interaction.exportName} 正在请求后端导出 · ${generatedAt}`);
    setCommandFeedback(`正在创建导出运行：${exportScope}。`);
    void gateway.createExportRun(exportPayload)
      .then(async (receipt) => {
        const runState = await refreshBackendRunReceipt(receipt.data, gateway);
        const runStatus = operationStatusFromBackendRun(runState.status);
        const traceId = receipt.meta?.trace_id ?? runState.trace_id ?? "trace_pending";
        const traceLabel = traceId.slice(0, 12);
        setCommandStatus(runStatus);
        setExportReceipt(
          `${interaction.exportName} 已创建导出运行 ${runState.id} · ${backendRunStatusLabel(runState.status)} · trace ${traceLabel}`
        );
        setCommandFeedback(
          `导出运行已创建：${runState.id}，当前${backendRunStatusLabel(runState.status)}，Trace ${traceId}。`
        );
      })
      .catch((error) => {
        setCommandStatus("error");
        setExportReceipt(`${interaction.exportName} 导出请求失败 · 本地范围草稿已保留`);
        setCommandFeedback(`导出请求失败，未创建后端导出任务：${error instanceof Error ? error.message : "unknown error"}`);
      });
  };

  const submitMutationRecord = async (record: MockMutationRecord) => {
    if (!workspaceSceneBinding && !["tenants", "projects", "settings"].includes(moduleKey)) {
      setCommandStatus("error");
      setCommandFeedback("当前项目尚未绑定已发布 SceneProfile，已阻断项目级写入；请先在项目管理完成校验、独立复核、发布和绑定。");
      return;
    }
    setCommandStatus("pending");
    setCommandFeedback(`${record.action} 已生成${record.status}：${record.entityKey}。正在写入后端工作项。`);
    try {
      const sceneLock = workspaceSceneBinding
        ? {
            scene_profile_id: workspaceSceneBinding.scene_profile_id,
            scene_profile_version_id: workspaceSceneBinding.scene_profile_version_id,
            scene_profile_snapshot_sha256: workspaceSceneBinding.manifest_sha256
          }
        : {};
      const receipt = await gateway.createPlatformMutation(moduleKey, {
        module_key: moduleKey,
        module_title: config.title,
        action: record.action,
        target: record.target,
        route: record.route,
        entity_key: record.entityKey,
        payload: record.payload,
        guardrail: record.guardrail,
        downstream: record.downstream,
        context: topbarContext,
        ...sceneLock,
        ...(activeSceneManifest?.task_type_refs[0] ? { task_type_id: activeSceneManifest.task_type_refs[0] } : {}),
        ...(activeSceneManifest?.label_version_refs[0]
          ? { label_version_id: activeSceneManifest.label_version_refs[0], base_version: activeSceneManifest.label_version_refs[0] }
          : {}),
        ...(activeSceneManifest?.knowledge_index_refs[0] ? { knowledge_index_id: activeSceneManifest.knowledge_index_refs[0] } : {}),
        ...(moduleKey === "assets" && selectedAssetKey ? { asset_key: selectedAssetKey } : {}),
        ...(moduleKey === "evaluation" && activeSceneManifest?.eval_dataset_version_refs[0]
          ? {
              dataset_id: activeSceneManifest.eval_dataset_version_refs[0],
              dataset_version: activeSceneManifest.eval_dataset_version_refs[0],
              model_version: topbarContext.model,
              label_version: activeSceneManifest.label_version_refs[0] ?? topbarContext.label,
              source: "ui_module_command"
            }
          : {})
      });
      const traceId = receipt.meta?.trace_id ?? receipt.data.trace_id ?? "trace_pending";
      setCommandStatus("success");
      setMutationRecords((current) => current.map((currentRecord) => currentRecord.id === record.id
        ? {
            ...currentRecord,
            status: "已提交",
            backendId: receipt.data.id,
            backendStatus: receipt.data.status,
            traceId,
            entityKey: `${currentRecord.entityKey} · ${receipt.data.id}`,
            guardrail: `${currentRecord.guardrail} · trace ${traceId}`,
            unavailableReason: "后端未返回可执行 next_action；审核和状态转移只能由对应资源 API 驱动。"
          }
        : currentRecord));
      setCommandFeedback(`${record.action} 已写入后端实体：${receipt.data.id}，状态 ${receipt.data.status}，Trace ${traceId}。`);
    } catch (error) {
      setCommandStatus("error");
      setMutationRecords((current) => current.map((currentRecord) => currentRecord.id === record.id
        ? {
            ...currentRecord,
            status: "失败",
            backendStatus: "failed",
            unavailableReason: "创建请求失败，可使用同一用户意图幂等重试。",
            guardrail: `${currentRecord.guardrail} · 写入失败：${error instanceof Error ? error.message : "unknown error"}`
          }
        : currentRecord));
      setCommandFeedback(`后端写入失败，未生成成功实体：${error instanceof Error ? error.message : "unknown error"}`);
    }
  };

  const createMutationRecord = (item: ModuleInteractionModel["crud"][number]) => {
    const record = buildMockMutationRecord(moduleKey, config, item);
    setMutationRecords((current) => [record, ...current].slice(0, 12));
    void submitMutationRecord(record);
  };
  const retryMutationRecord = (id: string) => {
    const record = mutationRecords.find((item) => item.id === id);
    if (!record || record.status !== "失败") return;
    setMutationRecords((current) => current.map((item) => item.id === id ? { ...item, status: "校验中" } : item));
    void submitMutationRecord({ ...record, status: "校验中" });
  };

  return {
    activeFilter,
    closeCommandPanel: () => setCommandMode(null),
    commandFeedback,
    commandMode,
    commandStatus,
    createMutationRecord,
    currentMutationRecords: mutationRecords.filter((record) => record.moduleKey === moduleKey),
    exportReceipt,
    moduleQuery,
    retryMutationRecord,
    setActiveFilter,
    setCommandFeedback,
    setCommandMode,
    setCommandStatus,
    setModuleQuery,
    toggleCommandMode
  };
}

export type ModuleCommands = ReturnType<typeof useModuleCommands>;
