import { useMemo, useState } from "react";

import {
  createAssetLineageEdges,
  createAssetLineageNodes,
  type AssetLineageNode
} from "./lineageModel";

export type AssetLineageMode = "impact" | "backfill" | "runs" | "review";

type UseAssetLineageOptions = {
  selectedAssetKey?: string;
  onSelect?: (assetKey: string) => void;
  onCreateBackfill?: (assetKey?: string) => void;
};

export function useAssetLineage({
  selectedAssetKey,
  onSelect,
  onCreateBackfill
}: UseAssetLineageOptions) {
  const [activeNodeId, setActiveNodeId] = useState<string | null>(null);
  const [lineageMode, setLineageMode] = useState<AssetLineageMode>("impact");
  const [lineageActionNote, setLineageActionNote] = useState("选择血缘节点后，可查看影响范围、创建回填草稿或派发人工复核。");
  const nodes = useMemo(() => createAssetLineageNodes(), []);
  const edges = useMemo(() => createAssetLineageEdges(), []);
  const selectedNode = nodes.find((node) => node.assetKey === selectedAssetKey);
  const activeNode = nodes.find((node) => node.id === activeNodeId) ?? selectedNode ?? nodes[0];
  const relatedNodeIds = new Set(
    edges
      .filter((edge) => edge.from === activeNode.id || edge.to === activeNode.id)
      .flatMap((edge) => [edge.from, edge.to])
      .concat(activeNode.id)
  );
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const directUpstream = edges
    .filter((edge) => edge.to === activeNode.id)
    .map((edge) => nodeById.get(edge.from))
    .filter((node): node is AssetLineageNode => Boolean(node));
  const directDownstream = edges
    .filter((edge) => edge.from === activeNode.id)
    .map((edge) => nodeById.get(edge.to))
    .filter((node): node is AssetLineageNode => Boolean(node));
  const impactedSamples = activeNode.status.includes("失败")
    ? "3 分区 / 42 片段"
    : activeNode.status.includes("待") || activeNode.tone === "human"
      ? "317 人审样本 / 12 下游"
      : activeNode.tone === "risk"
        ? "15 金额冲突 / 2 下游"
        : `${Math.max(24, (activeNode.quality ?? 92) - 54)} 样本 / ${directDownstream.length || 1} 下游`;
  const lineageModeOptions: Array<{ key: AssetLineageMode; label: string; meta: string }> = [
    { key: "impact", label: "影响分析", meta: "上游/下游" },
    { key: "backfill", label: "回填预案", meta: "分区与审批" },
    { key: "runs", label: "运行记录", meta: "Run / Check" },
    { key: "review", label: "人审介入", meta: "Human Loop" }
  ];
  const backfillPlanRows: Array<[string, string]> = [
    ["回填对象", activeNode.assetKey ?? activeNode.label],
    ["分区范围", activeNode.assetKey ? "2025-05-20 至 2025-05-26 / 极光中心店" : "当前节点关联分区"],
    ["影响样本", impactedSamples],
    ["覆盖策略", "不覆盖人工确认结果，写入候选资产版本"],
    ["审批规则", activeNode.tone === "human" || activeNode.tone === "risk" ? "必须人工确认" : "低风险可自动排队"]
  ];
  const reviewQueueRows: Array<[string, string, string]> = [
    ["HR-AF-128", "金额冲突复核", activeNode.tone === "label" || activeNode.tone === "human" ? "当前节点命中" : "相关下游"],
    ["HR-ASR-9021", "ASR 失败分区确认", activeNode.tone === "model" ? "当前节点命中" : "上游风险"],
    ["HR-BF-4410", "回填范围审批", activeNode.tone === "risk" ? "当前节点命中" : "待评估"]
  ];
  const actionButtons = [
    {
      label: "定位上游",
      detail: directUpstream[0]?.label ?? "无上游",
      onClick: () => {
        const nextNode = directUpstream[0];
        if (nextNode) handleNodeClick(nextNode);
        setLineageActionNote(nextNode ? `已定位上游节点：${nextNode.label}` : `${activeNode.label} 没有可定位上游。`);
      }
    },
    {
      label: "创建回填草稿",
      detail: impactedSamples,
      onClick: () => {
        setLineageMode("backfill");
        onCreateBackfill?.(activeNode.assetKey);
        setLineageActionNote(`${activeNode.label} 已生成回填草稿：按分区重算，等待审批后触发运行请求。`);
      }
    },
    {
      label: "派发人审",
      detail: activeNode.tone === "human" ? "当前队列" : "相关样本",
      onClick: () => {
        setLineageMode("review");
        setLineageActionNote(`${activeNode.label} 已加入 Human Loop，保留 asset_key、partition 和 trace_id。`);
      }
    },
    {
      label: activeNode.status.includes("失败") ? "重跑失败分区" : "查看运行记录",
      detail: activeNode.status,
      onClick: () => {
        setLineageMode("runs");
        setLineageActionNote(`${activeNode.label} 的运行记录已聚焦，失败分区会走幂等 run_key。`);
      }
    }
  ];
  const handleNodeClick = (node: AssetLineageNode) => {
    setActiveNodeId(node.id);
    setLineageActionNote(`已选择 ${node.label}，上下游路径和治理动作已按该节点刷新。`);
    if (node.assetKey) {
      onSelect?.(node.assetKey);
    }
  };

  return {
    actionButtons,
    activeNode,
    backfillPlanRows,
    directDownstream,
    directUpstream,
    edges,
    handleNodeClick,
    impactedSamples,
    lineageActionNote,
    lineageMode,
    lineageModeOptions,
    nodeById,
    nodes,
    relatedNodeIds,
    reviewQueueRows,
    setLineageActionNote,
    setLineageMode
  };
}

export type AssetLineageWorkspace = ReturnType<typeof useAssetLineage>;
