import { useState } from "react";

import type { OperationNotice } from "../../shared/contracts/operations";
import { LABEL_DEMO_MODE } from "../../shared/runtime/demoMode";
import { resolveProjectSceneLock } from "../../shared/runtime/projectSceneLock";
import { isRecordValue } from "../../shared/runtime/records";
import { assetCheckRetryDecision } from "./authoritativeAssetChecks";
import {
  assetRows as demoAssetRows,
  assetDagsterCompatibilityChecks,
  assetModuleConfig
} from "./catalog";
import type {
  AssetBackfillDraft,
  AssetCatalogRow,
  AssetCompatibilityStatus,
  AssetsModuleProps,
  HotwordBackfillRecovery
} from "./types";
import { useAssetActions } from "./useAssetActions";
import { useAuthoritativeAssetChecks } from "./useAuthoritativeAssetChecks";
import { useHotwordBackfillRecovery } from "./useHotwordBackfillRecovery";
import {
  backfillDraftForScope,
  loadingHotwordRecovery,
  resolveHotwordRecoveryForScope
} from "./hotwordRecoveryScope";

const text = (value: unknown, fallback: string) => typeof value === "string" && value.trim() ? value : fallback;
const list = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && Boolean(item.trim())) : [];

function projectionAssetRows(items: unknown[] | undefined): AssetCatalogRow[] {
  return (items ?? []).filter(isRecordValue).flatMap((item) => {
    const assetKey = text(item.asset_key, "");
    if (!assetKey) return [];
    const rawStatus = text(item.status, "unknown");
    const status = rawStatus === "success" ? "已生成" : rawStatus === "risk" ? "待回填" : rawStatus === "warning" ? "部分失败" : rawStatus;
    const downstream = list(item.downstream);
    return [{
      name: text(item.display_name, assetKey),
      domain: text(item.domain, "未分类"),
      status,
      version: text(item.version, "BFF 未提供"),
      quality: typeof item.quality_score === "number" ? item.quality_score : null,
      assetKey,
      definition: text(item.definition, "BFF 数据资产"),
      partition: text(item.latest_partition_key, text(item.partition, "BFF 未提供")),
      materialization: text(item.latest_materialization_id, "尚无生成记录"),
      owner: text(item.owner, "未分配"),
      freshness: text(item.freshness, "BFF 未提供"),
      upstream: list(item.upstream).join(" / ") || "无已登记上游",
      downstream,
      backfill: text(item.backfill, "仅允许通过受控回填接口创建运行"),
      checks: list(item.checks)
    }];
  });
}

