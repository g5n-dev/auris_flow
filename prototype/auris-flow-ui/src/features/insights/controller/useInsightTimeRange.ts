import type { InsightsModuleProps } from "../types";
import type { HotwordInsightsState } from "./useHotwordInsights";
import type { InsightDatasetState } from "./useInsightDataset";
import type { InsightTimeRange } from "../types";
import { useEffect, useMemo, useState } from "react";

export function useInsightTimeRange(scope: InsightsModuleProps & HotwordInsightsState & InsightDatasetState) {
  const { dataset } = scope;
  const [timeRange, setTimeRange] = useState<InsightTimeRange>("30d");

  const [customRangeDraft, setCustomRangeDraft] = useState({ startDate: "2025-05-01", endDate: dataset.context.date });

  const [agentOutput, setAgentOutput] = useState("Insight Agent 已读取当前事实表，可以点击任意指标或图表元素查看证据、生成报告或创建复核动作。");

  useEffect(() => {
      setCustomRangeDraft((current) => ({ ...current, endDate: dataset.context.date }));
    }, [dataset.context.date]);

  const formatMonthDay = (dateValue: string) => {
      const [, month, day] = dateValue.split("-");
      if (!month || !day) return dateValue;
      return `${Number(month)}/${Number(day)}`;
    };

  const buildCustomRangeLabels = (startDate: string, endDate: string) => {
      if (!startDate || !endDate || startDate > endDate) return ["自定义"];
      const start = new Date(`${startDate}T00:00:00`);
      const end = new Date(`${endDate}T00:00:00`);
      const span = Math.max(0, end.getTime() - start.getTime());
      const steps = span === 0 ? [0] : [0, 0.25, 0.5, 0.75, 1];
      return steps.map((ratio) => {
        const point = new Date(start.getTime() + span * ratio);
        const yyyy = point.getFullYear();
        const mm = String(point.getMonth() + 1).padStart(2, "0");
        const dd = String(point.getDate()).padStart(2, "0");
        return formatMonthDay(`${yyyy}-${mm}-${dd}`);
      });
    };

  const customRangeError =
      !customRangeDraft.startDate || !customRangeDraft.endDate
        ? "请选择开始和结束日期"
        : customRangeDraft.startDate > customRangeDraft.endDate
          ? "开始日期不能晚于结束日期"
          : "";

  const customRangeDetail = customRangeError
      ? "待输入"
      : `${formatMonthDay(customRangeDraft.startDate)}-${formatMonthDay(customRangeDraft.endDate)}`;

  const customReportScope = customRangeError
      ? "自定义范围待完善"
      : `自定义 ${customRangeDraft.startDate} 至 ${customRangeDraft.endDate}`;

  const updateCustomRangeDraft = (next: { startDate: string; endDate: string }) => {
      setCustomRangeDraft(next);
      setTimeRange("custom");
      const nextError =
        !next.startDate || !next.endDate
          ? "请选择开始和结束日期"
          : next.startDate > next.endDate
            ? "开始日期不能晚于结束日期"
            : "";
      setAgentOutput(
        nextError
          ? `自定义时间范围未生效：${nextError}。`
          : `已切换到自定义 ${next.startDate} 至 ${next.endDate}，北极星、趋势、排行和报告摘要将按该范围重新计算。`
      );
    };

  const applyCustomPreset = (days: number) => {
      const end = new Date(`${dataset.context.date}T00:00:00`);
      const start = new Date(end);
      start.setDate(end.getDate() - days + 1);
      const toDateInput = (date: Date) => {
        const yyyy = date.getFullYear();
        const mm = String(date.getMonth() + 1).padStart(2, "0");
        const dd = String(date.getDate()).padStart(2, "0");
        return `${yyyy}-${mm}-${dd}`;
      };
      updateCustomRangeDraft({ startDate: toDateInput(start), endDate: toDateInput(end) });
    };

  const insightTimeRanges = useMemo<Array<{ key: InsightTimeRange; label: string; detail: string; labels: string[]; reportScope: string }>>(() => [
      { key: "today", label: "今日", detail: "小时级", labels: ["09", "10", "11", "12", "13", "14", "15", "18"], reportScope: `${dataset.context.date} 当日小时` },
      { key: "7d", label: "7天", detail: "短期波动", labels: ["5/20", "5/21", "5/22", "5/23", "5/24", "5/25", "5/26"], reportScope: "最近 7 天" },
      { key: "30d", label: "30天", detail: "经营周期", labels: ["W1", "W2", "W3", "W4", "W5"], reportScope: "最近 30 天" },
      { key: "90d", label: "90天", detail: "季度趋势", labels: ["4月", "5月", "6月", "7月"], reportScope: "最近 90 天" },
      { key: "custom", label: "自定义", detail: customRangeDetail, labels: buildCustomRangeLabels(customRangeDraft.startDate, customRangeDraft.endDate), reportScope: customReportScope }
    ], [customRangeDetail, customRangeDraft.endDate, customRangeDraft.startDate, customReportScope, dataset.context.date]);

  return {
    timeRange,
    setTimeRange,
    customRangeDraft,
    setCustomRangeDraft,
    agentOutput,
    setAgentOutput,
    formatMonthDay,
    buildCustomRangeLabels,
    customRangeError,
    customRangeDetail,
    customReportScope,
    updateCustomRangeDraft,
    applyCustomPreset,
    insightTimeRanges
  };
}

export type InsightTimeRangeState = ReturnType<typeof useInsightTimeRange>;
