import { useCallback, useRef, useState } from "react";
import type { DataAssetItem } from "../shared/contracts/dataAssets";
import type { ModuleDeepLink, ModuleKey } from "../shared/contracts/navigation";
import { deepLinkBadcaseRegistry } from "../shared/fixtures/deepLinkBadcases";
import {
  deepLinkDataAssetRegistry,
  deepLinkEvidenceRegistry,
  deepLinkLabelCaseRegistry
} from "../shared/fixtures/deepLinkTargets";
import { LABEL_DEMO_MODE } from "../shared/runtime/demoMode";
import type { Lang, Theme } from "../shared/contracts/application";
import { assetRows } from "../workspace/moduleWorkspaceCatalog";

export function useShellNavigation() {
  const [accountSettingsOpen, setAccountSettingsOpen] = useState(false);
  const [activeModule, setActiveModule] = useState<ModuleKey>("home");
  const [deepLinkTarget, setDeepLinkTarget] = useState<ModuleDeepLink | null>(null);
  const [theme, setTheme] = useState<Theme>("light");
  const [lang, setLang] = useState<Lang>("zh");
  const [selectedDataAssetId, setSelectedDataAssetId] = useState("AF-128");
  const [selectedAssetKey, setSelectedAssetKey] = useState("auris/label/event_tags");
  const listeningNavigationResolverRef = useRef<((target: ModuleDeepLink) => ModuleDeepLink | null) | null>(null);
  const registerListeningNavigationResolver = useCallback((resolver: ((target: ModuleDeepLink) => ModuleDeepLink | null) | null) => {
    listeningNavigationResolverRef.current = resolver;
  }, []);

  const navigateModuleRoot = (module: ModuleKey) => {
    setDeepLinkTarget(null);
    setActiveModule(module);
  };

  const openListeningFromDataAsset = (asset: DataAssetItem) => {
    navigateToTarget({
      module: "listening",
      objectKind: "dataAsset",
      objectId: asset.id,
      title: asset.event,
      detail: `${asset.audio} / ${asset.docs.join("、")}`,
      focusMode: "evidence",
      origin: { label: "数据管理 / 叶子资产", module: "data", objectLabel: asset.id }
    });
  };

  const openAssetsFromDataAsset = (asset: DataAssetItem) => {
    setSelectedDataAssetId(asset.id);
    setSelectedAssetKey(asset.assetKey);
    setDeepLinkTarget({
      module: "assets",
      tab: "detail",
      objectKind: "asset",
      objectId: asset.assetKey,
      title: asset.event,
      detail: `${asset.id} / ${asset.partitionKey}`,
      focusMode: "detail",
      origin: { label: "数据管理 / 资产链路", module: "data", objectLabel: asset.id }
    });
    setActiveModule("assets");
  };

  const resolveDeepLinkTarget = (target: ModuleDeepLink): ModuleDeepLink | null => {
    const next: ModuleDeepLink = { ...target };
    if (target.module === "listening" && (target.objectKind === "dataAsset" || target.objectKind === "reviewSample")) {
      const runtimeTarget = listeningNavigationResolverRef.current?.(target);
      if (runtimeTarget) return runtimeTarget;
      if (!LABEL_DEMO_MODE) return null;
    }
    if (target.objectKind === "evidence" && target.objectId) {
      if (!LABEL_DEMO_MODE) return null;
      const relation = deepLinkEvidenceRegistry[target.objectId];
      if (!relation) return null;
      next.title = target.title ?? relation.label;
      next.detail = target.detail ?? `${relation.dataAssetId} / ${relation.assetKey}`;
      next.window = target.window ?? relation.window;
      if (target.module === "listening") {
        next.objectKind = "reviewSample";
        next.objectId = relation.sampleId;
      }
      if (target.module === "data") {
        next.objectKind = "dataAsset";
        next.objectId = relation.dataAssetId;
      }
      if (target.module === "assets") {
        next.objectKind = "asset";
        next.objectId = relation.assetKey;
      }
      if (target.module === "labels") {
        next.objectKind = "labelIntent";
        next.objectId = relation.labelIntentKey;
      }
      if (target.module === "evaluation" && relation.badcaseId) {
        next.objectKind = "evaluationBadcase";
        next.objectId = relation.badcaseId;
      }
      return next;
    }
    if (target.objectKind === "dataAsset" && target.objectId) {
      if (!LABEL_DEMO_MODE && target.module === "listening") return null;
      const relation = deepLinkDataAssetRegistry[target.objectId];
      if (!relation) return null;
      next.title = target.title ?? relation.label;
      next.detail = target.detail ?? `${target.objectId} / ${relation.assetKey}`;
      next.window = target.window ?? relation.window;
      if (target.module === "listening") {
        next.objectKind = "reviewSample";
        next.objectId = relation.sampleId;
      }
      if (target.module === "assets") {
        next.objectKind = "asset";
        next.objectId = relation.assetKey;
      }
      return next;
    }
    if (target.objectKind === "evaluationBadcase" && target.objectId && target.module === "listening") {
      if (!LABEL_DEMO_MODE) return null;
      const relation = deepLinkBadcaseRegistry[target.objectId];
      if (!relation) return null;
      next.objectKind = "reviewSample";
      next.objectId = relation.sampleId;
      next.title = target.title ?? relation.label;
      next.detail = target.detail ?? `${relation.capability} / ${relation.assetKey}`;
      next.window = target.window ?? relation.window;
      return next;
    }
    if (target.objectKind === "evaluationCase" && target.objectId && target.module === "listening") {
      if (!LABEL_DEMO_MODE) return null;
      const relation = deepLinkLabelCaseRegistry[target.objectId];
      if (!relation) return null;
      next.objectKind = "reviewSample";
      next.objectId = relation.sampleId;
      next.title = target.title ?? relation.label;
      next.detail = target.detail ?? `${relation.intentKey} / ${relation.evidenceId}`;
      next.window = target.window ?? relation.window;
      return next;
    }
    if (target.objectKind === "labelReview" && target.module === "listening") {
      if (!LABEL_DEMO_MODE) return null;
      next.objectKind = "reviewSample";
      next.objectId = "sample-af-128";
      next.title = target.title ?? "Human Loop HR-1029";
      next.detail = target.detail ?? "规则 vs ASR / 报价金额冲突";
      next.window = target.window ?? "12:27:18 - 12:28:01";
      return next;
    }
    if (target.objectKind === "reviewSample" && target.objectId) {
      const runtimeTarget = listeningNavigationResolverRef.current?.(next);
      if (runtimeTarget) return runtimeTarget;
      const knownDemoSample = Object.values(deepLinkDataAssetRegistry).some((relation) => relation.sampleId === target.objectId);
      if (!LABEL_DEMO_MODE || !knownDemoSample) return null;
    }
    if (target.objectKind === "asset" && target.objectId && !assetRows.some((asset) => asset.assetKey === target.objectId)) {
      return null;
    }
    return next;
  };

  const navigateToTarget = (target: ModuleDeepLink) => {
    const resolved = resolveDeepLinkTarget(target);
    if (!resolved) {
      setDeepLinkTarget({
        module: activeModule,
        title: "目标详情缺少演示绑定",
        detail: `未找到 ${target.objectKind ?? "对象"}:${target.objectId ?? "unknown"}，已停留当前页面。`,
        origin: target.origin
      });
      return;
    }
    if (resolved.module === "data" && resolved.objectKind === "dataAsset" && resolved.objectId) {
      setSelectedDataAssetId(resolved.objectId);
    }
    if (resolved.module === "assets" && resolved.objectKind === "asset" && resolved.objectId) {
      setSelectedAssetKey(resolved.objectId);
    }
    setDeepLinkTarget(resolved);
    setActiveModule(resolved.module);
  };

  return {
    accountSettingsOpen,
    activeModule,
    deepLinkTarget,
    lang,
    navigateModuleRoot,
    navigateToTarget,
    openAssetsFromDataAsset,
    openListeningFromDataAsset,
    registerListeningNavigationResolver,
    selectedAssetKey,
    selectedDataAssetId,
    setAccountSettingsOpen,
    setLang,
    setSelectedAssetKey,
    setSelectedDataAssetId,
    setTheme,
    theme
  };
}
