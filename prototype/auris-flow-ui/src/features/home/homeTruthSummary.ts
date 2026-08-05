export type HomeTruthSummaryKey = "running" | "pending" | "failed" | "latest_import";

export type HomeTruthSummaryCard = {
  key: HomeTruthSummaryKey;
  label: string;
  value: string;
  detail: string;
  tone: "blue" | "amber" | "red" | "teal";
  route: "canvas" | "listening" | "assets" | "data";
  source: "bff";
};

type JsonRecord = Record<string, unknown>;

const isRecord = (value: unknown): value is JsonRecord =>
  value !== null && typeof value === "object" && !Array.isArray(value);

const finiteCount = (record: JsonRecord, key: string): string | null => {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? Math.trunc(value).toLocaleString("zh-CN")
    : null;
};

export function buildHomeTruthSummary(raw: unknown): HomeTruthSummaryCard[] {
  const projection = isRecord(raw) ? raw : {};
  const sessions = Array.isArray(projection.sessions)
    ? projection.sessions.filter(isRecord)
    : [];
  const latest = sessions[0];
  const latestId = latest && (
    typeof latest.audio_session_id === "string"
      ? latest.audio_session_id
      : typeof latest.id === "string"
        ? latest.id
        : null
  );

  const cards: HomeTruthSummaryCard[] = [
    {
      key: "running",
      label: "运行中",
      value: finiteCount(projection, "running_count") ?? "—",
      detail: finiteCount(projection, "running_count") === null
        ? "BFF 未提供运行中业务任务计数"
        : "当前租户与项目的业务运行",
      tone: "blue",
      route: "canvas",
      source: "bff"
    },
    {
      key: "pending",
      label: "待处理",
      value: finiteCount(projection, "pending_count") ?? "—",
      detail: finiteCount(projection, "pending_count") === null
        ? "BFF 未提供待处理队列计数"
        : "等待人工处理的业务任务",
      tone: "amber",
      route: "listening",
      source: "bff"
    },
    {
      key: "failed",
      label: "失败",
      value: finiteCount(projection, "model_anomaly_count") ?? "—",
      detail: finiteCount(projection, "model_anomaly_count") === null
        ? "BFF 未提供失败业务运行计数"
        : "失败、阻断或死信业务运行",
      tone: "red",
      route: "assets",
      source: "bff"
    },
    {
      key: "latest_import",
      label: "最新导入",
      value: sessions.length.toLocaleString("zh-CN"),
      detail: latestId ? `最新会话 ${latestId}` : "当前范围尚无导入会话",
      tone: "teal",
      route: "data",
      source: "bff"
    }
  ];
  return cards;
}
