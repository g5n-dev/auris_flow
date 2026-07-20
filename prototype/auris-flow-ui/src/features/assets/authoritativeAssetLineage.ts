type UnknownRecord = Record<string, unknown>;

export type AuthoritativeAssetLineageAsset = {
  assetKey: string;
  label: string;
  traceId: string | null;
};

export type AuthoritativeAssetLineageNode = {
  id: string;
  label: string;
  nodeType: string;
  direction: string;
  traceId: string | null;
  rootTraceId: string | null;
  runId: string | null;
  partitionKey: string | null;
};

export type AuthoritativeAssetLineageEdge = {
  id: string;
  from: string;
  to: string;
  direction: string;
  source: string;
  relation: string | null;
  traceId: string | null;
};

export type AuthoritativeAssetMaterialization = {
  id: string;
  assetKey: string;
  status: string;
  runId: string | null;
  partitionKey: string | null;
  traceId: string | null;
};

export type AuthoritativeAssetLineage = {
  asset: AuthoritativeAssetLineageAsset;
  nodes: AuthoritativeAssetLineageNode[];
  edges: AuthoritativeAssetLineageEdge[];
  materializations: AuthoritativeAssetMaterialization[];
};

export type AssetLineageParseResult =
  | { ok: true; value: AuthoritativeAssetLineage }
  | { ok: false; reason: string };

export type AssetLineageReadState = {
  assetKey: string;
  scopeKey: string;
  requestKey: string;
  status: "idle" | "loading" | "ready" | "empty" | "error";
  value: AuthoritativeAssetLineage | null;
  reason: string;
};

export type AssetLineageReadAction =
  | { type: "begin"; assetKey: string; scopeKey: string; requestKey: string }
  | { type: "ready"; requestKey: string; value: AuthoritativeAssetLineage }
  | { type: "empty"; requestKey: string; value: AuthoritativeAssetLineage }
  | { type: "error"; requestKey: string; reason: string };

export const initialAssetLineageState: AssetLineageReadState = {
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

function optionalText(value: unknown): string | null {
  return text(value) || null;
}

function invalid(reason: string): AssetLineageParseResult {
  return { ok: false, reason };
}

export function parseAuthoritativeAssetLineage(
  raw: unknown,
  expectedAssetKey: string
): AssetLineageParseResult {
  if (!isRecord(raw)) return invalid("lineage data 不是对象");
  if (!isRecord(raw.asset)) return invalid("lineage 缺少 asset");
  const assetKey = text(raw.asset.asset_key);
  if (!assetKey || assetKey !== expectedAssetKey) return invalid("lineage asset_key 与当前选择不一致");
  if (!Array.isArray(raw.nodes) || !Array.isArray(raw.edges) || !Array.isArray(raw.materializations)) {
    return invalid("lineage 缺少 nodes、edges 或 materializations 数组");
  }

  const nodes: AuthoritativeAssetLineageNode[] = [];
  const nodeIds = new Set<string>();
  for (const rawNode of raw.nodes) {
    if (!isRecord(rawNode)) return invalid("lineage node 不是对象");
    const id = text(rawNode.asset_key);
    const label = text(rawNode.label);
    const nodeType = text(rawNode.node_type);
    if (!id || !label || !nodeType || nodeIds.has(id)) return invalid("lineage node 缺少强 ID、label、node_type 或存在重复");
    nodeIds.add(id);
    nodes.push({
      id,
      label,
      nodeType,
      direction: text(rawNode.direction) || "unknown",
      traceId: optionalText(rawNode.trace_id),
      rootTraceId: optionalText(rawNode.root_trace_id),
      runId: optionalText(rawNode.run_id),
      partitionKey: optionalText(rawNode.partition_key)
    });
  }
  if (!nodeIds.has(assetKey)) return invalid("lineage nodes 未包含当前资产");

  const edges: AuthoritativeAssetLineageEdge[] = [];
  const edgeIds = new Set<string>();
  for (const rawEdge of raw.edges) {
    if (!isRecord(rawEdge)) return invalid("lineage edge 不是对象");
    const from = text(rawEdge.from) || text(rawEdge.source_asset_key);
    const to = text(rawEdge.to) || text(rawEdge.target_asset_key);
    const id = text(rawEdge.edge_id) || `${from}->${to}`;
    if (!from || !to || !nodeIds.has(from) || !nodeIds.has(to) || edgeIds.has(id)) {
      return invalid("lineage edge 端点不存在、缺少 ID 或存在重复");
    }
    edgeIds.add(id);
    edges.push({
      id,
      from,
      to,
      direction: text(rawEdge.direction) || "unknown",
      source: text(rawEdge.lineage_source) || "unknown",
      relation: optionalText(rawEdge.relation),
      traceId: optionalText(rawEdge.trace_id)
    });
  }

  const materializations: AuthoritativeAssetMaterialization[] = [];
  const materializationIds = new Set<string>();
  for (const rawMaterialization of raw.materializations) {
    if (!isRecord(rawMaterialization)) return invalid("materialization 不是对象");
    const id = text(rawMaterialization.materialization_id);
    const materializationAssetKey = text(rawMaterialization.asset_key);
    if (!id || materializationAssetKey !== assetKey || materializationIds.has(id)) {
      return invalid("materialization 缺少强 ID、跨资产或存在重复");
    }
    materializationIds.add(id);
    materializations.push({
      id,
      assetKey: materializationAssetKey,
      status: text(rawMaterialization.status) || "unknown",
      runId: optionalText(rawMaterialization.run_id),
      partitionKey: optionalText(rawMaterialization.partition_key),
      traceId: optionalText(rawMaterialization.trace_id)
    });
  }

  return {
    ok: true,
    value: {
      asset: {
        assetKey,
        label: text(raw.asset.display_name) || assetKey.split("/").slice(-1)[0] || assetKey,
        traceId: optionalText(raw.asset.trace_id)
      },
      nodes,
      edges,
      materializations
    }
  };
}

export function assetLineageStateReducer(
  state: AssetLineageReadState,
  action: AssetLineageReadAction
): AssetLineageReadState {
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

export function readStateForSelectedAsset(
  state: AssetLineageReadState,
  selectedAssetKey: string,
  selectedScopeKey: string
): AssetLineageReadState {
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

export function assetLineageIsEmpty(value: AuthoritativeAssetLineage): boolean {
  return value.edges.length === 0 && value.materializations.length === 0 && value.nodes.length <= 1;
}
