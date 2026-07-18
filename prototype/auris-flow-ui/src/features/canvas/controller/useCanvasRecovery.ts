import type { CanvasModuleProps } from "../types";
import type { CanvasState } from "./useCanvasState";
import type { CanvasPrimitiveActions } from "./buildCanvasPrimitiveActions";
import { getControlledExperiment, listControlledExperiments, listHotwordPacks, listHotwordPackVersions, listTaskVersions } from "../../../api/client";
import type { ControlledExperiment, ControlledExperimentVariantDimension } from "../../../api/client";
import { hotwordVersionView } from "../../../shared/runtime/hotwordVersionViews";
import type { HotwordPackVersionView } from "../../../shared/runtime/hotwordVersionViews";
import { canvasIntents, experimentMetricSuggestions, taskCanvasVariants, taskFlowStages, taskTypeBlueprints } from "../catalog";
import { canvasNodeTemplates, slugifyDagsterName } from "../nodeTemplates";
import type { CanvasIntentKey } from "../types";
import { useEffect, useMemo } from "react";
import {
  experimentDimensionFields,
  taskVersionDimensionDocument,
  taskVersionId
} from "./experimentVersionTools";

export function useCanvasRecovery(scope: CanvasModuleProps & CanvasState & CanvasPrimitiveActions) {
  const { activeIntentKey, activeStageKey, activeTab, asrHotwordVersionId, contextProjectId, contextTenantId, controlledExperiment, declaredTaskTypeId, demoMode, experimentConfigDraft, experimentTaskVersions, hotwordPackVersionOptions, sceneManifest, selectedCanvasVariantKey, selectedExperimentMetricKey, selectedTaskTypeKey, selectedTemplateKey, setAsrHotwordVersionId, setCanvasNotice, setControlledExperiment, setExperimentConfigDraft, setExperimentLoading, setExperimentMode, setExperimentTaskVersions, setHotwordPackVersionOptions, setHotwordVersionOptionsLoading, setMetricDraftState, setSelectedExperimentMetricKey, setSelectedTaskTypeKey } = scope;
  const activeIntent = canvasIntents.find((intent) => intent.key === activeIntentKey) ?? canvasIntents[0];
  const selectedTemplate = canvasNodeTemplates.find((template) => template.key === selectedTemplateKey) ?? canvasNodeTemplates[0];
  const availableTaskTypes = useMemo(() => {
      const refs = sceneManifest?.task_type_refs ?? [];
      if (!refs.length) return demoMode ? taskTypeBlueprints : [];
      return refs.map((reference) => taskTypeBlueprints.find((task) => task.key === reference) ?? {
        key: reference,
        name: reference,
        intentKey: "review" as CanvasIntentKey,
        status: "SceneProfile 已声明",
        owner: sceneManifest?.roles.map((role) => role.display_name).join(" / ") || "场景负责人",
        sla: "由任务类型资源定义",
        reusableCanvases: "由任务类型版本与执行映射定义",
        description: `${sceneManifest?.display_name ?? "当前场景"} 的版本化任务类型强引用。`,
        defaultCanvas: slugifyDagsterName(reference)
      });
    }, [demoMode, sceneManifest]);

  const selectedTaskType = availableTaskTypes.find((task) => task.key === selectedTaskTypeKey)
      ?? availableTaskTypes[0]
      ?? {
        key: "unbound-task-type",
        name: "未绑定任务类型",
        intentKey: "review" as CanvasIntentKey,
        status: "阻断",
        owner: "待配置",
        sla: "—",
        reusableCanvases: "—",
        description: "当前项目必须先绑定包含 task_type_refs 的已发布 SceneProfile。",
        defaultCanvas: "unbound_task_type"
      };

  const availableExperimentMetrics = useMemo(() => {
      const declaredMetrics = sceneManifest?.metrics ?? [];
      if (!declaredMetrics.length) return demoMode ? experimentMetricSuggestions : [];
      return declaredMetrics.map((metric) => {
        const catalogMetric = experimentMetricSuggestions.find((item) => item.key === metric.metric_key);
        if (catalogMetric) return catalogMetric;
        const direction = /risk|error|failure|latency|backlog|conflict|crosstalk/i.test(metric.metric_key)
          ? "下降"
          : "提升";
        return {
          key: metric.metric_key,
          name: metric.display_name,
          category: "SceneProfile 指标",
          layer: "场景成效",
          source: metric.evidence_refs.join(" + "),
          formula: metric.calculator_ref,
          window: "按实验分流单元与场景分区聚合",
          owner: "场景治理",
          confidence: 100,
          status: "已由 SceneProfile 声明",
          reason: `${metric.display_name} 已锁定在当前 SceneProfile，可用于实验${direction}目标与可复算指标快照。`,
          events: metric.evidence_refs,
          guardrails: ["样本量满足后才允许决策", "守护指标不得退化", "指标证据必须带 trace_id"],
          sql: metric.calculator_ref,
          action: "通过后端指标快照与人工决策晋级候选版本",
          risk: "切换 SceneProfile 版本后必须新建实验，禁止沿用旧口径"
        };
      });
    }, [demoMode, sceneManifest]);
  const selectedCanvasVariant = taskCanvasVariants.find((variant) => variant.key === selectedCanvasVariantKey) ?? taskCanvasVariants[0];
  const activeFlowStage = taskFlowStages.find((stage) => stage.key === activeStageKey) ?? taskFlowStages[0];
  const selectedExperimentMetric = availableExperimentMetrics.find((metric) => metric.key === selectedExperimentMetricKey)
      ?? availableExperimentMetrics[0]
      ?? experimentMetricSuggestions[0];

  const defaultExperimentControl = experimentTaskVersions.find((version) => version.status === "published");

  const defaultExperimentCandidate = experimentTaskVersions.find((version) =>
    ["experiment_ready", "validated"].includes(String(version.status ?? ""))
  );

  const experimentControlTaskVersion = experimentTaskVersions.find((version) =>
    taskVersionId(version) === (controlledExperiment?.control_task_version_id || experimentConfigDraft.controlTaskVersionId)
  ) ?? defaultExperimentControl;

  const experimentCandidateTaskVersion = experimentTaskVersions.find((version) =>
    taskVersionId(version) === (controlledExperiment?.candidate_task_version_id || experimentConfigDraft.candidateTaskVersionId)
  ) ?? defaultExperimentCandidate;

  const experimentTreatmentPreview = useMemo(() => {
    const declaredDimension = controlledExperiment?.variant_dimension ?? experimentConfigDraft.variantDimension;
    const changedDimensions = experimentControlTaskVersion && experimentCandidateTaskVersion
      ? (Object.keys(experimentDimensionFields) as Array<Exclude<ControlledExperimentVariantDimension, "bundle">>).filter((dimension) =>
          JSON.stringify(taskVersionDimensionDocument(experimentControlTaskVersion, experimentDimensionFields[dimension]))
          !== JSON.stringify(taskVersionDimensionDocument(experimentCandidateTaskVersion, experimentDimensionFields[dimension]))
        )
      : [];
    const actualDimensions = controlledExperiment?.actual_changed_dimensions ?? changedDimensions;
    const compatible = controlledExperiment
      ? true
      : declaredDimension === "bundle"
        ? actualDimensions.length > 0
        : actualDimensions.length === 1 && actualDimensions[0] === declaredDimension;
    return {
      declaredDimension,
      changedDimensions: actualDimensions,
      compatible,
      diffSha256: controlledExperiment?.variant_diff_sha256 ?? null,
      status: !experimentControlTaskVersion || !experimentCandidateTaskVersion
        ? "缺少版本"
        : !actualDimensions.length
          ? "未检测到执行差异"
          : compatible
            ? "变量隔离通过"
            : "存在混杂变量"
    };
  }, [controlledExperiment, experimentConfigDraft.variantDimension, experimentControlTaskVersion, experimentCandidateTaskVersion]);

  const selectedHotwordPackVersion = hotwordPackVersionOptions.find((item) => item.id === asrHotwordVersionId);

  useEffect(() => {
      if (!declaredTaskTypeId) return;
      setSelectedTaskTypeKey((current) => sceneManifest?.task_type_refs.includes(current) ? current : declaredTaskTypeId);
    }, [declaredTaskTypeId, sceneManifest]);

  useEffect(() => {
      if (!availableExperimentMetrics.length) return;
      setSelectedExperimentMetricKey((current) => availableExperimentMetrics.some((metric) => metric.key === current)
        ? current
        : availableExperimentMetrics[0].key);
    }, [availableExperimentMetrics]);

  useEffect(() => {
    if (!experimentTaskVersions.length) return;
    setExperimentConfigDraft((current) => {
      const controlTaskVersionId = controlledExperiment?.control_task_version_id
        ?? (experimentTaskVersions.some((version) => taskVersionId(version) === current.controlTaskVersionId)
          ? current.controlTaskVersionId
          : taskVersionId(defaultExperimentControl));
      const candidateTaskVersionId = controlledExperiment?.candidate_task_version_id
        ?? (experimentTaskVersions.some((version) => taskVersionId(version) === current.candidateTaskVersionId)
          ? current.candidateTaskVersionId
          : taskVersionId(defaultExperimentCandidate));
      const variantDimension = controlledExperiment?.variant_dimension ?? current.variantDimension;
      if (
        controlTaskVersionId === current.controlTaskVersionId
        && candidateTaskVersionId === current.candidateTaskVersionId
        && variantDimension === current.variantDimension
      ) return current;
      return { ...current, controlTaskVersionId, candidateTaskVersionId, variantDimension };
    });
  }, [controlledExperiment, defaultExperimentCandidate, defaultExperimentControl, experimentTaskVersions]);

  useEffect(() => {
      if (activeTab !== "experiments" || !contextTenantId || !contextProjectId) return;
      let active = true;
      setExperimentLoading(true);
      void Promise.all([listControlledExperiments(), listTaskVersions()])
        .then(async ([experimentResponse, taskVersionResponse]) => {
          const taskVersions = taskVersionResponse.data.items.filter((item) => item.task_type_id === declaredTaskTypeId);
          if (active) setExperimentTaskVersions(taskVersions);
          const matching = experimentResponse.data.items.find((item) => item.task_type_id === declaredTaskTypeId)
            ?? experimentResponse.data.items[0];
          if (!matching) return null;
          return (await getControlledExperiment(matching.experiment_id)).data;
        })
        .then((experiment) => {
          if (!active) return;
          setControlledExperiment(experiment);
          if (!experiment) return;
          const modeByStatus: Record<ControlledExperiment["status"], string> = {
            draft: "草稿",
            running: "灰度中",
            paused: "暂停",
            stopped: "暂停",
            decided: "已决策"
          };
          setExperimentMode(modeByStatus[experiment.status]);
          setSelectedExperimentMetricKey(experiment.primary_metric.metric_key);
          setMetricDraftState("发布闸门");
        })
        .catch((error) => {
          if (!active) return;
          setControlledExperiment(null);
          setExperimentTaskVersions([]);
          setCanvasNotice({
            status: "error",
            title: "实验状态读取失败",
            detail: error instanceof Error ? error.message : "无法读取受控实验资源。"
          });
        })
        .finally(() => {
          if (active) setExperimentLoading(false);
        });
      return () => {
        active = false;
      };
    }, [activeTab, contextProjectId, contextTenantId, declaredTaskTypeId]);

  useEffect(() => {
      let active = true;
      setHotwordVersionOptionsLoading(true);
      void listHotwordPacks()
        .then(async (packsResponse) => {
          const packs = packsResponse.data.items.filter((pack) => pack.status !== "archived");
          const optionGroups = await Promise.all(packs.map(async (pack) => {
            const packId = typeof pack.pack_id === "string" ? pack.pack_id : typeof pack.id === "string" ? pack.id : null;
            if (!packId) return [];
            const currentVersionId = typeof pack.current_version_id === "string" ? pack.current_version_id : null;
            const packName = typeof pack.name === "string" ? pack.name : packId;
            const versionsResponse = await listHotwordPackVersions(packId, { limit: 100 });
            return versionsResponse.data.items
              .map((raw) => hotwordVersionView(raw))
              .filter((version): version is HotwordPackVersionView => version !== null)
              .filter((version) => !["deprecated", "rolled_back", "archived"].includes(version.status))
              .map((version) => ({
                id: version.id,
                label: `${packName} ${version.version}`,
                status: version.status,
                packId,
                current: version.id === currentVersionId
              }));
          }));
          if (!active) return;
          const options = optionGroups.flat().sort((left, right) => {
            if (left.current !== right.current) return left.current ? -1 : 1;
            if ((left.status === "published") !== (right.status === "published")) return left.status === "published" ? -1 : 1;
            return right.label.localeCompare(left.label, "zh-CN", { numeric: true });
          });
          setHotwordPackVersionOptions(options);
          const defaultPublished = options.find((option) => option.current && option.status === "published")
            ?? options.find((option) => option.status === "published");
          setAsrHotwordVersionId((current) => {
            const currentOption = options.find((option) => option.id === current);
            return currentOption?.status === "published" ? current : defaultPublished?.id ?? "";
          });
          if (!defaultPublished) {
            setCanvasNotice({
              status: "error",
              title: "ASR 热词版本恢复已阻断",
              detail: "当前项目没有 pack.current_version_id 指向的 published 版本，production 不可保存或运行。"
            });
          }
        })
        .catch((error) => {
          if (!active) return;
          setHotwordPackVersionOptions([]);
          setAsrHotwordVersionId("");
          setCanvasNotice({
            status: "error",
            title: "ASR 热词版本读取失败",
            detail: error instanceof Error ? error.message : "无法读取热词包与版本。"
          });
        })
        .finally(() => {
          if (active) setHotwordVersionOptionsLoading(false);
        });
      return () => {
        active = false;
      };
    }, []);

  return {
    activeIntent,
    selectedTemplate,
    availableTaskTypes,
    selectedTaskType,
    availableExperimentMetrics,
    selectedCanvasVariant,
    activeFlowStage,
    selectedExperimentMetric,
    experimentControlTaskVersion,
    experimentCandidateTaskVersion,
    experimentTreatmentPreview,
    selectedHotwordPackVersion
  };
}

export type CanvasRecoveryModel = ReturnType<typeof useCanvasRecovery>;
