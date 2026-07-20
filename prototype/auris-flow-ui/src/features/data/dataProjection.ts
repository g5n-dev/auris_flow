import type { DataAssetItem } from "../../shared/contracts/dataAssets";
import { isRecordValue } from "../../shared/runtime/records";
import {
  authoritativeProcessingProducts,
  normalizeSessionConfidence
} from "./dataTruthModel";

export const DATA_CONNECTOR_TARGET_BLOCKED_REASON = "当前会话未绑定本租户项目内已登记的数据资产";
export const DATA_PROJECTION_SCHEMA_BLOCKED_REASON = "BFF 音频会话聚合结构无可验证叶子对象，连接器导入已禁用";

export type DataProjectionAssetItem = DataAssetItem & {
  connectorImportEnabled: boolean;
  connectorImportBlockedReason: string;
};

export type DataProjectionResult = {
  items: DataProjectionAssetItem[];
  blockedReason: string;
};

function nonEmptyString(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function sessionStatus(value: unknown): DataAssetItem["status"] {
  if (value === "success") return "confirmed";
  if (value === "pending" || value === "pending_review") return "pending";
  return "risk";
}

function sessionDuration(startedAt: string, endedAt: string) {
  if (!startedAt || !endedAt) return "BFF 未提供";
  const durationMs = Date.parse(endedAt) - Date.parse(startedAt);
  if (!Number.isFinite(durationMs) || durationMs < 0) return "BFF 未提供";
  const totalSeconds = Math.floor(durationMs / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}分${String(seconds).padStart(2, "0")}秒`;
}

export function projectDataAggregationItems(projectionItems: unknown[] | undefined): DataProjectionResult {
  if (!projectionItems?.length || projectionItems.some((group) => !isRecordValue(group))) {
    return { items: [], blockedReason: DATA_PROJECTION_SCHEMA_BLOCKED_REASON };
  }

  const rows: DataProjectionAssetItem[] = [];
  for (const rawGroup of projectionItems) {
    const group = rawGroup as Record<string, unknown>;
    if (!Array.isArray(group.children) || group.children.some((child) => !isRecordValue(child))) {
      return { items: [], blockedReason: DATA_PROJECTION_SCHEMA_BLOCKED_REASON };
    }
    const groupKey = nonEmptyString(group.group_key) || "unknown / BFF 未提供分组";
    for (const rawChild of group.children) {
      const child = rawChild as Record<string, unknown>;
      const audioSessionId = nonEmptyString(child.audio_session_id);
      const recordingId = nonEmptyString(child.recording_id);
      if (!audioSessionId || !recordingId) {
        return { items: [], blockedReason: DATA_PROJECTION_SCHEMA_BLOCKED_REASON };
      }

      const connector = isRecordValue(child.connector_import) ? child.connector_import : null;
      const targetAssetKey = nonEmptyString(child.target_asset_key);
      const connectorImportEnabled = connector?.enabled === true && Boolean(targetAssetKey);
      const connectorImportBlockedReason = connectorImportEnabled
        ? ""
        : nonEmptyString(connector?.blocked_reason) || DATA_CONNECTOR_TARGET_BLOCKED_REASON;
      const startedAt = nonEmptyString(child.started_at);
      const endedAt = nonEmptyString(child.ended_at);

      rows.push({
        id: audioSessionId,
        space: nonEmptyString(child.store_id) || "BFF 未提供门店",
        time: groupKey,
        person: nonEmptyString(child.primary_employee_id) || "BFF 未绑定人物",
        event: nonEmptyString(child.event_type) || "BFF 未提供事件类型",
        tags: [],
        audio: recordingId,
        duration: sessionDuration(startedAt, endedAt),
        confidence: normalizeSessionConfidence(child.confidence),
        status: sessionStatus(child.status),
        docs: [],
        assetKey: targetAssetKey,
        partitionKey: groupKey,
        materializationId: "BFF 未提供",
        dagsterRunId: "BFF 未提供",
        freshness: "BFF 未提供",
        upstreamAssets: [],
        downstreamAssets: [],
        assetCheck: connectorImportEnabled ? "目标数据资产已验证" : connectorImportBlockedReason,
        bffEndpoint: `/api/v1/audio-sessions/${encodeURIComponent(audioSessionId)}`,
        processingProducts: authoritativeProcessingProducts(child),
        connectorImportEnabled,
        connectorImportBlockedReason
      });
    }
  }

  return rows.length
    ? { items: rows, blockedReason: "" }
    : { items: [], blockedReason: DATA_PROJECTION_SCHEMA_BLOCKED_REASON };
}
