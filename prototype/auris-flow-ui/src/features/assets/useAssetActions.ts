import {
  createExportRun,
  createPlatformMutation,
  getBackendRun,
  loadModuleProjection
} from "../../api/client";
import {
  backendRunFailed,
  backendRunStatusLabel,
  backendRunSucceeded,
  operationStatusFromBackendRun
} from "../../shared/runtime/backendRunStatus";
import { LABEL_DEMO_MODE } from "../../shared/runtime/demoMode";
import type { ProjectSceneLock } from "../../shared/runtime/projectSceneLock";
import type { FailedAssetCheckSelection } from "./authoritativeAssetChecks";
import type {
  AssetActionState,
  AssetBackfillDraft,
  AssetCatalogRow,
  HotwordBackfillBinding,
  HotwordBackfillRecovery
} from "./types";

type UseAssetActionsOptions = AssetActionState & {
  assetRows: AssetCatalogRow[];
  backfillReady: boolean;
  backfillBlockedReason: string;
  selectedAsset: AssetCatalogRow;
  currentDraft: AssetBackfillDraft | null;
  hotwordBackfillRecovery: HotwordBackfillRecovery;
  qualityRetryBlockedReason: string;
  qualityRetryReady: boolean;
  qualityRetrySelection: FailedAssetCheckSelection | null;
  readScopeKey: string;
  sceneProfileBlockedReason: string;
  sceneProfileLock: ProjectSceneLock | null;
  setSelectedAssetKey: (assetKey: string) => void;
};

