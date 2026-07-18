import type { BackendActionReceipt } from "../../../api/client";
import type { OperationNotice, OperationStatus } from "../../../shared/contracts/operations";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { defaultEvidencePageConfig } from "../fixtures/evidenceFixtures";
import { initialListeningSample } from "../fixtures/reviewSamples";
import type { ReviewSample } from "../fixtures/reviewSamples";
import type { AppealableReviewDecision, EvidencePageConfig, ListeningFeatureProps, ListeningScope, ListeningToolMode, MarkState, Mode, PanelTab } from "../types";
import { useState } from "react";

export function useListeningState(props: ListeningFeatureProps) {
  const { active, activeModule, currentUser, focus, getModuleTitle, navigateModuleRoot, navigateToTarget, registerListeningNavigationResolver, setSelectedDataAssetId, setSelectedAssetKey, topbarContext } = props;
  const [mode, setMode] = useState<Mode>("evidence");

  const [listeningScope, setListeningScope] = useState<ListeningScope>("segment");

  const [panelTab, setPanelTab] = useState<PanelTab>("agent");

  const [selectedWindow, setSelectedWindow] = useState("12:23 - 12:33");

  const [markState, setMarkState] = useState<MarkState>("none");

  const [agentState, setAgentState] = useState<"pending" | "accepted" | "rejected">("pending");

  const [activeQueue, setActiveQueue] = useState(initialListeningSample.queue);

  const [activeChip, setActiveChip] = useState(initialListeningSample.queue);

  const [pageConfigOpen, setPageConfigOpen] = useState(false);

  const [listeningTool, setListeningTool] = useState<ListeningToolMode | null>(null);

  const [listeningQuery, setListeningQuery] = useState("");

  const [evidencePageConfig, setEvidencePageConfig] = useState<EvidencePageConfig>(defaultEvidencePageConfig);

  const [activeSampleId, setActiveSampleId] = useState(initialListeningSample.id);

  const [backendReviewSamples, setBackendReviewSamples] = useState<ReviewSample[]>([]);

  const [listeningReadState, setListeningReadState] = useState<"idle" | "loading" | "ready" | "empty" | "error">(
      LABEL_DEMO_MODE ? "ready" : "idle"
    );

  const [listeningReadDetail, setListeningReadDetail] = useState(
      LABEL_DEMO_MODE ? "显式 DEMO 模式：使用本地调听样本。" : "等待读取 HumanReviewTask 与 AudioSession。"
    );

  const [listeningReadRetry, setListeningReadRetry] = useState(0);

  const [completedSampleIds, setCompletedSampleIds] = useState<string[]>([]);

  const [listeningNotice, setListeningNotice] = useState<OperationNotice>({
      status: "idle",
      title: "等待复核动作",
      detail: "队列切换、接受/拒绝、保存边界、重跑和确认下一通会写入当前会话处理记录。"
    });

  const [listeningActionPending, setListeningActionPending] = useState(false);

  const [listeningRunState, setListeningRunState] = useState<OperationStatus>("idle");

  const [latestReviewDecision, setLatestReviewDecision] = useState<AppealableReviewDecision | null>(null);

  const [appealComposerOpen, setAppealComposerOpen] = useState(false);

  const [appealReason, setAppealReason] = useState("");

  const [appealPending, setAppealPending] = useState(false);

  const [createdAppeal, setCreatedAppeal] = useState<BackendActionReceipt | null>(null);

  return { active, activeModule, currentUser, focus, getModuleTitle, navigateModuleRoot, navigateToTarget, registerListeningNavigationResolver, setSelectedDataAssetId, setSelectedAssetKey, topbarContext, mode, setMode, listeningScope, setListeningScope, panelTab, setPanelTab, selectedWindow, setSelectedWindow, markState, setMarkState, agentState, setAgentState, activeQueue, setActiveQueue, activeChip, setActiveChip, pageConfigOpen, setPageConfigOpen, listeningTool, setListeningTool, listeningQuery, setListeningQuery, evidencePageConfig, setEvidencePageConfig, activeSampleId, setActiveSampleId, backendReviewSamples, setBackendReviewSamples, listeningReadState, setListeningReadState, listeningReadDetail, setListeningReadDetail, listeningReadRetry, setListeningReadRetry, completedSampleIds, setCompletedSampleIds, listeningNotice, setListeningNotice, listeningActionPending, setListeningActionPending, listeningRunState, setListeningRunState, latestReviewDecision, setLatestReviewDecision, appealComposerOpen, setAppealComposerOpen, appealReason, setAppealReason, appealPending, setAppealPending, createdAppeal, setCreatedAppeal };
}

export type ListeningState = ReturnType<typeof useListeningState>;
