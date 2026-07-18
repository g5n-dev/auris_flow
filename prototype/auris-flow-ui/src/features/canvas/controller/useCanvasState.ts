import type { CanvasModuleProps } from "../types";
import type { BackendActionReceipt, ControlledExperiment, ControlledExperimentVariantDimension } from "../../../api/client";
import type { OperationNotice } from "../../../shared/contracts/operations";
import { canvasIntents } from "../catalog";
import { createDefaultMappingSuggestions } from "../fixtures/intentsMapping";
import { canvasNodeTemplates, defaultDraftForTemplate, slugifyDagsterName } from "../nodeTemplates";
import { defaultScheduleConfigs } from "../scheduleModel";
import type { AddableCanvasNode, CanvasDragState, CanvasDrawerTab, CanvasIntentKey, CanvasNodeDraft, CanvasNodePosition, CanvasRunLog, DagsterRunDraft, ExecutionState, FlowStageKey, MappingSuggestion, TaskScheduleMode } from "../types";
import { useRef, useState } from "react";

export type ExperimentConfigDraft = {
  variantDimension: ControlledExperimentVariantDimension;
  controlTaskVersionId: string;
  candidateTaskVersionId: string;
  candidateAllocationPpm: number;
  allocationUnit: "audio_session" | "conversation" | "store" | "user" | "device" | "business_object";
  minSampleSizePerArm: number;
  confidenceLevel: 0.9 | 0.95 | 0.99;
};

