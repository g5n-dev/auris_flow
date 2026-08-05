import { reviewQueueMockData } from "../fixtures/evidenceFixtures";
import { getReviewQueueMock } from "../fixtures/reviewSamples";
import type { ReviewDecisionActions } from "../hooks/reviewDecisionActions";
import type { ListeningScope, ListeningToolMode, Mode } from "../types";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
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
        if (!LABEL_DEMO_MODE) {
          setListeningTool(null);
          setListeningNotice({
            status: "error",
            title: "生产模式暂不可重跑",
            detail: "当前页面尚未接入受控 TaskRun 重跑接口；已阻断本地计时器伪造成功状态。"
          });
          return;
        }
        setListeningRunState("pending");
        setListeningNotice({
          status: "pending",
          title: "DEMO：证据链重跑预览",
          detail: `${activeReviewSummary.refreshJob} 仅更新本地演示状态，不创建生产 TaskRun。`
        });
        setListeningRunState("success");
        setListeningNotice({
          status: "success",
          title: "DEMO：证据预览已更新",
          detail: `${activeSample.sessionId} 仅更新本地演示快照，未写入 BFF。`
        });
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
