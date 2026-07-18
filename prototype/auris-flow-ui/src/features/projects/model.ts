import { projectionNumber, projectionRecord, projectionText } from "../../shared/runtime/projectionRecords";
import type { ProjectRow, ProjectStatusFilter } from "./types";

export function normalizeProjectProjectionItems(items: unknown[]): ProjectRow[] {
  return items.map((value, index) => {
    const record = projectionRecord(value, index, "project");
    const projectId = projectionText(record, ["project_id", "id"], `project_${index + 1}`);
    const rawStatus = projectionText(record, ["status"], "unknown").toLowerCase();
    const status = rawStatus === "active" || rawStatus === "running"
      ? "运行中"
      : rawStatus === "evaluating"
        ? "评测中"
        : rawStatus === "failed" || rawStatus === "error"
          ? "异常"
          : rawStatus === "pending_ingest"
            ? "待接入"
            : rawStatus === "configuring"
              ? "配置中"
              : projectionText(record, ["status"], "未提供");
    const passRate = projectionNumber(record, ["pass_rate", "auto_pass_rate"], Number.NaN);
    const assetStatus = projectionText(record, ["asset_status", "asset"], "未提供").toLowerCase();
    const asset = assetStatus === "healthy"
      ? "健康"
      : assetStatus === "backfill_pending"
        ? "待回填"
        : assetStatus;
    const added = record.today_added ?? record.audio_count ?? record.added;
    return {
      name: projectionText(record, ["name", "project_name"], projectId),
      owner: projectionText(record, ["owner_name", "owner"], "未提供"),
      status,
      added: typeof added === "number" ? added.toLocaleString("zh-CN") : typeof added === "string" && added.trim() ? added : "—",
      pending: projectionNumber(record, ["pending_count", "pending"]),
      pass: Number.isFinite(passRate) ? `${passRate.toLocaleString("zh-CN", { maximumFractionDigits: 1 })}%` : "—",
      asset,
      projectId,
      traceId: projectionText(record, ["trace_id"], "") || undefined,
      scene: projectionText(record, ["scene"], ""),
      dataMode: projectionText(record, ["data_mode"], ""),
      labelVersion: projectionText(record, ["label_version"], ""),
      qualityTarget: projectionText(record, ["quality_target"], "")
    };
  });
}

export function filterProjects(
  projects: ProjectRow[],
  queryValue: string,
  statusFilter: ProjectStatusFilter
) {
  const query = queryValue.trim().toLowerCase();
  return projects.filter((project) => {
    if (query && !`${project.name} ${project.owner} ${project.status} ${project.asset} ${project.projectId ?? ""}`.toLowerCase().includes(query)) return false;
    if (statusFilter === "running") return project.status === "运行中";
    if (statusFilter === "attention") return project.status !== "运行中" || project.asset !== "健康" || project.pending > 20;
    return true;
  });
}

export function projectIdFromName(name: string) {
  const asciiSlug = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 24);
  const hash = Array.from(name).reduce((value, char) => (value * 31 + char.charCodeAt(0)) >>> 0, 7).toString(36);
  return `ui_project_${asciiSlug || hash}_${Date.now().toString(36)}`;
}
