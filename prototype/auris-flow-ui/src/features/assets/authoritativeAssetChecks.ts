type UnknownRecord = Record<string, unknown>;

export type AuthoritativeAssetCheckStatus =
  | "passed"
  | "success"
  | "failed"
  | "error"
  | "warning"
  | "pending"
  | "running"
  | "skipped";

export type AuthoritativeAssetCheck = {
  id: string;
  name: string;
  status: AuthoritativeAssetCheckStatus;
  failedPartitions: string[];
};

export type AuthoritativeAssetChecks = {
  assetKey: string;
  label: string;
  traceId: string | null;
  checks: AuthoritativeAssetCheck[];
};

export type FailedAssetCheckSelection = {
  failedCheckIds: string[];
  failedPartitions: string[];
};

export type AssetChecksParseResult =
  | { ok: true; value: AuthoritativeAssetChecks }
  | { ok: false; reason: string };

export type AssetChecksReadState = {
  assetKey: string;
  scopeKey: string;
  requestKey: string;
  status: "idle" | "loading" | "ready" | "empty" | "error";
  value: AuthoritativeAssetChecks | null;
  reason: string;
};

export type AssetChecksReadAction =
  | { type: "begin"; assetKey: string; scopeKey: string; requestKey: string }
  | { type: "ready"; requestKey: string; value: AuthoritativeAssetChecks }
  | { type: "empty"; requestKey: string; value: AuthoritativeAssetChecks }
  | { type: "error"; requestKey: string; reason: string };

export type AssetCheckRetryDecision = {
  enabled: boolean;
  blockedReason: string;
  selection: FailedAssetCheckSelection | null;
};

const CHECK_STATUSES = new Set<AuthoritativeAssetCheckStatus>([
  "passed",
  "success",
  "failed",
  "error",
  "warning",
  "pending",
  "running",
  "skipped"
]);

export const initialAssetChecksState: AssetChecksReadState = {
  assetKey: "",
  scopeKey: "",
  requestKey: "",
  status: "idle",
  value: null,
  reason: ""
};

function isRecord(value: unknown): value is UnknownRecord {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function text(value: unknown): string {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function invalid(reason: string): AssetChecksParseResult {
  return { ok: false, reason };
}

function parseFailedPartitions(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  const partitions: string[] = [];
  for (const rawPartition of value) {
    const partition = text(rawPartition);
    if (!partition || partitions.includes(partition)) return null;
    partitions.push(partition);
  }
  return partitions;
}

export function parseAuthoritativeAssetChecks(
  raw: unknown,
  expectedAssetKey: string
): AssetChecksParseResult {
  if (!isRecord(raw)) return invalid("资产详情不是对象");
  const assetKey = text(raw.asset_key);
  if (!assetKey || assetKey !== expectedAssetKey) return invalid("checks asset_key 与当前选择不一致");
  if (!Array.isArray(raw.checks)) return invalid("资产详情未返回 checks 数组");

  const checks: AuthoritativeAssetCheck[] = [];
  const checkIds = new Set<string>();
  for (const rawCheck of raw.checks) {
    if (!isRecord(rawCheck)) return invalid("check 不是对象");
    const id = text(rawCheck.check_id);
    const name = text(rawCheck.name);
    const status = text(rawCheck.status);
    const failedPartitions = parseFailedPartitions(rawCheck.failed_partitions);
    if (
      !id
      || !name
      || !CHECK_STATUSES.has(status as AuthoritativeAssetCheckStatus)
      || failedPartitions === null
      || checkIds.has(id)
    ) {
      return invalid("check 缺少强 check_id/name/status/failed_partitions，或存在重复与非法值");
    }
    checkIds.add(id);
    checks.push({
      id,
      name,
      status: status as AuthoritativeAssetCheckStatus,
      failedPartitions
    });
  }

  return {
    ok: true,
    value: {
      assetKey,
      label: text(raw.display_name) || assetKey.split("/").slice(-1)[0] || assetKey,
      traceId: text(raw.trace_id) || null,
      checks
    }
  };
}

export function failedAssetCheckSelection(
  value: AuthoritativeAssetChecks
): FailedAssetCheckSelection {
  const failedChecks = value.checks.filter(
    (check) => check.status === "failed" || check.status === "error" || check.failedPartitions.length > 0
  );
  return {
    failedCheckIds: failedChecks.map((check) => check.id),
    failedPartitions: Array.from(new Set(failedChecks.flatMap((check) => check.failedPartitions)))
  };
}

export function assetChecksStateReducer(
  state: AssetChecksReadState,
  action: AssetChecksReadAction
): AssetChecksReadState {
  if (action.type === "begin") {
    return {
      assetKey: action.assetKey,
      scopeKey: action.scopeKey,
      requestKey: action.requestKey,
      status: "loading",
      value: null,
      reason: ""
    };
  }
  if (action.requestKey !== state.requestKey) return state;
  if (action.type === "error") {
    return { ...state, status: "error", value: null, reason: action.reason };
  }
  return {
    ...state,
    status: action.type === "empty" ? "empty" : "ready",
    value: action.value,
    reason: ""
  };
}

export function readChecksStateForSelectedAsset(
  state: AssetChecksReadState,
  selectedAssetKey: string,
  selectedScopeKey: string
): AssetChecksReadState {
  if (state.assetKey === selectedAssetKey && state.scopeKey === selectedScopeKey) return state;
  return {
    assetKey: selectedAssetKey,
    scopeKey: selectedScopeKey,
    requestKey: "",
    status: "loading",
    value: null,
    reason: ""
  };
}

export function assetCheckRetryDecision(
  state: AssetChecksReadState,
  sceneLockReady: boolean,
  sceneBlockedReason: string
): AssetCheckRetryDecision {
  if (state.status === "loading" || state.status === "idle") {
    return { enabled: false, blockedReason: "正在读取当前资产的权威 checks", selection: null };
  }
  if (state.status === "error") {
    return { enabled: false, blockedReason: `权威 checks 读取失败：${state.reason}`, selection: null };
  }
  if (state.status === "empty" || !state.value?.checks.length) {
    return { enabled: false, blockedReason: "当前资产详情未返回 checks，质量重跑保持禁用", selection: null };
  }
  const selection = failedAssetCheckSelection(state.value);
  if (!selection.failedCheckIds.length) {
    return { enabled: false, blockedReason: "当前权威 checks 没有失败或错误项，禁止无目标重跑", selection: null };
  }
  if (!sceneLockReady) {
    return { enabled: false, blockedReason: sceneBlockedReason, selection: null };
  }
  return { enabled: true, blockedReason: "", selection };
}
