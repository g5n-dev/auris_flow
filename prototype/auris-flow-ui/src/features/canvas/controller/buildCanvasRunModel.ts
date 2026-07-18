import type { CanvasModuleProps } from "../types";
import type { CanvasState } from "./useCanvasState";
import type { CanvasPrimitiveActions } from "./buildCanvasPrimitiveActions";
import type { CanvasRecoveryModel } from "./useCanvasRecovery";
import type { CanvasSectionModel } from "./buildCanvasSectionModel";
import type { CanvasScheduleModel } from "./useCanvasScheduleModel";
import type { CanvasExecutionPlan } from "./buildCanvasExecutionPlan";
import type { CanvasRuntimeModel } from "./useCanvasRuntimeModel";
import type { CanvasNodeCollections } from "./buildCanvasNodeCollections";
import type { CanvasNodeContextModel } from "./buildCanvasNodeContextModel";
import type { CanvasNodeInteractions } from "./buildCanvasNodeInteractions";
import type { CanvasTaskDagModel } from "./buildCanvasTaskDagModel";
import type { CanvasDraftModelActions } from "./buildCanvasDraftModelActions";
import type { CanvasExecutionActions } from "./buildCanvasExecutionActions";
import type { CanvasExperimentActions } from "./buildCanvasExperimentActions";
import { backendReleaseRequester, refreshBackendRunReceipt } from "../../../api/backendRuns";
import { runTaskVersionOnce } from "../../../api/client";
import { backendRunFailed, backendRunStatusLabel, backendRunSubmitted, backendRunSucceeded, normalizeBackendRunStatus, operationStatusFromBackendRun } from "../../../shared/runtime/backendRunStatus";
import { isRecordValue } from "../../../shared/runtime/records";

