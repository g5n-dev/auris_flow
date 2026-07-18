import type { EvaluationState } from "./useEvaluationState";
import type { EvaluationContextActions } from "./buildEvaluationContextActions";
import type { HotwordPackVersionView } from "../../../shared/runtime/hotwordVersionViews";
import { getBackendRun, getTaskVersion } from "../../../api/client";
import { backendRunStatusLabel } from "../../../shared/runtime/backendRunStatus";

export function buildHotwordPublishPollingAction(
  hotwordPollGenerationRef: EvaluationState["hotwordPollGenerationRef"],
  hotwordPublishRetryRunRef: EvaluationState["hotwordPublishRetryRunRef"],
  pushRunRecord: EvaluationContextActions["pushRunRecord"],
  refreshHotwordCandidateVersion: (versionId: string) => Promise<HotwordPackVersionView>,
  setEvaluationNotice: EvaluationState["setEvaluationNotice"],
  setHotwordPublishRecovery: EvaluationState["setHotwordPublishRecovery"],
  setHotwordPublished: EvaluationState["setHotwordPublished"],
  shortTrace: EvaluationContextActions["shortTrace"],
  waitForHotwordPoll: () => Promise<void>
) {
  return async function pollHotwordPublishRun(
    versionId: string,
    runId: string,
    generation: number,
    initialTrace?: string
  ): Promise<void> {
    const maxAttempts = 10;
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      if (hotwordPollGenerationRef.current !== generation) return;
      const response = await getBackendRun(runId);
      const raw = response.data.raw;
      const status = typeof raw.status === "string" ? raw.status.toLowerCase() : response.data.status.toLowerCase();
      const trace = response.meta?.trace_id ?? response.data.trace_id ?? initialTrace;
      if (["failed", "error", "dead_letter", "canceled", "cancelled"].includes(status)) {
        hotwordPublishRetryRunRef.current = runId;
        setHotwordPublishRecovery({
          status: "failed",
          runId,
          traceId: trace,
          detail: `${backendRunStatusLabel(status)} · 可重试 Run`
        });
        setEvaluationNotice({
          status: "error",
          title: "词包发布运行失败",
          detail: `${runId} · ${backendRunStatusLabel(status)} · Trace ${trace ?? "no-trace"}`
        });
        pushRunRecord("ASR 热词词包发布", `${runId} / ${status} / ${shortTrace(trace)}`, "待确认");
        return;
      }
      if (["success", "succeeded", "complete", "completed"].includes(status)) {
        const liveVersion = await refreshHotwordCandidateVersion(versionId);
        if (liveVersion.status !== "published") {
          setEvaluationNotice({
            status: "error",
            title: "词包发布回执异常",
            detail: `${runId} / ${versionId} / ${liveVersion.status}`
          });
          return;
        }
        const nestedPublish = raw.hotword_publish && typeof raw.hotword_publish === "object" && !Array.isArray(raw.hotword_publish)
          ? raw.hotword_publish as Record<string, unknown>
          : null;
        const nestedTask = raw.task_version;
        const taskVersionId = typeof raw.task_version_id === "string"
          ? raw.task_version_id
          : typeof nestedPublish?.task_version_id === "string"
            ? nestedPublish.task_version_id
            : typeof raw.task_version_draft_id === "string"
              ? raw.task_version_draft_id
              : nestedTask && typeof nestedTask === "object" && typeof (nestedTask as Record<string, unknown>).id === "string"
                ? String((nestedTask as Record<string, unknown>).id)
                : liveVersion.taskVersionId;
        if (!taskVersionId) {
          setHotwordPublishRecovery({ status: "failed", runId, traceId: trace, detail: "缺少 task_version_id" });
          setEvaluationNotice({ status: "error", title: "词包发布回执不完整", detail: `${runId} 缺少 TaskVersion ID。` });
          return;
        }
        const taskVersionResponse = await getTaskVersion(taskVersionId);
        const taskVersionStatus = String(taskVersionResponse.data.status ?? "unknown");
        hotwordPublishRetryRunRef.current = null;
        setHotwordPublished(true);
        setHotwordPublishRecovery({
          status: "success",
          runId,
          traceId: trace,
          taskVersionId,
          detail: `${versionId} / ${taskVersionId} / ${taskVersionStatus}`
        });
        setEvaluationNotice({
          status: "success",
          title: "词包已人工发布",
          detail: `${taskVersionId} · ${taskVersionStatus} · ${versionId} · Run ${runId} · Trace ${trace ?? "no-trace"}`
        });
        pushRunRecord("ASR 热词词包发布", `${taskVersionId} / ${runId} / ${shortTrace(trace)}`);
        return;
      }
      setEvaluationNotice({
        status: "pending",
        title: "词包发布运行已创建",
        detail: `${runId} · ${backendRunStatusLabel(status)} · 轮询 ${attempt}/${maxAttempts} · Trace ${trace ?? "no-trace"}`
      });
      setHotwordPublishRecovery({
        status: "pending",
        runId,
        traceId: trace,
        detail: `${backendRunStatusLabel(status)} ${attempt}/${maxAttempts}`
      });
      if (attempt < maxAttempts) await waitForHotwordPoll();
    }
    if (hotwordPollGenerationRef.current === generation) {
      setEvaluationNotice({
        status: "error",
        title: "词包发布等待超时",
        detail: `${runId} · 轮询超时`
      });
      setHotwordPublishRecovery({ status: "pending", runId, traceId: initialTrace, detail: "等待恢复" });
    }
  };
}
