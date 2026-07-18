import { useEffect, useRef } from "react";

import type { ModuleDeepLink } from "../../../shared/contracts/navigation";
import type { ListeningPresentation } from "../model/listeningPresentation";

export function useListeningFocus(context: ListeningPresentation) {
  const {
    active,
    focus,
    listeningReadState,
    registerListeningNavigationResolver,
    reviewSamplePool,
    selectReviewSample,
    setListeningNotice,
    setListeningScope,
    setMode,
    setSelectedWindow
  } = context;
  const appliedFocusRef = useRef<ModuleDeepLink | null>(null);

  useEffect(() => {
    registerListeningNavigationResolver((target) => {
      if (target.module !== "listening") return null;
      const sample = target.objectKind === "dataAsset" && target.objectId
        ? reviewSamplePool.find((item) => item.dataAssetId === target.objectId)
        : target.objectKind === "reviewSample" && target.objectId
          ? reviewSamplePool.find((item) => item.id === target.objectId)
          : undefined;
      if (!sample) return null;
      return {
        ...target,
        objectKind: "reviewSample",
        objectId: sample.id,
        title: target.title ?? sample.queueTitle,
        detail: target.detail ?? `${sample.dataAssetId} / ${sample.assetKey}`,
        window: target.window ?? sample.window
      };
    });
    return () => registerListeningNavigationResolver(null);
  }, [registerListeningNavigationResolver, reviewSamplePool]);

  useEffect(() => {
    if (!focus || focus.module !== "listening") {
      appliedFocusRef.current = null;
      return;
    }
    if (!active || appliedFocusRef.current === focus) return;
    if (focus.title === "目标详情缺少演示绑定") {
      appliedFocusRef.current = focus;
      setListeningNotice({
        status: "error",
        title: "目标详情缺少演示绑定",
        detail: focus.detail ?? `未找到 ${focus.objectKind ?? "对象"}:${focus.objectId ?? "unknown"}，已停留当前页面。`
      });
      return;
    }
    if (listeningReadState === "idle" || listeningReadState === "loading") return;
    const sample = focus.objectKind === "reviewSample" && focus.objectId
      ? reviewSamplePool.find((item) => item.id === focus.objectId)
      : reviewSamplePool[0];
    if (!sample) {
      appliedFocusRef.current = focus;
      setListeningNotice({
        status: "error",
        title: "调听对象不可用",
        detail: `${focus.objectId ?? "当前目标"} 未关联权威复核任务；不会载入演示样本。`
      });
      return;
    }
    appliedFocusRef.current = focus;
    selectReviewSample(sample);
    setSelectedWindow(focus.window ?? sample.window);
    setMode(focus.focusMode === "matrix" ? "matrix" : "evidence");
    setListeningScope("segment");
    setListeningNotice({
      status: "success",
      title: `已定位${focus.title ?? sample.queueTitle}`,
      detail: `${focus.origin?.label ?? "关联跳转"} → ${sample.sessionId} / ${focus.window ?? sample.window} / ${sample.assetKey}`
    });
  }, [active, focus, listeningReadState, reviewSamplePool, selectReviewSample, setListeningNotice, setListeningScope, setMode, setSelectedWindow]);

  return context;
}

export type ListeningController = ReturnType<typeof useListeningFocus>;
