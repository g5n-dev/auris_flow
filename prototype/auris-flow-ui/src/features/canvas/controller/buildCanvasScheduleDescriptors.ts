import type {
  CanvasNullableRow,
  CanvasRunConfigDescriptor,
  CanvasSchedulePlan,
  CanvasSchedulePlanDescriptor
} from "../fixtures/viewDescriptors";
import type { TaskScheduleMode } from "../types";

type CanvasSchedulePlanContext = {
  scheduleConfigs: Record<TaskScheduleMode, Record<string, string>>;
  activeIntentTaskId: string;
  selectedCanvasVariantKey: string;
  activePartitionKey: string;
};

function resolvePlaceholders(values: Array<string | null>, replacements: string[]): string[] {
  let replacementIndex = 0;
  return values.map((value) => value ?? replacements[replacementIndex++]);
}

function resolveRowPlaceholders(rows: Array<Array<string | null>>, replacements: string[]): string[][] {
  let replacementIndex = 0;
  return rows.map((row) => row.map((value) => value ?? replacements[replacementIndex++]));
}

export function buildCanvasRunConfig(
  descriptors: CanvasRunConfigDescriptor[],
  dynamicValues: string[]
): Array<[string, string]> {
  return descriptors.map(([key, staticValue], index) => [key, staticValue ?? dynamicValues[index]]);
}

export function buildCanvasSchedulePlan(
  descriptors: Record<TaskScheduleMode, CanvasSchedulePlanDescriptor>,
  context: CanvasSchedulePlanContext
): CanvasSchedulePlan {
  const { activeIntentTaskId, activePartitionKey, scheduleConfigs, selectedCanvasVariantKey } = context;
  const timed = scheduleConfigs["定时运行"];
  const manual = scheduleConfigs["手动运行"];
  const event = scheduleConfigs["数据到达触发"];
  const backfill = scheduleConfigs["一次性回填"];

  return {
    定时运行: {
      ...descriptors["定时运行"],
      title: `${timed.frequency}由 Agent 生成运行参数`,
      trigger: `cron ${timed.cron}`,
      timezone: timed.timezone,
      partition: timed.partition,
      runKey: `${activeIntentTaskId}:${selectedCanvasVariantKey}:schedule:${activePartitionKey}`,
      productState: `${timed.activeWindow} 内按 ${timed.frequency} 触发，${timed.runParams}。`,
      guardrails: resolvePlaceholders(descriptors["定时运行"].guardrails, [timed.missedPolicy]),
      dagsterRows: resolveRowPlaceholders(descriptors["定时运行"].dagsterRows, [`schedule_mode=cron / ${timed.cron}`])
    },
    手动运行: {
      ...descriptors["手动运行"],
      partition: manual.runScope || activePartitionKey,
      runKey: `${activeIntentTaskId}:${selectedCanvasVariantKey}:manual:{operator}:{timestamp}`,
      productState: `由 ${manual.operator} 发起，原因：${manual.reason}。`,
      guardrails: resolvePlaceholders(descriptors["手动运行"].guardrails, [
        manual.overwritePolicy,
        `阶段范围：${manual.stageScope}`,
        manual.approval
      ]),
      dagsterRows: resolveRowPlaceholders(descriptors["手动运行"].dagsterRows, [`trigger=manual / reason=${manual.reason}`])
    },
    数据到达触发: {
      ...descriptors["数据到达触发"],
      trigger: event.eventSources,
      runKey: `${activeIntentTaskId}:${selectedCanvasVariantKey}:sensor:{source_event_id}`,
      productState: `${event.readiness} 后触发，${event.batchPolicy}。`,
      guardrails: resolvePlaceholders(descriptors["数据到达触发"].guardrails, [
        event.readiness,
        `${event.debounceMinutes} 分钟去抖`,
        event.missingPolicy
      ]),
      dagsterRows: resolveRowPlaceholders(descriptors["数据到达触发"].dagsterRows, [event.cursor, event.missingPolicy])
    },
    一次性回填: {
      ...descriptors["一次性回填"],
      trigger: `${backfill.startDate}~${backfill.endDate} + ${backfill.storeScope}`,
      partition: `${backfill.startDate}~${backfill.endDate}|${backfill.storeScope}`,
      runKey: `${activeIntentTaskId}:${selectedCanvasVariantKey}:backfill:{partition}`,
      productState: `${backfill.storeScope} 在 ${backfill.startDate} 至 ${backfill.endDate} 批量补跑，${backfill.recomputeScope}。`,
      guardrails: resolvePlaceholders(descriptors["一次性回填"].guardrails, [
        `并发上限 ${backfill.maxConcurrency} 个分区`,
        backfill.skipSucceeded,
        backfill.recomputeScope
      ]),
      dagsterRows: resolveRowPlaceholders(descriptors["一次性回填"].dagsterRows, [
        backfill.recomputeScope,
        `max_active_runs=${backfill.maxConcurrency}`
      ])
    }
  } as CanvasSchedulePlan;
}

export function buildCanvasDagsterCompatibilityRows(
  descriptors: Record<TaskScheduleMode, CanvasNullableRow[]>,
  context: CanvasSchedulePlanContext
): Record<TaskScheduleMode, string[][]> {
  const { activeIntentTaskId, activePartitionKey, scheduleConfigs, selectedCanvasVariantKey } = context;
  const timed = scheduleConfigs["定时运行"];
  const manual = scheduleConfigs["手动运行"];
  const event = scheduleConfigs["数据到达触发"];
  const backfill = scheduleConfigs["一次性回填"];

  return {
    定时运行: resolveRowPlaceholders(descriptors["定时运行"], [timed.cron, timed.timezone]),
    手动运行: resolveRowPlaceholders(descriptors["手动运行"], [
      `${activeIntentTaskId}:${selectedCanvasVariantKey}:manual:{operator}:{timestamp}`,
      manual.runScope || activePartitionKey
    ]),
    数据到达触发: resolveRowPlaceholders(descriptors["数据到达触发"], [
      event.eventSources,
      String(Math.max(1, Number(event.debounceMinutes) || 1) * 60),
      event.cursor
    ]),
    一次性回填: resolveRowPlaceholders(descriptors["一次性回填"], [
      `${backfill.startDate}~${backfill.endDate}|${backfill.storeScope}`,
      backfill.maxConcurrency,
      backfill.recomputeScope
    ])
  };
}

export function cloneCanvasRows(rows: string[][]): string[][] {
  return rows.map((row) => [...row]);
}
