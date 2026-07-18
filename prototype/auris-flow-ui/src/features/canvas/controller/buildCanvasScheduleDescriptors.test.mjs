import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

import ts from "typescript";

const controllerUrl = new URL("./buildCanvasScheduleDescriptors.ts", import.meta.url);
const executionPlanUrl = new URL("./buildCanvasExecutionPlan.ts", import.meta.url);
const fixtureUrl = new URL("../fixtures/data/canvas-view-fixtures.json", import.meta.url);
const scheduleModelUrl = new URL("./useCanvasScheduleModel.ts", import.meta.url);

async function loadBuilder() {
  const source = await readFile(controllerUrl, "utf8");
  const transpiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022
    },
    fileName: controllerUrl.pathname
  });
  return import(`data:text/javascript;base64,${Buffer.from(transpiled.outputText).toString("base64")}`);
}

const configs = {
  定时运行: {
    frequency: "每 5 分钟",
    cron: "*/5 * * * *",
    timezone: "UTC",
    partition: "tenant × day × five-minutes",
    activeWindow: "08:00-20:00",
    runParams: "读取动态输入",
    missedPolicy: "错过后人工确认"
  },
  手动运行: {
    operator: "alice",
    runScope: "",
    reason: "发布前回归",
    stageScope: "只运行模型链",
    overwritePolicy: "只写新版本不覆盖",
    approval: "需要主管确认"
  },
  数据到达触发: {
    eventSources: "audio_url,authenticated_event",
    readiness: "认证事件 + 单据事件齐备",
    debounceMinutes: "7",
    cursor: "cursor-42",
    missingPolicy: "生成待补数记录",
    batchPolicy: "同一门店窗口合并"
  },
  一次性回填: {
    startDate: "2025-06-01",
    endDate: "2025-06-03",
    storeScope: "华东门店",
    maxConcurrency: "8",
    recomputeScope: "全链路重算",
    skipSucceeded: "只重算失败分区"
  }
};

const context = {
  scheduleConfigs: configs,
  activeIntentTaskId: "task-42",
  selectedCanvasVariantKey: "variant-b",
  activePartitionKey: "2025-06-03|store-7"
};

test("activeRunConfig 描述符保持原有顺序、固定值和每次构建身份", async () => {
  const fixture = JSON.parse(await readFile(fixtureUrl, "utf8"));
  const { buildCanvasRunConfig } = await loadBuilder();
  const dynamicValues = Array.from({ length: fixture.runConfig.length }, (_, index) => `dynamic-${index}`);

  const first = buildCanvasRunConfig(fixture.runConfig, dynamicValues);
  const second = buildCanvasRunConfig(fixture.runConfig, dynamicValues);

  assert.deepEqual(first.map(([key]) => key), [
    "flow_template",
    "canvas_variant",
    "flow_stage",
    "experiment_mode",
    "tenant_id",
    "project_id",
    "scene_profile_id",
    "scene_profile_version_id",
    "scene_profile_snapshot_sha256",
    "task_version",
    "partition_key",
    "schedule_mode",
    "primary_metric_id",
    "metric_window",
    "guardrail_set",
    "asset_selection",
    "run_tags",
    "max_retries",
    "concurrency_limit",
    "human_loop_queue",
    "materialization_mode",
    "execution_job"
  ]);
  assert.equal(first[14][1], "writeback_success_rate + run_failure_rate + review_backlog");
  assert.equal(first[13][1], "dynamic-13");
  assert.equal(first[15][1], "dynamic-15");
  assert.notEqual(first, second);
  first.forEach((row, index) => assert.notEqual(row, second[index]));
});

