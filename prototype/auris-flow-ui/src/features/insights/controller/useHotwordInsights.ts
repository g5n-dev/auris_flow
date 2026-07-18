import type { InsightsModuleProps } from "../types";
import type { HotwordStatistics, HotwordStatisticsItem } from "../../../api/client";
import { createHotwordAnalysisRun, getBackendRun, getHotwordStatistics, listHotwordBadcases } from "../../../api/client";
import type { OperationNotice } from "../../../shared/contracts/operations";
import { backendRunFailed, backendRunStatusLabel, backendRunSucceeded } from "../../../shared/runtime/backendRunStatus";
import type { InsightTabKey } from "../types";
import { useEffect, useState } from "react";

export function useHotwordInsights(scope: InsightsModuleProps) {
  const { activeTab, navigateToTarget, topbarContext } = scope;
  const insightTabLabels: Record<InsightTabKey, string> = {
      business: "业务大盘",
      store: "门店归因",
      sales: "销售绩效",
      tags: "标签资产",
      quality: "模型质量",
      reports: "报告中心"
    };

  const currentTab = (["business", "store", "sales", "tags", "quality", "reports"].includes(activeTab) ? activeTab : "business") as InsightTabKey;

  const currentTabLabel = insightTabLabels[currentTab];

  const hotwordEmptyStatistics: HotwordStatistics = {
      metrics: {
        coverage_rate: Number.NaN,
        recall_rate: Number.NaN,
        error_rate: Number.NaN,
        false_boost_rate: Number.NaN,
        impacted_sessions: Number.NaN
      },
      items: [],
      discovery: {
        annotation_correction_count: 0,
        unique_terms: 0,
        impacted_session_count: 0,
        threshold_met_term_count: 0,
        evidence_level: "discovery",
        eligible_for_release_gate: false
      }
    };

  const [hotwordStatistics, setHotwordStatistics] = useState<HotwordStatistics>(hotwordEmptyStatistics);

  const [hotwordFilters, setHotwordFilters] = useState({
      startDate: "2025-05-20",
      endDate: topbarContext.date,
      storeId: "BJ-AURORA-001",
      provider: "auris-audio-stack",
      modelVersion: "audio-v2.3.1",
      versionId: "hwpv-auto-sales-v1-8"
    });

  const [hotwordLoadNotice, setHotwordLoadNotice] = useState<OperationNotice>({
      status: "idle",
      title: "等待热词统计",
      detail: "进入模型质量后按当前门店、Provider、模型与词包版本读取预计算快照。"
    });

  const [hotwordLoadRevision, setHotwordLoadRevision] = useState(0);

  const [hotwordAnalysisNotice, setHotwordAnalysisNotice] = useState<OperationNotice>({
      status: "idle",
      title: "热词分析待运行",
      detail: "分析只生成指标快照和 Badcase 候选，不修改历史转写。"
    });

  const [hotwordAnalysisPending, setHotwordAnalysisPending] = useState(false);

  useEffect(() => {
      if (currentTab !== "quality") return;
      let mounted = true;
      setHotwordLoadNotice({
        status: "pending",
        title: "正在读取热词统计",
        detail: `${hotwordFilters.storeId} / ${hotwordFilters.provider} / ${hotwordFilters.versionId}`
      });
      Promise.allSettled([
        getHotwordStatistics({
          date_from: hotwordFilters.startDate,
          date_to: hotwordFilters.endDate,
          store_id: hotwordFilters.storeId,
          provider: hotwordFilters.provider,
          model_version: hotwordFilters.modelVersion,
          hotword_pack_version_id: hotwordFilters.versionId
        }),
        listHotwordBadcases({
          hotword_pack_version_id: hotwordFilters.versionId,
          limit: 20
        })
      ])
        .then(([statisticsResult, badcaseResult]) => {
          if (!mounted) return;
          if (statisticsResult.status === "rejected") throw statisticsResult.reason;
          const response = statisticsResult.value;
          const badcaseItems = badcaseResult.status === "fulfilled" ? badcaseResult.value.data.items : [];
          let linkedBadcaseCount = 0;
          const items = response.data.items.map((item) => {
            const badcaseIds = Array.isArray(item.badcase_ids) ? item.badcase_ids : [item.badcase_id];
            const badcase = badcaseItems.find((candidate) => badcaseIds.includes(candidate.badcase_id));
            if (!badcase) return item;
            linkedBadcaseCount += 1;
            if (item.data_source === "listening_annotation") {
              return {
                ...item,
                badcase_id: badcase.badcase_id,
                badcase_status: badcase.status,
                badcase_resource_version: badcase.resource_version
              };
            }
            const expectedCount = badcase.expected_count || item.expected_count;
            const weightedErrors = badcase.weighted_error_count ?? item.weighted_error_count ?? 0;
            return {
              ...item,
              ...badcase,
              badcase_id: badcase.badcase_id,
              canonical_term: badcase.standard_term ?? badcase.canonical_term,
              error_rate: badcase.error_rate || (expectedCount ? Math.round((weightedErrors / expectedCount) * 1000) / 10 : 0)
            };
          });
          setHotwordStatistics({ ...response.data, items });
          if (badcaseResult.status === "rejected") {
            setHotwordLoadNotice({
              status: "error",
              title: "热词统计已加载，Badcase 关联失败",
              detail: `${badcaseResult.reason instanceof Error ? badcaseResult.reason.message : "unknown error"}；统计快照已保留，点击重试补齐关联。`
            });
            return;
          }
          setHotwordLoadNotice({
            status: "success",
            title: "热词统计已同步",
            detail: `${items.length} 个易错词 · 标注修正 ${response.data.discovery.annotation_correction_count} 次（仅发现） · ${linkedBadcaseCount} 个已关联后端 Badcase · Trace ${response.meta?.trace_id ?? "no-trace"}`
          });
        })
        .catch((error) => {
          if (!mounted) return;
          setHotwordStatistics(hotwordEmptyStatistics);
          setHotwordLoadNotice({
            status: "error",
            title: "热词统计读取失败",
            detail: `${error instanceof Error ? error.message : "unknown error"}；未使用 mock 或零值覆盖真实响应。`
          });
        });
      return () => {
        mounted = false;
      };
    }, [
      currentTab,
      hotwordFilters.endDate,
      hotwordFilters.modelVersion,
      hotwordFilters.provider,
      hotwordFilters.startDate,
      hotwordFilters.storeId,
      hotwordFilters.versionId,
      hotwordLoadRevision
    ]);

  const runHotwordAnalysis = async () => {
      if (hotwordAnalysisPending) return;
      setHotwordAnalysisPending(true);
      setHotwordAnalysisNotice({
        status: "pending",
        title: "热词分析运行创建中",
        detail: `${hotwordFilters.startDate} 至 ${hotwordFilters.endDate} / ${hotwordFilters.provider}`
      });
      try {
        const response = await createHotwordAnalysisRun({
          date_from: hotwordFilters.startDate,
          date_to: hotwordFilters.endDate,
          store_id: hotwordFilters.storeId,
          provider: hotwordFilters.provider,
          model_version: hotwordFilters.modelVersion,
          hotword_pack_version_id: hotwordFilters.versionId
        });
        const runId = response.data.id;
        const initialTrace = response.meta?.trace_id ?? response.data.trace_id;
        for (let attempt = 1; attempt <= 10; attempt += 1) {
          const run = await getBackendRun(runId);
          const status = run.data.status;
          const trace = run.meta?.trace_id ?? run.data.trace_id ?? initialTrace;
          if (backendRunFailed(status)) {
            setHotwordAnalysisNotice({
              status: "error",
              title: "热词分析运行失败",
              detail: `${runId} · ${backendRunStatusLabel(status)} · Trace ${trace ?? "no-trace"}`
            });
            return;
          }
          if (backendRunSucceeded(status)) {
            setHotwordAnalysisNotice({
              status: "success",
              title: "热词分析已完成",
              detail: `${runId} · Trace ${trace ?? "no-trace"} · 正在刷新统计快照`
            });
            setHotwordLoadRevision((current) => current + 1);
            return;
          }
          setHotwordAnalysisNotice({
            status: "pending",
            title: "热词分析运行已创建",
            detail: `${runId} · ${backendRunStatusLabel(status)} · 轮询 ${attempt}/10 · Trace ${trace ?? "no-trace"}`
          });
          if (attempt < 10) await new Promise<void>((resolve) => window.setTimeout(resolve, 500));
        }
        setHotwordAnalysisNotice({
          status: "error",
          title: "热词分析等待超时",
          detail: `${runId} 在有限轮询窗口内未完成，可稍后从 RunRecord 恢复。`
        });
      } catch (error) {
        setHotwordAnalysisNotice({
          status: "error",
          title: "热词分析创建失败",
          detail: error instanceof Error ? error.message : "后端未返回可追踪运行。"
        });
      } finally {
        setHotwordAnalysisPending(false);
      }
    };

  const openHotwordBadcase = (item: HotwordStatisticsItem) => {
      navigateToTarget({
        module: "evaluation",
        tab: "badcase",
        objectKind: "evaluationBadcase",
        objectId: item.badcase_id,
        focusMode: "evidence",
        title: `${item.canonical_term ?? item.standard_term ?? "热词"} / ${item.error_type}`,
        detail: `${item.recognized_text} · 优先级 ${item.priority ?? item.priority_score ?? 0}`,
        origin: { label: "洞察 / 模型质量 / ASR 热词", module: "insights", objectLabel: item.badcase_id }
      });
    };

  return {
    insightTabLabels,
    currentTab,
    currentTabLabel,
    hotwordEmptyStatistics,
    hotwordStatistics,
    setHotwordStatistics,
    hotwordFilters,
    setHotwordFilters,
    hotwordLoadNotice,
    setHotwordLoadNotice,
    hotwordLoadRevision,
    setHotwordLoadRevision,
    hotwordAnalysisNotice,
    setHotwordAnalysisNotice,
    hotwordAnalysisPending,
    setHotwordAnalysisPending,
    runHotwordAnalysis,
    openHotwordBadcase
  };
}

export type HotwordInsightsState = ReturnType<typeof useHotwordInsights>;