export function buildCanvasRunModel(scope: CanvasModuleProps & CanvasState & CanvasPrimitiveActions & CanvasRecoveryModel & CanvasSectionModel & CanvasScheduleModel & CanvasExecutionPlan & CanvasRuntimeModel & CanvasNodeCollections & CanvasNodeContextModel & CanvasNodeInteractions & CanvasTaskDagModel & CanvasDraftModelActions & CanvasExecutionActions & CanvasExperimentActions) {
  const { activePartitionKey, activeRunKey, activeTab, controlledExperiment, currentUser, dagsterRunDraft, draftState, experimentSubjectKey, markTaskDraftDirty, persistTaskDraft, pushRunHistory, reloadControlledExperiment, resetActiveMappings, savedTaskVersionId, savedTaskVersionIdRef, selectedCanvasVariant, setCanvasAction, setCanvasNotice, setDraftState, setDrawerTab, setExecutionState, shortTrace, taskReleaseGate, updateExecutionState } = scope;
  const runTaskOnce = async () => {
      const runningExperiment = activeTab === "experiments" && controlledExperiment?.status === "running" ? controlledExperiment : null;
      if (runningExperiment && !experimentSubjectKey.trim()) {
        setCanvasNotice({
          status: "error",
          title: "缺少实验分流单元",
          detail: `请输入一个 ${runningExperiment.allocation_unit} ID。分流不能使用画布版本或门店分区代替。`
        });
        return;
      }
      setCanvasAction("run");
      updateExecutionState("queued");
      setCanvasNotice({
        status: "pending",
        title: runningExperiment
          ? "正在分流并创建实验运行"
          : draftState === "未保存"
            ? "正在保存草稿并创建运行"
            : "正在创建运行请求",
        detail: runningExperiment
          ? `${runningExperiment.experiment_id} 将按冻结设计为当前 ${runningExperiment.allocation_unit} 选择 TaskVersion。`
          : `${dagsterRunDraft.jobName} 将通过 BFF 创建 task_run，幂等键 ${activeRunKey}。`
      });
      try {
        let experimentRunKey: string | null = null;
        if (runningExperiment) {
          if (!globalThis.crypto?.subtle) {
            throw new Error("当前浏览器不支持 Web Crypto，无法生成隐私安全的实验运行键");
          }
          const subjectDigest = await globalThis.crypto.subtle.digest(
            "SHA-256",
            new TextEncoder().encode(experimentSubjectKey.trim())
          );
          const subjectSha256 = Array.from(
            new Uint8Array(subjectDigest),
            (byte) => byte.toString(16).padStart(2, "0")
          ).join("");
          experimentRunKey = `${activeRunKey}:experiment:${runningExperiment.experiment_id}:${subjectSha256.slice(0, 24)}`;
        }
        let taskVersionId = runningExperiment?.control_task_version_id
          ?? savedTaskVersionIdRef.current
          ?? savedTaskVersionId;
        if (!runningExperiment && (!taskVersionId || draftState === "未保存")) {
          const draftReceipt = await persistTaskDraft();
          taskVersionId = draftReceipt.data.id;
          setDraftState("已保存");
          pushRunHistory(`TaskVersionDraft · ${taskVersionId}`, `运行前保存 / ${shortTrace(draftReceipt.data.trace_id)}`);
        }
        if (!taskVersionId) throw new Error("缺少可运行的 TaskVersion");
        const receipt = await runTaskVersionOnce({
          task_version_id: taskVersionId,
          trigger_type: "manual",
          execution_mode: runningExperiment ? "experiment" : "diagnostic",
          ...(runningExperiment
            ? {
                experiment_id: runningExperiment.experiment_id,
                experiment_subject_key: experimentSubjectKey.trim()
              }
            : {}),
          partition_key: dagsterRunDraft.partitionKey || activePartitionKey,
          run_key: experimentRunKey ?? activeRunKey,
          source: "canvas_module",
          ...(!runningExperiment ? {
            job_name: dagsterRunDraft.jobName,
            asset_selection: dagsterRunDraft.assetSelection,
            canvas_variant: selectedCanvasVariant.key
          } : {})
        });
        const runState = await refreshBackendRunReceipt(receipt.data);
        const runStatus = operationStatusFromBackendRun(runState.status);
        const rawRunDetail = runState.raw.run_detail;
        const runDetail = rawRunDetail && typeof rawRunDetail === "object"
          ? rawRunDetail as Record<string, unknown>
          : runState.raw;
        const assignedArm = typeof runDetail.experiment_arm === "string" ? runDetail.experiment_arm : null;
        const assignedTaskVersion = typeof runDetail.task_version_id === "string" ? runDetail.task_version_id : taskVersionId;
        if (runningExperiment) await reloadControlledExperiment(runningExperiment.experiment_id);
        setCanvasAction(null);
        updateExecutionState(backendRunSucceeded(runState.status) ? "success" : "queued");
        setCanvasNotice({
          status: runStatus,
          title: backendRunSucceeded(runState.status)
            ? "运行已完成"
            : backendRunSubmitted(runState.status)
              ? "运行已提交，等待外部完成"
              : backendRunFailed(runState.status)
                ? "运行请求已创建但执行异常"
                : "运行请求已创建",
          detail: runningExperiment
            ? `${runState.id} ${backendRunStatusLabel(runState.status)}；arm=${assignedArm ?? "待回执"}，TaskVersion=${assignedTaskVersion}，实验模式禁止外部回写；trace：${shortTrace(runState.trace_id)}。`
            : `${runState.id} ${backendRunStatusLabel(runState.status)}；诊断模式不执行外部回写；trace：${shortTrace(runState.trace_id)}。`
        });
        pushRunHistory(
          `TaskRun · ${runState.id}`,
          `${backendRunStatusLabel(runState.status)}${assignedArm ? ` / arm=${assignedArm}` : ""} / ${shortTrace(runState.trace_id)}`
        );
      } catch (error) {
        setCanvasAction(null);
        updateExecutionState("idle");
        setCanvasNotice({
          status: "error",
          title: "运行请求创建失败",
          detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。"
        });
        pushRunHistory("运行请求 · 运行一次", "创建失败");
      }
    };

  const discardTaskChanges = () => {
      resetActiveMappings();
      setExecutionState("idle");
      markTaskDraftDirty();
      setDrawerTab("overview");
        setCanvasNotice({
          status: "idle",
          title: "已放弃当前草稿改动",
          detail: "映射建议、运行状态和未发布变更已恢复到当前演示基线。"
        });
    };

  const releaseHistoryDescription =
      draftState === "已发布"
        ? "已发布版本 / 可复制为新草稿继续迭代"
        : draftState === "已保存"
          ? "已保存草稿 / 等待发布门禁放行"
          : "未保存草稿 / 输入输出、实验和调度仍可编辑";

  const taskReleaseNeedsOtherApprover = Boolean(
      taskReleaseGate &&
      normalizeBackendRunStatus(taskReleaseGate.status) === "blocked" &&
      backendReleaseRequester(taskReleaseGate) === currentUser.userId
    );

  const taskPublishLabel = taskReleaseGate
      ? normalizeBackendRunStatus(taskReleaseGate.status) === "blocked"
        ? taskReleaseNeedsOtherApprover ? "刷新发布状态" : "审批发布"
        : backendRunSucceeded(taskReleaseGate.status)
          ? "已发布"
          : ["pending", "running", "submitted", "dispatched"].includes(normalizeBackendRunStatus(taskReleaseGate.status))
            ? "刷新发布状态"
            : "重新发布"
      : "发布版本";

  const taskReleaseMaterialization = taskReleaseGate && isRecordValue(taskReleaseGate.raw.release_materialization)
      ? taskReleaseGate.raw.release_materialization
      : null;

  const taskProductionReleaseHead = taskReleaseMaterialization
      && isRecordValue(taskReleaseMaterialization.task_version_release_head)
      ? taskReleaseMaterialization.task_version_release_head
      : null;

  return {
    runTaskOnce,
    discardTaskChanges,
    releaseHistoryDescription,
    taskReleaseNeedsOtherApprover,
    taskPublishLabel,
    taskReleaseMaterialization,
    taskProductionReleaseHead
  };
}

export type CanvasRunModel = ReturnType<typeof buildCanvasRunModel>;