export function useAssetActions({
  assetAction,
  assetRows,
  backfillReady,
  backfillBlockedReason,
  currentDraft,
  hotwordBackfillRecovery,
  qualityRetryBlockedReason,
  qualityRetryReady,
  qualityRetrySelection,
  readScopeKey,
  sceneProfileBlockedReason,
  sceneProfileLock,
  selectedAsset,
  setAssetAction,
  setAssetNotice,
  setBackfillDraft,
  setSelectedAssetKey
}: UseAssetActionsOptions) {
  const createBackfillDraft = (
    assetKey = selectedAsset.assetKey,
    reason = "资产详情建议生成",
    context: { rootTraceId?: string; hotwordBinding?: HotwordBackfillBinding } = {}
  ) => {
    const targetAsset = assetRows.find((asset) => asset.assetKey === assetKey) ?? selectedAsset;
    setSelectedAssetKey(targetAsset.assetKey);
    setBackfillDraft({
      scopeKey: readScopeKey,
      assetKey: targetAsset.assetKey,
      assetName: targetAsset.name,
      draftId: LABEL_DEMO_MODE
        ? `BF-${targetAsset.materialization.split("-").slice(-2).join("-")}`
        : `UI-DRAFT-${crypto.randomUUID()}`,
      reason,
      status: "草稿",
      rootTraceId: context.rootTraceId,
      hotwordBinding: context.hotwordBinding
    });
    setAssetNotice({
      status: "success",
      title: "UI 回填草稿已生成",
      detail: `${targetAsset.name} 已生成本地 UI 草稿；这不是服务端回执，提交前不会影响已确认资产。`
    });
  };

  const submitBackfillDraft = async () => {
    if (!currentDraft || assetAction === "backfill") return;
    const targetAsset = assetRows.find((asset) => asset.assetKey === currentDraft.assetKey) ?? selectedAsset;
    if (!sceneProfileLock || !backfillReady || targetAsset.partition === "BFF 未提供") {
      setAssetNotice({
        status: "error",
        title: "受控回填提交已阻断",
        detail: `${backfillBlockedReason || "项目写入上下文未就绪"}；UI 草稿保留，未发送回填请求。`
      });
      return;
    }
    if (currentDraft.reason.includes("ASR 热词") && !currentDraft.hotwordBinding) {
      setAssetNotice({
        status: "error",
        title: "ASR 热词受控回填已阻断",
        detail: hotwordBackfillRecovery.reason
      });
      return;
    }
    setAssetAction("backfill");
    setAssetNotice({
      status: "pending",
      title: "受控回填提交中",
      detail: `${currentDraft.draftId} · ${targetAsset.partition}；不覆盖历史资产。`
    });
    try {
      const receipt = await createPlatformMutation("assets", {
        action: "提交受控资产回填",
        ...sceneProfileLock,
        asset_key: currentDraft.assetKey,
        partition_key: targetAsset.partition,
        reason: currentDraft.reason,
        impact_scope: {
          scope: "current_project",
          ui_draft_id: currentDraft.draftId,
          ...((currentDraft.hotwordBinding?.sourceMaterializationId || targetAsset.materialization !== "尚无生成记录") ? {
            materialization_id: currentDraft.hotwordBinding?.sourceMaterializationId ?? targetAsset.materialization
          } : {}),
          downstream_assets: targetAsset.downstream,
          root_trace_id: currentDraft.rootTraceId,
          overwrite_history: false,
          ...(currentDraft.hotwordBinding ? {
            hotword_pack_version_id: currentDraft.hotwordBinding.hotwordPackVersionId,
            eval_run_id: currentDraft.hotwordBinding.evalRunId,
            task_version_id: currentDraft.hotwordBinding.taskVersionId,
            source_asset: currentDraft.hotwordBinding.sourceAsset,
            source_materialization_id: currentDraft.hotwordBinding.sourceMaterializationId
          } : {})
        }
      });
      setBackfillDraft({ ...currentDraft, status: "待审批" });
      if (!currentDraft.hotwordBinding) {
        setAssetNotice({
          status: operationStatusFromBackendRun(receipt.data.status),
          title: "受控回填运行已创建",
          detail: `${receipt.data.id} · ${backendRunStatusLabel(receipt.data.status)} · Trace ${receipt.meta?.trace_id ?? receipt.data.trace_id ?? "no-trace"}；历史只读。`
        });
        return;
      }
      const runId = receipt.data.id;
      const initialTrace = receipt.meta?.trace_id ?? receipt.data.trace_id;
      for (let attempt = 1; attempt <= 10; attempt += 1) {
        const run = await getBackendRun(runId);
        const status = run.data.status;
        const trace = run.meta?.trace_id ?? run.data.trace_id ?? initialTrace;
        if (backendRunFailed(status)) {
          setAssetNotice({
            status: "error",
            title: "ASR 热词受控回填失败",
            detail: `${runId} · ${backendRunStatusLabel(status)} · Trace ${trace ?? "no-trace"}`
          });
          return;
        }
        if (backendRunSucceeded(status)) {
          await loadModuleProjection("assets", { force: true });
          setAssetNotice({
            status: "success",
            title: "受控回填已完成",
            detail: `${runId} · ${currentDraft.hotwordBinding.hotwordPackVersionId} · root_trace_id ${currentDraft.hotwordBinding.rootTraceId} · 资产投影已刷新`
          });
          return;
        }
        setAssetNotice({
          status: "pending",
          title: "受控回填运行已创建",
          detail: `${runId} · ${backendRunStatusLabel(status)} · 轮询 ${attempt}/10 · Trace ${trace ?? "no-trace"}`
        });
        if (attempt < 10) await new Promise<void>((resolve) => window.setTimeout(resolve, 500));
      }
      setAssetNotice({
        status: "error",
        title: "受控回填等待超时",
        detail: `${runId} 在有限轮询窗口内未完成，历史资产保持只读。`
      });
    } catch (error) {
      setAssetNotice({
        status: "error",
        title: "受控回填提交失败",
        detail: `${error instanceof Error ? error.message : "BFF 请求失败"}；历史未改变。`
      });
    } finally {
      setAssetAction(null);
    }
  };

  const rerunAssetQuality = async () => {
    if (assetAction === "quality") return;
    if (!sceneProfileLock || !qualityRetryReady || !qualityRetrySelection) {
      setAssetNotice({
        status: "error",
        title: "质量校验重跑已阻断",
        detail: `${qualityRetryBlockedReason || sceneProfileBlockedReason || "权威失败 checks 未就绪"}；未创建运行。`
      });
      return;
    }
    setAssetAction("quality");
    setAssetNotice({
      status: "pending",
      title: "质量校验重跑中",
      detail: `${selectedAsset.name} 正在创建 AssetCheck 重跑任务。`
    });
    try {
      const receipt = await createPlatformMutation("assets", {
        action: "重跑质量校验",
        ...sceneProfileLock,
        target: { asset_key: selectedAsset.assetKey },
        reason: `${selectedAsset.name} 质量门禁重跑`,
        failed_check_ids: qualityRetrySelection.failedCheckIds,
        failed_partitions: qualityRetrySelection.failedPartitions
      });
      setAssetAction(null);
      setAssetNotice({
        status: operationStatusFromBackendRun(receipt.data.status),
        title: "质量校验运行已创建",
        detail: `${receipt.data.id} 当前${backendRunStatusLabel(receipt.data.status)}，trace：${
          (receipt.meta?.trace_id ?? receipt.data.trace_id)?.slice(0, 12) ?? "no-trace"
        }。`
      });
    } catch (error) {
      setAssetAction(null);
      setAssetNotice({
        status: "error",
        title: "质量校验重跑失败",
        detail: `未创建运行：${error instanceof Error ? error.message : "unknown error"}。`
      });
    }
  };

  const exportAssetPackage = async () => {
    if (assetAction === "export") return;
    if (!sceneProfileLock) {
      setAssetNotice({
        status: "error",
        title: "资产包导出已阻断",
        detail: `${sceneProfileBlockedReason}；未创建导出运行。`
      });
      return;
    }
    setAssetAction("export");
    setAssetNotice({
      status: "pending",
      title: "正在生成资产包",
      detail: `${selectedAsset.name} 正在创建导出运行。`
    });
    try {
      const receipt = await createExportRun({
        target: "data_asset",
        object_id: selectedAsset.assetKey,
        source: "ui_asset_package",
        ...sceneProfileLock
      });
      setAssetAction(null);
      setAssetNotice({
        status: operationStatusFromBackendRun(receipt.data.status),
        title: "资产包导出已创建",
        detail: `${receipt.data.id} 当前${backendRunStatusLabel(receipt.data.status)}，trace：${
          (receipt.meta?.trace_id ?? receipt.data.trace_id)?.slice(0, 12) ?? "no-trace"
        }。`
      });
    } catch (error) {
      setAssetAction(null);
      setAssetNotice({
        status: "error",
        title: "资产包导出失败",
        detail: `未创建运行：${error instanceof Error ? error.message : "unknown error"}。`
      });
    }
  };

  return { createBackfillDraft, exportAssetPackage, rerunAssetQuality, submitBackfillDraft };
}
