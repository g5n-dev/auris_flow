import type { CanvasActionScope } from "./canvasActionScope";
import type { CanvasDraftModelActions } from "./buildCanvasDraftModelActions";
import { backendReleaseRequester, refreshBackendRunReceipt } from "../../../api/backendRuns";
import { decideBackendRun, publishTaskVersionDraft, type BackendActionReceipt } from "../../../api/client";
import {
  backendRunStatusLabel,
  backendRunSucceeded,
  normalizeBackendRunStatus,
  operationStatusFromBackendRun
} from "../../../shared/runtime/backendRunStatus";
import { isRecordValue } from "../../../shared/runtime/records";

export function buildCanvasReleaseActions(scope: CanvasActionScope & CanvasDraftModelActions) {
  const {
    currentUser,
    draftState,
    experimentMode,
    persistTaskDraft,
    pushRunHistory,
    recoveredTaskVersion,
    rememberTaskVersionId,
    savedTaskVersionId,
    scheduleMode,
    selectedCanvasVariant,
    selectedTaskType,
    setCanvasAction,
    setCanvasNotice,
    setDraftState,
    setDrawerTab,
    setExecutionState,
    setRecoveredTaskVersion,
    setSelectedNodeId,
    setTaskReleaseGate,
    shortTrace,
    taskDraftValidation,
    taskReleaseGate
  } = scope;

  const markTaskVersionPublished = (runState: BackendActionReceipt) => {
    const runDetail = isRecordValue(runState.raw.run_detail)
      ? runState.raw.run_detail
      : runState.raw;
    const releaseMaterialization = isRecordValue(runDetail.release_materialization)
      ? runDetail.release_materialization
      : null;
    const releaseHead = releaseMaterialization && isRecordValue(releaseMaterialization.task_version_release_head)
      ? releaseMaterialization.task_version_release_head
      : null;
    const activeTaskVersionId = typeof releaseHead?.active_task_version_id === "string"
      ? releaseHead.active_task_version_id
      : null;
    const publishedTaskVersionId = activeTaskVersionId
      ?? (typeof runDetail.task_version_id === "string" ? runDetail.task_version_id : null);
    const releaseGeneration = typeof releaseHead?.generation === "number"
      ? releaseHead.generation
      : null;
    if (publishedTaskVersionId) rememberTaskVersionId(publishedTaskVersionId);
    setDraftState("已发布");
    setRecoveredTaskVersion((current) => ({
      ...(current ?? {}),
      ...(publishedTaskVersionId
        ? { id: publishedTaskVersionId, task_version_id: publishedTaskVersionId }
        : {}),
      status: "published",
      publish_run_id: runState.id,
      ...(releaseHead ? { production_release_head: releaseHead } : {})
    }));
    setExecutionState("success");
    setCanvasNotice({
      status: "success",
      title: "任务版本已发布",
      detail: activeTaskVersionId
        ? `${activeTaskVersionId} 已成为生产版本头${releaseGeneration ? `（第 ${releaseGeneration} 代）` : ""}；trace：${shortTrace(runState.trace_id)}。`
        : `${publishedTaskVersionId ?? runState.id} 已完成发布物化；trace：${shortTrace(runState.trace_id)}。`
    });
  };

  const publishTaskVersion = async () => {
    if (taskReleaseGate) {
      const gateStatus = normalizeBackendRunStatus(taskReleaseGate.status);
      if (gateStatus === "blocked") {
        if (backendReleaseRequester(taskReleaseGate) === currentUser.userId) {
          setCanvasAction("publish");
          const runState = await refreshBackendRunReceipt(taskReleaseGate);
          setTaskReleaseGate(runState);
          setCanvasAction(null);
          if (backendRunSucceeded(runState.status)) {
            markTaskVersionPublished(runState);
          } else {
            setCanvasNotice({
              status: operationStatusFromBackendRun(runState.status),
              title: "等待其他管理员审批",
              detail: `${runState.id} 由 ${currentUser.name} 发起，需管理员审批；仅刷新。`
            });
          }
          return;
        }
        setCanvasAction("publish");
        setCanvasNotice({
          status: "pending",
          title: "正在审批发布门禁",
          detail: `${taskReleaseGate.id} 将记录审批人、理由和 trace，再重新进入发布队列。`
        });
        try {
          const decision = await decideBackendRun(
            taskReleaseGate.id,
            "approved",
            `${selectedTaskType.name} 已通过兼容性、资产契约和回滚检查`
          );
          setTaskReleaseGate(decision.data);
          pushRunHistory(`TaskVersionPublish · ${decision.data.id}`, `门禁已放行 / ${shortTrace(decision.data.trace_id)}`);
          await new Promise<void>((resolve) => window.setTimeout(resolve, 520));
          const runState = await refreshBackendRunReceipt(decision.data);
          setTaskReleaseGate(runState);
          if (backendRunSucceeded(runState.status)) {
            markTaskVersionPublished(runState);
          } else {
            setExecutionState("queued");
            setCanvasNotice({
              status: operationStatusFromBackendRun(runState.status),
              title: "发布门禁已放行",
              detail: `${runState.id} 当前${backendRunStatusLabel(runState.status)}；可刷新运行状态，trace：${shortTrace(runState.trace_id)}。`
            });
          }
        } catch (error) {
          setCanvasNotice({
            status: "error",
            title: "发布审批失败",
            detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。"
          });
        } finally {
          setCanvasAction(null);
        }
        return;
      }
      if (["pending", "running", "submitted", "dispatched"].includes(gateStatus)) {
        setCanvasAction("publish");
        const runState = await refreshBackendRunReceipt(taskReleaseGate);
        setTaskReleaseGate(runState);
        setCanvasAction(null);
        if (backendRunSucceeded(runState.status)) {
          markTaskVersionPublished(runState);
        } else {
          setCanvasNotice({
            status: operationStatusFromBackendRun(runState.status),
            title: "发布状态已刷新",
            detail: `${runState.id} 当前${backendRunStatusLabel(runState.status)}；trace：${shortTrace(runState.trace_id)}。`
          });
        }
        return;
      }
      if (backendRunSucceeded(gateStatus)) {
        markTaskVersionPublished(taskReleaseGate);
        return;
      }
      setTaskReleaseGate(null);
    }
    const recoveredStatus = typeof recoveredTaskVersion?.status === "string" ? recoveredTaskVersion.status.toLowerCase() : null;
    const recoveredHotwordVersionId = typeof recoveredTaskVersion?.hotword_pack_version_id === "string"
      ? recoveredTaskVersion.hotword_pack_version_id
      : null;
    const canPublishRecoveredTask = Boolean(
      recoveredTaskVersion &&
      savedTaskVersionId &&
      recoveredStatus === "draft" &&
      recoveredHotwordVersionId
    );
    if (!taskDraftValidation.canPublish && !canPublishRecoveredTask) {
      setCanvasNotice({
        status: "error",
        title: "发布被阻断",
        detail: `${taskDraftValidation.summary}：${taskDraftValidation.blockers.map((item) => item.label).join("、")}。`
      });
      pushRunHistory("TaskVersion · 发布版本", `阻断：${taskDraftValidation.blockers.map((item) => item.label).join(" / ")}`);
      return;
    }
    setCanvasAction("publish");
    setCanvasNotice({
      status: "pending",
      title: "正在创建发布门禁",
      detail: `${selectedTaskType.name} 将先保存草稿，再创建发布 gate；历史运行不会被覆盖。`
    });
    setSelectedNodeId("dagster");
    setDrawerTab("plan");
    try {
      let taskVersionId = savedTaskVersionId;
      if (!taskVersionId || draftState === "未保存") {
        const draftReceipt = await persistTaskDraft();
        taskVersionId = draftReceipt.data.id;
        setDraftState("已保存");
        pushRunHistory(`TaskVersionDraft · ${taskVersionId}`, `发布前保存 / ${shortTrace(draftReceipt.data.trace_id)}`);
      }
      const receipt = await publishTaskVersionDraft(taskVersionId, {
        source: "canvas_module",
        reason: `${selectedTaskType.name} / ${selectedCanvasVariant.name} 发布门禁`,
        gates: taskDraftValidation.blockers.length
          ? taskDraftValidation.blockers.map((item) => item.label)
          : ["compatibility", "asset_contract", "human_approval"],
        schedule_mode: scheduleMode,
        experiment_mode: experimentMode
      });
      setCanvasAction(null);
      setTaskReleaseGate(receipt.data);
      setExecutionState("queued");
      setCanvasNotice({
        status: "success",
        title: "发布门禁已创建",
        detail: `${receipt.data.id} 状态 ${receipt.data.status}，待审批后进入执行队列；trace：${shortTrace(receipt.data.trace_id)}。`
      });
      pushRunHistory(`TaskVersionPublish · ${receipt.data.id}`, `${receipt.data.status} / ${shortTrace(receipt.data.trace_id)}`);
    } catch (error) {
      setCanvasAction(null);
      setCanvasNotice({
        status: "error",
        title: "发布门禁创建失败",
        detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。"
      });
      pushRunHistory("TaskVersion · 发布版本", "创建失败");
    }
  };

  return {
    markTaskVersionPublished,
    publishTaskVersion
  };
}

export type CanvasReleaseActions = ReturnType<typeof buildCanvasReleaseActions>;
