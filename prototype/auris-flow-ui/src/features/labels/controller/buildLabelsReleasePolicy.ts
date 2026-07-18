import type { BackendActionReceipt } from "../../../api/client";
import { normalizeBackendRunStatus } from "../../../shared/runtime/backendRunStatus";
import type { LabelPublishAction, LabelPublishRequestState } from "../types";

export function labelPublishReason(receipt: BackendActionReceipt) {
  const blockedReasons = Array.isArray(receipt.raw.blocked_reasons)
    ? receipt.raw.blocked_reasons
        .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
        .map((item) => `${String(item.code ?? "BLOCKED")}：${String(item.message ?? "发布门禁未通过")}`)
    : [];
  if (blockedReasons.length) return blockedReasons.join("；");
  const reasonCode = typeof receipt.raw.reason_code === "string" ? receipt.raw.reason_code : "RELEASE_BLOCKED";
  const reason = typeof receipt.raw.failure_reason === "string"
    ? receipt.raw.failure_reason
    : typeof receipt.raw.message === "string"
      ? receipt.raw.message
      : "后端发布门禁未放行";
  return `${reasonCode}：${reason}`;
}

export function buildLabelsReleasePolicy(
  labelPublishRequest: LabelPublishRequestState,
  labelEvalSucceeded: boolean
) {
  const labelPublishActionLabels: Record<LabelPublishAction, string> = {
    gate: "提交门禁",
    gray: "灰度发布",
    candidate: "发布候选",
    execute: "执行发布动作"
  };
  const labelPublishPending = labelPublishRequest.status === "pending";
  const labelPublishBlocked = labelPublishRequest.status === "blocked";
  const releaseBackendStatus = normalizeBackendRunStatus(labelPublishRequest.backendStatus ?? "");
  const labelCandidatePublishDisabled = labelPublishPending || labelPublishBlocked || !labelEvalSucceeded;
  const labelGrayPublishDisabled = labelPublishPending
    || labelPublishBlocked
    || !["shadowing", "monitoring"].includes(releaseBackendStatus);
  const labelPromotePublishDisabled = labelPublishPending
    || labelPublishBlocked
    || !["gray-releasing", "monitoring"].includes(releaseBackendStatus);
  const labelReleaseDisabledReason = (action: "candidate" | "gray" | "execute") => {
    if (labelPublishBlocked) return labelPublishRequest.error;
    if (labelPublishPending) return "发布请求处理中，请等待后端回执";
    if (action === "candidate" && !labelEvalSucceeded) return "必须先 GET 读回 success/completed EvalRun，POST 受理态不能创建发布 Bundle";
    if (action === "gray" && labelGrayPublishDisabled) return "仅后端 shadowing/monitoring 状态可人工批准 10% 灰度";
    if (action === "execute" && labelPromotePublishDisabled) return "仅后端 gray-releasing/monitoring 且监控门禁通过后可人工晋级";
    return undefined;
  };
  return {
    labelPublishActionLabels,
    labelPublishPending,
    labelPublishBlocked,
    releaseBackendStatus,
    labelCandidatePublishDisabled,
    labelGrayPublishDisabled,
    labelPromotePublishDisabled,
    labelReleaseDisabledReason
  };
}