export function useAssetsWorkspace({
  activeTab,
  setActiveTab,
  selectedAssetKey,
  setSelectedAssetKey,
  navigateToTarget,
  projectionItems,
  readScopeKey,
  workspaceSceneBinding,
  workspaceSceneState
}: AssetsModuleProps) {
  const assetRows = LABEL_DEMO_MODE ? demoAssetRows : projectionAssetRows(projectionItems);
  const [backfillDraft, setBackfillDraft] = useState<AssetBackfillDraft | null>(null);
  const [hotwordBackfillRecoveryState, setHotwordBackfillRecovery] = useState<HotwordBackfillRecovery>(
    () => loadingHotwordRecovery(readScopeKey)
  );
  const hotwordBackfillRecovery = resolveHotwordRecoveryForScope(
    hotwordBackfillRecoveryState,
    readScopeKey
  );
  const [compatFilter, setCompatFilter] = useState<"全部" | Exclude<AssetCompatibilityStatus, "兼容">>("全部");
  const [assetAction, setAssetAction] = useState<string | null>(null);
  const [assetNotice, setAssetNotice] = useState<OperationNotice>({
    status: "idle",
    title: "等待资产操作",
    detail: "回填、重跑和导出记录运行链路。"
  });
  const selectedAsset = assetRows.find((asset) => asset.assetKey === selectedAssetKey) ?? assetRows[0];
  if (!selectedAsset) throw new Error("BFF 资产投影缺少有效 asset_key");
  const { lock: sceneProfileLock, blockedReason: sceneProfileBlockedReason } = resolveProjectSceneLock(
    workspaceSceneBinding,
    workspaceSceneState
  );
  const authoritativeAssetChecks = useAuthoritativeAssetChecks(selectedAsset.assetKey, readScopeKey);
  const qualityRetryDecision = assetCheckRetryDecision(
    authoritativeAssetChecks,
    sceneProfileLock !== null,
    sceneProfileBlockedReason
  );
  const partitionReady = LABEL_DEMO_MODE || selectedAsset.partition !== "BFF 未提供";
  const backfillReady = partitionReady && sceneProfileLock !== null;
  const backfillBlockedReason = !sceneProfileLock
    ? sceneProfileBlockedReason
    : partitionReady
      ? ""
      : "BFF 尚未返回权威 partition_key；UI 草稿可以保留，但不会发送占位值";
  useHotwordBackfillRecovery(readScopeKey, setHotwordBackfillRecovery);
  const assetDomainSummary = Array.from(new Set(assetRows.map((asset) => asset.domain))).map((domain) => {
    const rows = assetRows.filter((asset) => asset.domain === domain);
    const qualityScores = rows.flatMap((asset) => asset.quality === null ? [] : [asset.quality]);
    return {
      domain,
      count: rows.length,
      avgQuality: qualityScores.length ? Math.round(qualityScores.reduce((sum, quality) => sum + quality, 0) / qualityScores.length) : null,
      attention: rows.filter((asset) => asset.status !== "已生成").length
    };
  });
  const maxDomainCount = Math.max(...assetDomainSummary.map((item) => item.count), 1);
  const statusSummary = [
    { label: "已生成", count: assetRows.filter((asset) => asset.status === "已生成").length, tone: "ok" },
    { label: "部分失败", count: assetRows.filter((asset) => asset.status.includes("失败")).length, tone: "danger" },
    { label: "待回填", count: assetRows.filter((asset) => asset.status.includes("待")).length, tone: "warn" }
  ];
  const healthyCount = statusSummary.find((item) => item.label === "已生成")?.count ?? 0;
  const healthyPercent = Math.round((healthyCount / assetRows.length) * 100);
  const qualityScores = assetRows.flatMap((asset) => asset.quality === null ? [] : [asset.quality]);
  const avgAssetQuality = qualityScores.length ? Math.round(qualityScores.reduce((sum, quality) => sum + quality, 0) / qualityScores.length) : null;
  const qualityMin = qualityScores.length ? Math.min(...qualityScores) - 3 : 0;
  const qualityMax = qualityScores.length ? Math.max(...qualityScores) + 3 : 100;
  const qualityChart = {
    width: 520,
    height: 172,
    left: 34,
    right: 496,
    top: 24,
    bottom: 132
  };
  const qualityX = (index: number) =>
    qualityChart.left + index * ((qualityChart.right - qualityChart.left) / Math.max(assetRows.length - 1, 1));
  const qualityY = (value: number) =>
    qualityChart.bottom - ((value - qualityMin) / Math.max(qualityMax - qualityMin, 1)) * (qualityChart.bottom - qualityChart.top);
  const qualityPath = assetRows
    .flatMap((asset, index) => asset.quality === null ? [] : [[index, asset.quality] as const])
    .map(([index, quality], pointIndex) => `${pointIndex === 0 ? "M" : "L"}${qualityX(index).toFixed(1)} ${qualityY(quality).toFixed(1)}`)
    .join(" ");
  const scopedBackfillDraft = backfillDraftForScope(backfillDraft, readScopeKey);
  const currentDraft = scopedBackfillDraft?.assetKey === selectedAsset.assetKey ? scopedBackfillDraft : null;
  const visibleCompatibilityChecks = assetDagsterCompatibilityChecks.filter(
    ([, status]) => compatFilter === "全部" || status === compatFilter
  );
  const compatibleCount = assetDagsterCompatibilityChecks.filter(([, status]) => status === "兼容").length;
  const compatibilityScore = Math.round((compatibleCount / assetDagsterCompatibilityChecks.length) * 100);
  const currentTabLabel =
    assetModuleConfig.tabs.find((tab) => tab.id === activeTab)?.label ?? assetModuleConfig.tabs[0].label;
  const actions = useAssetActions({
    assetAction,
    assetRows,
    assetNotice,
    backfillDraft,
    backfillReady,
    backfillBlockedReason,
    currentDraft,
    hotwordBackfillRecovery,
    qualityRetryBlockedReason: qualityRetryDecision.blockedReason,
    qualityRetryReady: qualityRetryDecision.enabled,
    qualityRetrySelection: qualityRetryDecision.selection,
    readScopeKey,
    sceneProfileBlockedReason,
    sceneProfileLock,
    selectedAsset,
    setAssetAction,
    setAssetNotice,
    setBackfillDraft,
    setSelectedAssetKey
  });

  return {
    ...actions,
    activeTab,
    assetAction,
    assetRows,
    assetDomainSummary,
    assetNotice,
    avgAssetQuality,
    backfillDraft,
    backfillReady,
    backfillBlockedReason,
    compatFilter,
    compatibilityScore,
    compatibleCount,
    currentDraft,
    currentTabLabel,
    healthyCount,
    healthyPercent,
    hotwordBackfillRecovery,
    maxDomainCount,
    navigateToTarget,
    qualityChart,
    qualityPath,
    authoritativeAssetChecks,
    qualityRetryBlockedReason: qualityRetryDecision.blockedReason,
    qualityRetryReady: qualityRetryDecision.enabled,
    qualityX,
    qualityY,
    readScopeKey,
    sceneProfileBlockedReason,
    sceneProfileLock,
    selectedAsset,
    setActiveTab,
    setAssetNotice,
    setBackfillDraft,
    setCompatFilter,
    setSelectedAssetKey,
    statusSummary,
    visibleCompatibilityChecks
  };
}

export type AssetsWorkspace = ReturnType<typeof useAssetsWorkspace>;
