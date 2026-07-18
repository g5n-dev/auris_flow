import { projectionNumber, projectionRecord, projectionText } from "../../shared/runtime/projectionRecords";
import type { TenantRiskFilter, TenantRow } from "./types";

export function normalizeTenantProjectionItems(items: unknown[]): TenantRow[] {
  return items.map((value, index) => {
    const record = projectionRecord(value, index, "tenant");
    const tenantId = projectionText(record, ["tenant_id", "id"], `tenant_${index + 1}`);
    const rawStatus = projectionText(record, ["status"], "unknown").toLowerCase();
    const status = rawStatus === "active"
      ? "活跃"
      : rawStatus === "trial"
        ? "试运行"
        : rawStatus === "paused"
          ? "暂停"
          : rawStatus === "configuring"
            ? "配置中"
            : projectionText(record, ["status"], "未提供");
    const storageValue = record.storage_usage_tb ?? record.storage;
    const storage = typeof storageValue === "number"
      ? `${storageValue.toLocaleString("zh-CN", { maximumFractionDigits: 1 })} TB`
      : typeof storageValue === "string" && storageValue.trim()
        ? storageValue
        : "未提供";
    return {
      name: projectionText(record, ["name", "tenant_name"], tenantId),
      status,
      projects: projectionNumber(record, ["project_count", "projects"]),
      members: projectionNumber(record, ["member_count", "members"]),
      storage,
      risk: projectionText(record, ["risk", "risk_status"], status === "活跃" ? "未提供" : status),
      tenantId,
      traceId: projectionText(record, ["trace_id"], "") || undefined
    };
  });
}

export function filterTenants(
  tenants: TenantRow[],
  queryValue: string,
  riskFilter: TenantRiskFilter
) {
  const query = queryValue.trim().toLowerCase();
  return tenants.filter((tenant) => {
    if (query && !`${tenant.name} ${tenant.status} ${tenant.risk}`.toLowerCase().includes(query)) return false;
    if (riskFilter === "risk") return tenant.risk !== "正常";
    if (riskFilter === "active") return tenant.status === "活跃";
    if (riskFilter === "trial") return tenant.status === "试运行";
    if (riskFilter === "paused") return tenant.status === "暂停";
    return true;
  });
}

export function resolveTenantOutputAssetKey(asset: string) {
  if (asset.includes("raw") || asset.includes("url_index") || asset.includes("audio_index")) return "auris/audio/raw_recordings";
  if (asset.includes("asr") || asset.includes("transcript")) return "auris/model/asr_transcripts";
  if (asset.includes("speaker")) return "auris/audio/voice_segments";
  if (asset.includes("quality") || asset.includes("report")) return "auris/eval/quality_metrics";
  return "auris/audio/voice_segments";
}

export function deriveTenantQuotaRows(selectedTenant: TenantRow) {
  const rows = [
    ["项目", selectedTenant.projects, selectedTenant.name === "极光汽车" ? 16 : selectedTenant.name === "北区经销集团" ? 10 : 6],
    ["成员", selectedTenant.members, selectedTenant.name === "极光汽车" ? 120 : selectedTenant.name === "北区经销集团" ? 80 : 32],
    ["存储", Number.parseFloat(selectedTenant.storage), selectedTenant.name === "极光汽车" ? 27 : selectedTenant.name === "北区经销集团" ? 16 : 6],
    ["月处理小时", selectedTenant.projects * 180, selectedTenant.name === "保险销售陪练" ? 500 : 2400],
    ["并发任务", selectedTenant.status === "暂停" ? 0 : selectedTenant.projects + 4, selectedTenant.name === "极光汽车" ? 24 : 12]
  ] as const;
  return rows.map(([name, used, limit]) => [
    String(name),
    Math.min(100, Math.round((Number(used) / Number(limit)) * 100)),
    `${used} / ${limit}`
  ] as [string, number, string]);
}
