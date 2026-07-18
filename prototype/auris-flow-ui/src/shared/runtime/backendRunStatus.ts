import type { OperationStatus } from "../contracts/operations";

export const normalizeBackendRunStatus = (status?: string) => String(status ?? "pending").toLowerCase();

export const backendRunSucceeded = (status?: string) =>
  ["success", "succeeded", "complete", "completed", "materialized"].includes(normalizeBackendRunStatus(status));

export const backendRunSubmitted = (status?: string) =>
  ["submitted", "dispatched"].includes(normalizeBackendRunStatus(status));

export const backendRunFailed = (status?: string) =>
  ["failed", "error", "dead_letter", "canceled", "cancelled"].includes(normalizeBackendRunStatus(status));

export const operationStatusFromBackendRun = (status?: string): OperationStatus => {
  if (backendRunSucceeded(status)) return "success";
  if (backendRunFailed(status)) return "error";
  return "pending";
};

export const backendRunStatusLabel = (status?: string) => {
  const normalized = normalizeBackendRunStatus(status);
  if (["success", "succeeded", "complete", "completed", "materialized"].includes(normalized)) return "已完成";
  if (["submitted", "dispatched"].includes(normalized)) return "已提交，等待外部完成";
  if (normalized === "running") return "运行中";
  if (["queued", "pending"].includes(normalized)) return "等待执行";
  if (normalized === "blocked") return "等待门禁";
  if (normalized === "dead_letter") return "死信待处理";
  if (["failed", "error"].includes(normalized)) return "执行失败";
  if (["canceled", "cancelled"].includes(normalized)) return "已取消";
  return `状态 ${status ?? "pending"}`;
};