test("activeSchedulePlan 的静态描述与全部动态覆盖保持迁移前语义", async () => {
  const fixture = JSON.parse(await readFile(fixtureUrl, "utf8"));
  const { buildCanvasSchedulePlan } = await loadBuilder();
  const actual = buildCanvasSchedulePlan(fixture.schedulePlans, context);

  assert.deepEqual(actual, {
    定时运行: {
      title: "每 5 分钟由 Agent 生成运行参数",
      dagsterObject: "定时计划",
      definition: "evidence_dataflow_15m_schedule",
      trigger: "cron */5 * * * *",
      timezone: "UTC",
      partition: "tenant × day × five-minutes",
      runKey: "task-42:variant-b:schedule:2025-06-03|store-7",
      productState: "08:00-20:00 内按 每 5 分钟 触发，读取动态输入。",
      guardrails: ["只处理未消费输入引用", "同一分区 run_key 幂等", "错过后人工确认", "失败 2 次后进入人工复核队列"],
      dagsterRows: [
        ["定时计划", "evidence_dataflow_15m_schedule", "按租户/门店/日期窗口生成运行请求。"],
        ["运行参数生成", "build_run_config_from_assets", "读取输入资源新鲜度、模型版本和 Human Loop 策略。"],
        ["Run Config", "schedule_mode=cron / */5 * * * *", "写入 flow_template、canvas_variant、model_version、experiment_arm 和 partition_key。"],
        ["资产质量检查", "input_ready + output_sink_ready", "校验平台登录、音频 URL、OBS 回写和平台回调配置。"]
      ]
    },
    手动运行: {
      title: "管理员手动发起一次运行",
      dagsterObject: "运行请求",
      definition: "manual_evidence_dataflow_run",
      trigger: "POST /api/v1/task-runs",
      timezone: "Asia/Shanghai",
      partition: "2025-06-03|store-7",
      runKey: "task-42:variant-b:manual:{operator}:{timestamp}",
      productState: "由 alice 发起，原因：发布前回归。",
      guardrails: ["必须选择租户/项目/门店/日期", "只写新版本不覆盖", "阶段范围：只运行模型链", "需要主管确认"],
      dagsterRows: [
        ["运行请求", "manual_evidence_dataflow_run", "BFF 校验权限后调用执行入口。"],
        ["Run Tags", "trigger=manual / reason=发布前回归", "记录 operator_id、flow_version、canvas_variant、model_version 和 reason。"],
        ["Idempotency", "manual:{partition}:{operator}", "短时间重复点击不会创建重复运行。"]
      ]
    },
    数据到达触发: {
      title: "输入引用就绪后自动触发",
      dagsterObject: "事件监听",
      definition: "evidence_input_ready_sensor",
      trigger: "audio_url,authenticated_event",
      timezone: "Asia/Shanghai",
      partition: "source_event_id → tenant/store/date",
      runKey: "task-42:variant-b:sensor:{source_event_id}",
      productState: "认证事件 + 单据事件齐备 后触发，同一门店窗口合并。",
      guardrails: ["认证事件 + 单据事件齐备", "7 分钟去抖", "生成待补数记录"],
      dagsterRows: [
        ["事件监听", "evidence_input_ready_sensor", "监听输入资产生成事件并生成运行请求。"],
        ["Cursor", "cursor-42", "保证增量消费，不重复拉同一批音频 URL。"],
        ["SkipReason", "生成待补数记录", "输入不完整时不启动 job，只记录可解释状态。"]
      ]
    },
    一次性回填: {
      title: "按日期/门店批量补跑",
      dagsterObject: "回填策略",
      definition: "evidence_dataflow_backfill",
      trigger: "2025-06-01~2025-06-03 + 华东门店",
      timezone: "Asia/Shanghai",
      partition: "2025-06-01~2025-06-03|华东门店",
      runKey: "task-42:variant-b:backfill:{partition}",
      productState: "华东门店 在 2025-06-01 至 2025-06-03 批量补跑，全链路重算。",
      guardrails: ["并发上限 8 个分区", "只重算失败分区", "全链路重算", "OBS URL 和平台回调使用独立幂等键"],
      dagsterRows: [
        ["BackfillPolicy", "multi_partition_backfill", "按 tenant/store/date 批量发起运行。"],
        ["Recompute", "全链路重算", "默认复用已有转写，可只重算标签和回写资产。"],
        ["Concurrency", "max_active_runs=8", "避免 OBS 上传和平台回调打满下游服务。"]
      ]
    }
  });
});

test("activeSchedulePlan 每次构建都克隆顶层、guardrail、二维行和行元素", async () => {
  const fixture = JSON.parse(await readFile(fixtureUrl, "utf8"));
  const { buildCanvasSchedulePlan } = await loadBuilder();
  const first = buildCanvasSchedulePlan(fixture.schedulePlans, context);
  const second = buildCanvasSchedulePlan(fixture.schedulePlans, context);

  assert.notEqual(first, second);
  for (const mode of ["定时运行", "手动运行", "数据到达触发", "一次性回填"]) {
    assert.notEqual(first[mode], second[mode]);
    assert.notEqual(first[mode].guardrails, second[mode].guardrails);
    assert.notEqual(first[mode].dagsterRows, second[mode].dagsterRows);
    first[mode].dagsterRows.forEach((row, index) => assert.notEqual(row, second[mode].dagsterRows[index]));
  }
});

