type ApiStatus = "checking" | "online" | "degraded" | "offline";

export function ApiStatusPill({ status }: { status: ApiStatus }) {
  const label =
    status === "online"
      ? "后端已就绪"
      : status === "degraded"
        ? "后端依赖异常"
        : status === "checking"
          ? "后端检查中"
          : "后端未连接";
  return (
    <div className={`api-status-pill ${status}`} title="FastAPI BFF /readyz 就绪状态">
      <span />
      {label}
    </div>
  );
}
