import type { TaskScheduleMode } from "./types";

export const scheduleFrequencyCronMap: Record<string, string> = {
  "每 5 分钟": "*/5 * * * *",
  "每 15 分钟": "*/15 * * * *",
  "每 30 分钟": "*/30 * * * *",
  "每小时": "0 * * * *",
  "每天 02:00": "0 2 * * *"
};

export const defaultScheduleConfigs: Record<TaskScheduleMode, Record<string, string>> = {
  定时运行: {
    frequency: "每 15 分钟",
    cron: "*/15 * * * *",
    timezone: "Asia/Shanghai",
    partition: "tenant_id × store_id × business_date × 15m_window",
    activeWindow: "门店营业时间 09:00-21:00",
    runParams: "Agent 根据输入新鲜度、模型版本和实验策略生成",
    missedPolicy: "错过窗口不补跑，下一窗口继续"
  },
  手动运行: {
    operator: "当前登录用户",
    runScope: "2025-05-26|aurora-center",
    reason: "发布前 smoke test",
    stageScope: "全流程",
    overwritePolicy: "不覆盖已物化资产",
    approval: "无需审批，写入审计日志"
  },
  数据到达触发: {
    eventSources: "platform_session,audio_url,authenticated_event,document_event",
    readiness: "关键输入引用齐备",
    debounceMinutes: "2",
    cursor: "last_source_event_id",
    missingPolicy: "SkipReason，不启动 job",
    batchPolicy: "同一客户组事件合并"
  },
  一次性回填: {
    startDate: "2025-05-20",
    endDate: "2025-05-26",
    storeScope: "极光中心店",
    maxConcurrency: "4",
    recomputeScope: "只重算标签和复核",
    skipSucceeded: "跳过已成功分区"
  }
};