test("Dagster 兼容行严格覆盖动态值并保持二维数组身份", async () => {
  const fixture = JSON.parse(await readFile(fixtureUrl, "utf8"));
  const { buildCanvasDagsterCompatibilityRows } = await loadBuilder();
  const first = buildCanvasDagsterCompatibilityRows(fixture.dagsterCompatibilityRows, context);
  const second = buildCanvasDagsterCompatibilityRows(fixture.dagsterCompatibilityRows, context);

  assert.deepEqual(first, {
    定时运行: [
      ["调度对象", "定时计划"],
      ["cron_schedule", "*/5 * * * *"],
      ["execution_timezone", "UTC"],
      ["run_config_fn", "build_run_config_from_assets(context.scheduled_execution_time)"],
      ["tags", "trigger=schedule, schedule_mode=cron, canvas_variant"]
    ],
    手动运行: [
      ["调度对象", "手动运行请求"],
      ["run_key", "task-42:variant-b:manual:{operator}:{timestamp}"],
      ["partition_key", "2025-06-03|store-7"],
      ["run_config", "flow_template, canvas_variant, selected_stage, overwrite_policy"],
      ["tags", "trigger=manual, operator_id, reason, audit_id"]
    ],
    数据到达触发: [
      ["调度对象", "数据到达监听"],
      ["monitored_assets", "audio_url,authenticated_event"],
      ["minimum_interval_seconds", "420"],
      ["cursor", "cursor-42"],
      ["yield", "运行请求 | 跳过原因"]
    ],
    一次性回填: [
      ["调度对象", "分区回填"],
      ["partition_set", "2025-06-01~2025-06-03|华东门店"],
      ["max_concurrent_runs", "8"],
      ["run_config", "全链路重算"],
      ["tags", "trigger=backfill, backfill_id, skip_succeeded"]
    ]
  });
  assert.notEqual(first, second);
  for (const mode of ["定时运行", "手动运行", "数据到达触发", "一次性回填"]) {
    assert.notEqual(first[mode], second[mode]);
    first[mode].forEach((row, index) => assert.notEqual(row, second[mode][index]));
  }
});

test("输出接收器从 JSON 克隆，不泄漏 fixture 行引用", async () => {
  const fixture = JSON.parse(await readFile(fixtureUrl, "utf8"));
  const { cloneCanvasRows } = await loadBuilder();
  const first = cloneCanvasRows(fixture.scheduleOutputSinks);
  const second = cloneCanvasRows(fixture.scheduleOutputSinks);

  assert.deepEqual(first, [
    ["processed_wav_asset", "object-storage://processed-audio/{tenant}/{task_run}/", "按当前 MinIO / OBS / OSS Provider 上传处理后的 WAV，生成支持 HTTP Range 的 processed_wav_url。"],
    ["platform_audio_callback", "POST /api/v1/output-sinks/platform-callbacks", "把 wav_url、标签、证据包和 run_id 回写业务平台。"],
    ["review_queue_asset", "auris/task/evidence-dataflow/review_queue", "低置信或冲突样本进入人工复核。"],
    ["export_manifest", "object-storage://exports/{partition}/manifest.json", "按当前对象存储 Provider 登记 CSV/Parquet/API 导出批次。"]
  ]);
  assert.notEqual(first, second);
  first.forEach((row, index) => assert.notEqual(row, second[index]));
});

test("buildCanvasExecutionPlan 不再内联 Dagster 兼容表和输出接收器", async () => {
  const source = await readFile(executionPlanUrl, "utf8");
  assert.match(source, /buildCanvasDagsterCompatibilityRows\(/);
  assert.match(source, /cloneCanvasRows\(/);
  assert.doesNotMatch(source, /\["调度对象", "定时计划"\]/);
  assert.doesNotMatch(source, /\["processed_wav_asset", "object-storage:/);
});

test("useCanvasScheduleModel 只消费 fixture 描述符，不再内联静态计划", async () => {
  const source = await readFile(scheduleModelUrl, "utf8");
  assert.match(source, /buildCanvasRunConfig\(/);
  assert.match(source, /buildCanvasSchedulePlan\(/);
  assert.doesNotMatch(source, /\["flow_template", selectedTaskType\.key\]/);
  assert.doesNotMatch(source, /const activeSchedulePlan = \{\s*定时运行:/);
});
