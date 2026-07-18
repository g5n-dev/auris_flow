import type {
  LabelVersionItem,
  ManualLabelDraftReceipt,
  ManualLabelRebasePreview,
  ManualLabelValueType,
  ReleaseBundleHead
} from "../../../../../api/manualLabelClient";

export type ManualLabelWorkflowStatus =
  | "idle"
  | "loading-scope"
  | "ready"
  | "saving-draft"
  | "draft"
  | "submitting"
  | "stale"
  | "previewing"
  | "awaiting-confirmation"
  | "rebasing"
  | "submitted"
  | "error";

export type ManualLabelWorkflowState = {
  status: ManualLabelWorkflowStatus;
  sourceRegionId: string | null;
  releaseHead: ReleaseBundleHead | null;
  items: LabelVersionItem[];
  selectedLabelId: string;
  draft: ManualLabelDraftReceipt | null;
  mappingBundleId: string;
  preview: ManualLabelRebasePreview | null;
  rebaseConfirmed: boolean;
  message: string;
};

export const initialManualLabelWorkflowState: ManualLabelWorkflowState = {
  status: "idle",
  sourceRegionId: null,
  releaseHead: null,
  items: [],
  selectedLabelId: "",
  draft: null,
  mappingBundleId: "",
  preview: null,
  rebaseConfirmed: false,
  message: "打开标签区域后读取 production Release Head。"
};

const normalize = (value: string) => value.trim().toLocaleLowerCase("zh-CN");

export function matchLabelVersionItem(
  items: readonly LabelVersionItem[],
  fieldKey: string,
  label: string
): LabelVersionItem | null {
  const candidates = [fieldKey, label].map(normalize).filter(Boolean);
  const exact = items.filter((item) => {
    const names = [item.label_id, item.canonical_name, ...item.aliases].map(normalize);
    return candidates.some((candidate) => names.includes(candidate));
  });
  return exact.length === 1 ? exact[0] : null;
}

export function parseManualLabelValue(
  valueType: ManualLabelValueType,
  rawValue: string,
  startMs: number,
  endMs: number
): unknown {
  const value = rawValue.trim();
  if (valueType === "boolean") {
    if (["true", "1", "是", "有", "命中"].includes(value.toLocaleLowerCase("zh-CN"))) return true;
    if (["false", "0", "否", "无", "未命中"].includes(value.toLocaleLowerCase("zh-CN"))) return false;
    throw new Error("布尔标签值必须是 true/false、是/否或 1/0");
  }
  if (valueType === "numeric") {
    const numberValue = Number(value);
    if (!value || !Number.isFinite(numberValue)) throw new Error("数值标签必须填写有限数字");
    return numberValue;
  }
  if (valueType === "multi") {
    const values = [...new Set(value.split(/[，,]/).map((item) => item.trim()).filter(Boolean))];
    if (!values.length) throw new Error("多值标签至少填写一个值，并用逗号分隔");
    return values;
  }
  if (valueType === "temporal") return { start_ms: startMs, end_ms: endMs };
  if (!value) throw new Error("标签值不能为空");
  return value;
}

export function resolveManualLabelOccurredAt(
  sessionStartedAt: string | undefined,
  audioSessionId: string,
  regionClock: string,
  startMs: number
): string | null {
  if (sessionStartedAt) {
    const startedAt = new Date(sessionStartedAt);
    if (!Number.isNaN(startedAt.getTime())) {
      return new Date(startedAt.getTime() + startMs).toISOString();
    }
  }
  const dateMatch = audioSessionId.match(/S(\d{4})(\d{2})(\d{2})/);
  const timeMatch = regionClock.match(/^(\d{2}):(\d{2}):(\d{2})$/);
  if (!dateMatch || !timeMatch) return null;
  const [, year, month, day] = dateMatch;
  const [, hour, minute, second] = timeMatch;
  const parsed = new Date(`${year}-${month}-${day}T${hour}:${minute}:${second}+08:00`);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

export async function sha256Evidence(document: Record<string, unknown>): Promise<string> {
  if (!globalThis.crypto?.subtle) throw new Error("当前浏览器不支持证据 SHA-256，已阻断写入");
  const sorted = Object.fromEntries(Object.entries(document).sort(([left], [right]) => left.localeCompare(right)));
  const bytes = new TextEncoder().encode(JSON.stringify(sorted));
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function mappingBundleFromLifecycle(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const replacement = (value as Record<string, unknown>).replacement;
  if (!replacement || typeof replacement !== "object" || Array.isArray(replacement)) return "";
  const mappingBundleId = (replacement as Record<string, unknown>).mapping_bundle_id;
  return typeof mappingBundleId === "string" ? mappingBundleId.trim() : "";
}

export function rebasedAnnotationId(annotationId: string, generation: number): string {
  const suffix = `-rebase-g${generation}`;
  return `${annotationId.slice(0, 128 - suffix.length)}${suffix}`;
}