export function useCanvasState(scope: CanvasModuleProps) {
  const { apiContext, sceneBinding } = scope;
  const sceneManifest = sceneBinding?.version.manifest ?? null;

  const declaredTaskTypeId = sceneManifest?.task_type_refs[0] ?? "";

  const contextTenantId = String(apiContext.tenantId ?? "").trim();

  const contextProjectId = String(apiContext.projectId ?? "").trim();

  const contextStore = String(apiContext.store ?? "").trim();

  const contextDate = String(apiContext.date ?? "").trim();

  const contextPartitionKey = [contextDate, contextStore].filter(Boolean).join("|") || "unpartitioned";

  const initialIntent: CanvasIntentKey = "review";

  const [activeIntentKey, setActiveIntentKey] = useState<CanvasIntentKey>(initialIntent);

  const [activeStageKey, setActiveStageKey] = useState<FlowStageKey>("ingest");

  const [selectedNodeId, setSelectedNodeId] = useState("platformAuth");

  const [drawerTab, setDrawerTab] = useState<CanvasDrawerTab>("overview");

  const [executionState, setExecutionState] = useState<ExecutionState>("idle");

  const [mappingSuggestionsByIntent, setMappingSuggestionsByIntent] = useState<Record<CanvasIntentKey, MappingSuggestion[]>>(() =>
      createDefaultMappingSuggestions()
    );

  const [selectedMappingId, setSelectedMappingId] = useState("risk_event_type");

  const [mappingConfidenceThreshold, setMappingConfidenceThreshold] = useState(92);

  const [nodeLibraryOpen, setNodeLibraryOpen] = useState(false);

  const [addedNodes, setAddedNodes] = useState<AddableCanvasNode[]>([]);

  const [nodePositions, setNodePositions] = useState<Record<string, CanvasNodePosition>>({});

  const [dragState, setDragState] = useState<CanvasDragState | null>(null);

  const [selectedTemplateKey, setSelectedTemplateKey] = useState(canvasNodeTemplates[0].key);

  const [nodeDraft, setNodeDraft] = useState<CanvasNodeDraft>(() => defaultDraftForTemplate(canvasNodeTemplates[0], canvasIntents[3]));

  const [scheduleMode, setScheduleMode] = useState<TaskScheduleMode>("定时运行");

  const [scheduleConfigs, setScheduleConfigs] = useState<Record<TaskScheduleMode, Record<string, string>>>(defaultScheduleConfigs);

  const [backfillConfirmed, setBackfillConfirmed] = useState(false);

  const [draftState, setDraftState] = useState<"未保存" | "已保存" | "已发布">("未保存");

  const [savedTaskVersionId, setSavedTaskVersionId] = useState<string | null>(null);

  const savedTaskVersionIdRef = useRef<string | null>(null);

  const rememberTaskVersionId = (taskVersionId: string | null) => {
      savedTaskVersionIdRef.current = taskVersionId;
      setSavedTaskVersionId(taskVersionId);
    };

  const [recoveredTaskVersion, setRecoveredTaskVersion] = useState<Record<string, unknown> | null>(null);

  const [taskReleaseGate, setTaskReleaseGate] = useState<BackendActionReceipt | null>(null);

  const [selectedTaskTypeKey, setSelectedTaskTypeKey] = useState(declaredTaskTypeId || "evidence-dataflow");

  const [selectedCanvasVariantKey, setSelectedCanvasVariantKey] = useState("stable-v3");

  const [experimentMode, setExperimentMode] = useState("未创建");

  const [selectedExperimentMetricKey, setSelectedExperimentMetricKey] = useState("");

  const [metricDraftState, setMetricDraftState] = useState<"AI草稿" | "已加入观测" | "发布闸门">("AI草稿");

  const [controlledExperiment, setControlledExperiment] = useState<ControlledExperiment | null>(null);

  const [experimentTaskVersions, setExperimentTaskVersions] = useState<Array<Record<string, unknown>>>([]);

  const [experimentLoading, setExperimentLoading] = useState(false);

  const [experimentActionPending, setExperimentActionPending] = useState<string | null>(null);

  const [experimentSubjectKey, setExperimentSubjectKey] = useState("");

  const [experimentConfigDraft, setExperimentConfigDraft] = useState<ExperimentConfigDraft>({
    variantDimension: "workflow",
    controlTaskVersionId: "",
    candidateTaskVersionId: "",
    candidateAllocationPpm: 200_000,
    allocationUnit: "audio_session",
    minSampleSizePerArm: 30,
    confidenceLevel: 0.95
  });

  const [canvasLevel, setCanvasLevel] = useState<"abstract" | "nodes">("abstract");

  const [canvasNotice, setCanvasNotice] = useState<OperationNotice>({
      status: "idle",
      title: "等待配置动作",
      detail: "添加节点、生成映射、校验、运行、调度和发布都会记录到当前任务版本。"
    });

  const [canvasAction, setCanvasAction] = useState<string | null>(null);

  const [asrExecutionMode, setAsrExecutionMode] = useState<"production" | "shadow">("production");

  const [asrHotwordVersionId, setAsrHotwordVersionId] = useState("");

  const [hotwordPackVersionOptions, setHotwordPackVersionOptions] = useState<Array<{
      id: string;
      label: string;
      status: string;
      packId: string;
      current: boolean;
    }>>([]);

  const [hotwordVersionOptionsLoading, setHotwordVersionOptionsLoading] = useState(true);

  const [runHistory, setRunHistory] = useState<CanvasRunLog[]>([]);

  const [dagsterRunDraft, setDagsterRunDraft] = useState<DagsterRunDraft>(() => ({
      jobName: `${slugifyDagsterName(declaredTaskTypeId || "evidence-dataflow")}_job`,
      partitionKey: contextPartitionKey,
      assetSelection: "platform_session, tenant_list, audio_url_index, authenticated_events, review_queue_asset",
      runTags: `tenant_id=${contextTenantId || "unbound"}, project_id=${contextProjectId || "unbound"}, trigger=schedule, source=auris-flow`,
      runConfigJson:
        '{\n  "ops": {\n    "ingest": {"config": {"stage": "platform_session + authenticated_events"}},\n    "risk_review": {"config": {"human_loop": "login-risk-review"}}\n  }\n}',
      maxRetries: "2",
      concurrencyLimit: "4",
      materializationMode: "外部数据源 + 质量检查 + 复核队列",
      failurePolicy: "失败 2 次后进入人工复核队列",
      reason: "发布前 smoke test"
    }));

  const dragMovedRef = useRef(false);

  return {
    sceneManifest,
    declaredTaskTypeId,
    contextTenantId,
    contextProjectId,
    contextStore,
    contextDate,
    contextPartitionKey,
    initialIntent,
    activeIntentKey,
    setActiveIntentKey,
    activeStageKey,
    setActiveStageKey,
    selectedNodeId,
    setSelectedNodeId,
    drawerTab,
    setDrawerTab,
    executionState,
    setExecutionState,
    mappingSuggestionsByIntent,
    setMappingSuggestionsByIntent,
    selectedMappingId,
    setSelectedMappingId,
    mappingConfidenceThreshold,
    setMappingConfidenceThreshold,
    nodeLibraryOpen,
    setNodeLibraryOpen,
    addedNodes,
    setAddedNodes,
    nodePositions,
    setNodePositions,
    dragState,
    setDragState,
    selectedTemplateKey,
    setSelectedTemplateKey,
    nodeDraft,
    setNodeDraft,
    scheduleMode,
    setScheduleMode,
    scheduleConfigs,
    setScheduleConfigs,
    backfillConfirmed,
    setBackfillConfirmed,
    draftState,
    setDraftState,
    savedTaskVersionId,
    setSavedTaskVersionId,
    savedTaskVersionIdRef,
    rememberTaskVersionId,
    recoveredTaskVersion,
    setRecoveredTaskVersion,
    taskReleaseGate,
    setTaskReleaseGate,
    selectedTaskTypeKey,
    setSelectedTaskTypeKey,
    selectedCanvasVariantKey,
    setSelectedCanvasVariantKey,
    experimentMode,
    setExperimentMode,
    selectedExperimentMetricKey,
    setSelectedExperimentMetricKey,
    metricDraftState,
    setMetricDraftState,
    controlledExperiment,
    setControlledExperiment,
    experimentTaskVersions,
    setExperimentTaskVersions,
    experimentLoading,
    setExperimentLoading,
    experimentActionPending,
    setExperimentActionPending,
    experimentSubjectKey,
    setExperimentSubjectKey,
    experimentConfigDraft,
    setExperimentConfigDraft,
    canvasLevel,
    setCanvasLevel,
    canvasNotice,
    setCanvasNotice,
    canvasAction,
    setCanvasAction,
    asrExecutionMode,
    setAsrExecutionMode,
    asrHotwordVersionId,
    setAsrHotwordVersionId,
    hotwordPackVersionOptions,
    setHotwordPackVersionOptions,
    hotwordVersionOptionsLoading,
    setHotwordVersionOptionsLoading,
    runHistory,
    setRunHistory,
    dagsterRunDraft,
    setDagsterRunDraft,
    dragMovedRef
  };
}

export type CanvasState = ReturnType<typeof useCanvasState>;
