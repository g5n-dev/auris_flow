import { useState } from "react";

import type { OperationNotice } from "../../shared/contracts/operations";
import {
  assetDagsterCompatibilityChecks,
  assetModuleConfig,
  assetRows
} from "./catalog";
import type {
  AssetBackfillDraft,
  AssetCompatibilityStatus,
  AssetsModuleProps,
  HotwordBackfillRecovery
} from "./types";
import { useAssetActions } from "./useAssetActions";
import { useHotwordBackfillRecovery } from "./useHotwordBackfillRecovery";

export function useAssetsWorkspace({
  activeTab,
  setActiveTab,
  selectedAssetKey,
  setSelectedAssetKey,
  navigateToTarget
}: AssetsModuleProps) {
  const [backfillDraft, setBackfillDraft] = useState<AssetBackfillDraft | null>(null);
  const [hotwordBackfillRecovery, setHotwordBackfillRecovery] = useState<HotwordBackfillRecovery>({
    status: "loading",
    reason: "正在读取已发布词包与权威 ASR 物化。"
  });
  const [compatFilter, setCompatFilter] = useState<"全部" | Exclude<AssetCompatibilityStatus, "兼容">>("全部");
  const [assetAction, setAssetAction] = useState<string | null>(null);
  const [assetNotice, setAssetNotice] = useState<OperationNotice>({
    status: "idle",
    title: "等待资产操作",
    detail: "回填、重跑和导出记录运行链路。"
  });
  const selectedAsset = assetRows.find((asset) => asset.assetKey === selectedAssetKey) ?? assetRows[0];
  useHotwordBackfillRecovery(setHotwordBackfillRecovery);
  const assetDomainSummary = Array.from(new Set(assetRows.map((asset) => asset.domain))).map((domain) => {
    const rows = assetRows.filter((asset) => asset.domain === domain);
    return {
      domain,
      count: rows.length,
      avgQuality: Math.round(rows.reduce((sum, asset) => sum + asset.quality, 0) / rows.length),
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
  const avgAssetQuality = Math.round(assetRows.reduce((sum, asset) => sum + asset.quality, 0) / assetRows.length);
  const qualityMin = Math.min(...assetRows.map((asset) => asset.quality)) - 3;
  const qualityMax = Math.max(...assetRows.map((asset) => asset.quality)) + 3;
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
    .map((asset, index) => `${index === 0 ? "M" : "L"}${qualityX(index).toFixed(1)} ${qualityY(asset.quality).toFixed(1)}`)
    .join(" ");
  const currentDraft = backfillDraft?.assetKey === selectedAsset.assetKey ? backfillDraft : null;
  const visibleCompatibilityChecks = assetDagsterCompatibilityChecks.filter(
    ([, status]) => compatFilter === "全部" || status === compatFilter
  );
  const compatibleCount = assetDagsterCompatibilityChecks.filter(([, status]) => status === "兼容").length;
  const compatibilityScore = Math.round((compatibleCount / assetDagsterCompatibilityChecks.length) * 100);
  const currentTabLabel =
    assetModuleConfig.tabs.find((tab) => tab.id === activeTab)?.label ?? assetModuleConfig.tabs[0].label;
  const actions = useAssetActions({
    assetAction,
    assetNotice,
    backfillDraft,
    currentDraft,
    hotwordBackfillRecovery,
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
    assetDomainSummary,
    assetNotice,
    avgAssetQuality,
    backfillDraft,
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
    qualityX,
    qualityY,
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
