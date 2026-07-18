import { reviewQueueMockData } from "../fixtures/evidenceFixtures";
import { getReviewQueueMock } from "../fixtures/reviewSamples";
import type { ReviewDecisionActions } from "../hooks/reviewDecisionActions";
import type { ListeningScope, ListeningToolMode, Mode } from "../types";
import { useMemo } from "react";

export function buildListeningPresentation(context: ReviewDecisionActions) {
  const { activeSample, selectedLabel, setListeningNotice, setListeningRunState, setListeningTool } = context;
  const activeReviewMock = useMemo(() => getReviewQueueMock(activeSample.queue), [activeSample.queue]);

  const activeReviewSummary = useMemo(
      () => ({
        title: activeSample.queueTitle,
        state: selectedLabel,
        confidence: activeSample.confidence,
        queue: activeReviewMock.label,
        queueCount: activeReviewMock.count,
        dataAssetId: activeReviewMock.dataAssetId,
        assetKey: activeReviewMock.assetKey,
        api: activeReviewMock.api,
        dagsterAsset: activeReviewMock.dagsterAsset,
        refreshJob: activeReviewMock.refreshJob,
        linkedViews: activeReviewMock.linkedViews.join(" / ")
      }),
      [activeReviewMock, activeSample.confidence, activeSample.queueTitle, selectedLabel]
    );

  const listeningModes: Array<{ id: Mode; label: string; note: string; scope: ListeningScope }> = [
      { id: "simple", label: "完整调听", note: "还原会话", scope: "conversation" },
      { id: "evidence", label: "证据审查", note: "逐条判定", scope: "segment" },
      { id: "matrix", label: "审音矩阵", note: "关系排查", scope: "segment" }
    ];

  const reviewQueueItems = reviewQueueMockData;

  const toggleListeningTool = (tool: ListeningToolMode) => {
      setListeningTool((current) => (current === tool ? null : tool));
      if (tool === "rerun") {
        setListeningRunState("pending");
        setListeningNotice({
          status: "pending",
          title: "证据链重跑已排队",
          detail: `${activeReviewSummary.refreshJob} 正在重跑当前样本的转写、串音、标签和单据比对。`
        });
        window.setTimeout(() => {
          setListeningRunState("success");
          setListeningNotice({
            status: "success",
            title: "证据链重跑完成",
            detail: `${activeSample.sessionId} 已生成新的证据快照，当前页面已保留人工标记和队列上下文。`
          });
        }, 760);
        return;
      }
      setListeningNotice({
        status: "idle",
        title: tool === "search" ? "证据检索已打开" : tool === "filter" ? "队列过滤已打开" : "接待单候选已打开",
        detail:
          tool === "search"
            ? "输入关键词后只在当前会话、资产 Key 和单据链路中检索。"
            : tool === "filter"
              ? "过滤条件会收敛当前复核队列，不改变已处理记录。"
              : "按门店、员工、客户、时间窗和单据字段展示当前会话可关联的销售接待单。"
      });
    };

  return { ...context, activeReviewMock, activeReviewSummary, listeningModes, reviewQueueItems, toggleListeningTool };
}

export type ListeningPresentation = ReturnType<typeof buildListeningPresentation>;
