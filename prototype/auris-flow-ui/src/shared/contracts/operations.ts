export type OperationStatus = "idle" | "pending" | "success" | "error";

export type BackendStatus = "checking" | "online" | "degraded" | "offline";

export type OperationNotice = {
  status: OperationStatus;
  title: string;
  detail: string;
};
