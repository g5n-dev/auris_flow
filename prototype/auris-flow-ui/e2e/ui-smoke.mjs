import { chromium } from "playwright";
import { createServer as createHttpServer } from "node:http";
import { createServer } from "vite";

const root = new URL("../", import.meta.url).pathname;
const configuredPort = process.env.AURIS_UI_SMOKE_PORT;
const port = configuredPort ? Number(configuredPort) : 0;

const moduleChecks = [
  { nav: "首页", title: "运营首页", tabs: ["总览", "待处理", "异常提醒", "最近资产"] },
  { nav: "租户", title: "租户管理", tabs: ["概览", "项目", "成员", "ASR 接入", "资源配额", "审计日志"] },
  { nav: "项目", title: "项目管理", tabs: ["项目概览", "数据源", "成员", "标签体系", "质量目标"] },
  { nav: "任务", title: "任务配置", tabs: ["流程配置", "流程模板", "编排版本", "输入输出", "触发与调度", "AB实验", "运行记录", "版本发布"] },
  { nav: "数据", title: "数据管理", tabs: ["音频数据", "人物/声纹", "事件", "关联视图"] },
  { nav: "知识库", title: "知识库", tabs: ["知识总览", "知识连接器", "索引构建", "知识可视化", "质量管理", "效果展示", "运行记录"] },
  { nav: "调听", title: "调听工作台" },
  { nav: "标签", title: "标签生产治理台", tabs: ["标签体系", "智能抽取", "规则/Prompt", "评测人审", "版本发布"] },
  { nav: "洞察", title: "业务洞察", tabs: ["业务大盘", "门店归因", "销售绩效", "标签资产", "模型质量", "报告中心"] },
  { nav: "评测", title: "评测中心", tabs: ["自动化评测", "打标评测", "Prompt优化", "人工评测", "评测集", "模型对比", "badcase"] },
  { nav: "资产", title: "数据资产", tabs: ["资产目录", "资产详情", "资产血缘", "数据回填", "资产质量", "资产任务"] },
  { nav: "设置", title: "设置", tabs: ["模型配置", "工具配置", "阈值配置", "权限配置", "存储配置", "通知配置"] }
];

const exactText = (text) => new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`);

function assert(condition, message, detail = undefined) {
  if (!condition) {
    const error = new Error(message);
    if (detail !== undefined) error.detail = detail;
    throw error;
  }
}

async function clickExactButton(scope, text, timeout = 6000) {
  const button = scope.locator("button").filter({ hasText: exactText(text) }).first();
  await button.waitFor({ state: "visible", timeout });
  await button.click();
}

async function clickButtonContaining(scope, text, timeout = 6000) {
  const button = scope.locator("button").filter({ hasText: text }).first();
  await button.waitFor({ state: "visible", timeout });
  await button.click();
}

async function expectBodyText(page, text, timeout = 8000) {
  try {
    await page.locator("body").filter({ hasText: text }).waitFor({ state: "visible", timeout });
  } catch (error) {
    const bodyText = await page.locator("body").innerText().catch(() => "");
    error.detail = {
      expectedText: text,
      bodyText: bodyText.slice(0, 1600)
    };
    throw error;
  }
}

async function expectMetricText(page, text, timeout = 8000) {
  await page.locator(".module-metrics").first().filter({ hasText: text }).waitFor({ state: "visible", timeout });
}

async function assertLazyBranchesHealthy(page, context, timeout = 10000) {
  const deadline = Date.now() + timeout;
  while (true) {
    const errors = await page.locator('.feature-module-error:visible, [data-testid$="-module-load-error"]:visible').allTextContents();
    assert(errors.length === 0, `${context} 出现 lazy 错误边界`, { errors });
    const fallbacks = await page.locator('.feature-module-loading:visible, [data-testid$="-loading"]:visible').allTextContents();
    if (fallbacks.length === 0) return;
    assert(Date.now() < deadline, `${context} 的 lazy fallback 未在时限内收敛`, { fallbacks });
    await page.waitForTimeout(50);
  }
}

async function openModule(page, label, title) {
  const nav = page.locator(`button[aria-label="导航：${label}"]`).first();
  await nav.waitFor({ state: "visible", timeout: 8000 });
  await nav.click();
  await expectBodyText(page, title);
  if (label === "知识库") {
    await page.getByTestId("knowledge-module-root").waitFor({ state: "visible", timeout: 10000 });
  }
  if (label !== "调听") {
    await expectBodyText(page, "顶部指标投影已同步");
  }
  await assertLazyBranchesHealthy(page, `模块 ${label}`);
}

async function openTab(page, label) {
  const tab = page.locator(".module-tabs button").filter({ hasText: exactText(label) }).first();
  await tab.waitFor({ state: "visible", timeout: 6000 });
  await tab.click();
  await expectBodyText(page, label);
  const selected = await tab.getAttribute("aria-selected");
  assert(selected === "true", `tab ${label} clicked but did not become active`, { selected });
  await expectBodyText(page, label);
  await assertLazyBranchesHealthy(page, `Tab ${label}`);
}

async function responseData(response, message) {
  const json = await response.json().catch(() => ({}));
  assert(json?.data && typeof json.data === "object", message, json);
  return json.data;
}

async function completePromptCandidateGate(page, candidateId, previousCandidateId = "") {
  const binding = page.getByTestId("prompt-review-binding");
  await binding.filter({ hasText: candidateId }).waitFor({ state: "visible", timeout: 8000 });
  await binding.filter({ hasText: "awaiting-review" }).waitFor({ state: "visible", timeout: 8000 });
  if (previousCandidateId) {
    assert(
      await binding.filter({ hasText: previousCandidateId }).count() === 0,
      "新 OptimizationRun 仍展示上一轮 PromptVersionCandidate",
      { candidateId, previousCandidateId, binding: await binding.innerText() }
    );
  }
  assert(
    await page.getByRole("button", { name: "锁定并运行评测", exact: true }).isDisabled(),
    "新 PromptVersionCandidate 尚未复核时错误继承旧审批"
  );

  const reviewResponsePromise = page.waitForResponse(
    (response) => response.url().endsWith(`/api/v1/prompt-version-candidates/${candidateId}/review-submissions`) && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByTestId("prompt-double-blind-review").click();
  const reviewResponse = await reviewResponsePromise;
  assert(reviewResponse.status() === 201, `Prompt 候选 ${candidateId} 未提交双盲复核`);
  const reviewReceipt = await responseData(reviewResponse, `Prompt 候选 ${candidateId} 双盲复核缺少回执`);
  assert(
    reviewReceipt.status === "awaiting-adjudication" && reviewReceipt.received_reviews === 2,
    `Prompt 候选 ${candidateId} 未完成两份密封复核并进入裁决`,
    reviewReceipt
  );
  await binding.filter({ hasText: "awaiting-adjudication" }).waitFor({ state: "visible", timeout: 8000 });

  const adjudicationResponsePromise = page.waitForResponse(
    (response) => response.url().endsWith(`/api/v1/prompt-version-candidates/${candidateId}/adjudications`) && response.request().method() === "POST",
    { timeout: 10000 }
  );
  const adjudicationButton = page.getByRole("button", { name: "独立仲裁 Prompt", exact: true });
  await adjudicationButton.waitFor({ state: "visible", timeout: 8000 });
  await adjudicationButton.click();
  const adjudicationResponse = await adjudicationResponsePromise;
  assert(adjudicationResponse.status() === 201, `Prompt 候选 ${candidateId} 未提交独立裁决`);
  const adjudicationReceipt = await responseData(adjudicationResponse, `Prompt 候选 ${candidateId} 裁决缺少回执`);
  assert(adjudicationReceipt.status === "approved", `Prompt 候选 ${candidateId} 裁决后未批准`, adjudicationReceipt);
  await binding.filter({ hasText: "approved" }).waitFor({ state: "visible", timeout: 8000 });
  assert(
    await page.getByRole("button", { name: "锁定并运行评测", exact: true }).isEnabled(),
    `Prompt 候选 ${candidateId} 完成双复核/裁决后仍不可评测`
  );
}

async function assertListeningTranscriptLayout(page) {
  const backendListeningHead = page.locator('[data-listening-source="bff"]');
  await backendListeningHead.waitFor({ state: "visible", timeout: 10000 });
  assert(Boolean(await backendListeningHead.getAttribute("data-listening-task-id")), "调听页未绑定 BFF HumanReviewTask 强 ID");
  await backendListeningHead.getByText("S20250526-000128", { exact: true }).waitFor({ state: "visible", timeout: 5000 });
  await clickButtonContaining(page.locator(".listening-mode-switch"), "完整调听");
  const chat = page.locator(".simple-chat-scroll").first();
  await chat.waitFor({ state: "visible", timeout: 8000 });
  const layout = await chat.evaluate((element) => {
    const chatRect = element.getBoundingClientRect();
    const turns = [...element.querySelectorAll(".simple-turn")].map((turn) => {
      const rect = turn.getBoundingClientRect();
      const probeX = Math.min(window.innerWidth - 1, rect.left + Math.max(12, rect.width * 0.55));
      const probeY = rect.top + Math.max(12, Math.min(rect.height - 1, rect.height * 0.5));
      const visible =
        probeX >= 0 &&
        probeX < window.innerWidth &&
        probeY >= Math.max(0, chatRect.top) &&
        probeY < Math.min(window.innerHeight, chatRect.bottom);
      const probe = document.elementFromPoint(
        probeX,
        Math.min(window.innerHeight - 1, Math.max(0, probeY))
      );
      return {
        width: rect.width,
        height: rect.height,
        visible,
        covered: visible && !probe?.closest(".simple-turn")?.isSameNode(turn)
      };
    });
    return {
      width: chatRect.width,
      height: chatRect.height,
      turns
    };
  });
  assert(layout.width > 320 && layout.height > 240, "完整调听主区尺寸异常", layout);
  assert(layout.turns.length >= 5, "完整调听未渲染完整对话", layout);
  assert(layout.turns.filter((turn) => turn.visible).length >= 3, "完整调听首屏有效对话不足", layout);
  assert(
    layout.turns.every((turn) => turn.width > 240 && turn.height > 48 && !turn.covered),
    "完整调听对话被空白图层覆盖",
    layout
  );

  await clickButtonContaining(page.locator(".listening-mode-switch"), "审音矩阵");
  const matrix = page.locator(".matrix-page");
  await matrix.waitFor({ state: "visible", timeout: 8000 });
  await clickExactButton(matrix.locator(".matrix-summary"), "串音候选 2");
  await matrix.locator(".matrix-context-bar").filter({ hasText: "串音候选" }).waitFor({ state: "visible", timeout: 6000 });
  await matrix.locator(".matrix-context-bar").filter({ hasText: "已筛选 串音候选" }).waitFor({ state: "visible", timeout: 6000 });
  await assertLazyBranchesHealthy(page, "调听审音矩阵状态筛选");
}

async function runModuleCommandSmoke(page) {
  await openModule(page, "知识库", "知识库");

  const quickActions = page.locator(".module-head .quick-actions").first();
  await clickExactButton(quickActions, "搜索");
  const idleFoot = page.locator(".module-command-foot").first();
  await clickExactButton(idleFoot, "记录链路");
  await expectBodyText(page, "尚无可记录动作");
  await page.locator(".module-command-panel input").fill("SOP");
  await clickButtonContaining(page.locator(".module-command-results"), "销售话术 SOP");
  await expectBodyText(page, "已定位");

  await page.getByLabel("关闭模块操作面板").click();
  await clickExactButton(quickActions, "筛选");
  await clickButtonContaining(page.locator(".module-command-panel"), "质量风险");
  await expectBodyText(page, "已应用筛选");

  await page.getByLabel("关闭模块操作面板").click();
  const exportResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/exports") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await clickExactButton(quickActions, "导出");
  const exportResponse = await exportResponsePromise;
  const exportJson = await exportResponse.json().catch(() => ({}));
  assert(exportResponse.status() === 202, "global export expected 202", exportJson);
  assert(exportJson?.data?.run_id || exportJson?.data?.id, "global export missing backend run id", exportJson);
  assert(exportJson?.meta?.trace_id, "global export missing trace id", exportJson);
  await expectBodyText(page, "已创建导出运行", 10000);
  await expectBodyText(page, exportJson.data.run_id || exportJson.data.id);
  await expectBodyText(page, exportJson.meta.trace_id.slice(0, 12));
  const exportFoot = page.locator(".module-command-foot").first();
  await clickExactButton(exportFoot, "固定回执");
  await expectBodyText(page, "当前操作回执已固定");
}

async function runLabelGovernanceTruthSmoke(page) {
  await openModule(page, "标签", "标签生产治理台");

  for (const tab of ["标签体系", "智能抽取", "规则/Prompt", "评测人审", "版本发布"]) {
    await openTab(page, tab);
    const runRail = page.getByTestId("label-optimization-run-rail");
    await runRail.waitFor({ state: "visible", timeout: 8000 });
    await runRail.getByText("下一动作", { exact: true }).waitFor({ state: "visible", timeout: 4000 });
    assert(await runRail.getAttribute("aria-label") === "标签优化运行轨道", `${tab} 缺少可访问的统一优化运行轨道`);
  }

  await openTab(page, "智能抽取");
  await page.getByTestId("label-fact-state-idle").waitFor({ state: "visible", timeout: 6000 });
  assert(await page.locator('[data-demo-mode="false"]').count() > 0, "UI smoke 必须覆盖默认关闭 demo_mode 的后端事实模式");
  assert(await page.getByTestId("label-candidate-LC-quote-01").count() === 0, "未运行抽取时静态候选泄漏到联调模式");
  assert(await page.getByRole("button", { name: "运行抽取", exact: true }).isDisabled(), "LabelVersion 强 ID 未保存时仍允许运行抽取");
  const initialLabelVersionResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/label-versions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.locator(".label-v2-hero-actions").getByRole("button", { name: "保存", exact: true }).click();
  const initialLabelVersionReceipt = await initialLabelVersionResponse;
  assert(initialLabelVersionReceipt.status() === 201, "抽取前未保存 LabelVersion 强 ID");
  const initialLabelVersion = await responseData(initialLabelVersionReceipt, "初始 LabelVersion 回执缺少强 ID");
  assert(await page.getByRole("button", { name: "运行抽取", exact: true }).isDisabled(), "缺少绑定 PromptVersion 时错误开放抽取");
  await openTab(page, "规则/Prompt");
  const initialPromptVersionResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/prompt-versions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByRole("button", { name: "保存 PromptVersion 草稿", exact: true }).click();
  const initialPromptVersionReceipt = await initialPromptVersionResponse;
  assert(initialPromptVersionReceipt.status() === 201, "抽取前未保存绑定 LabelVersion 的 PromptVersion 草稿");
  const initialPromptVersion = await responseData(initialPromptVersionReceipt, "初始 PromptVersion 回执缺少强 ID");
  assert(
    initialPromptVersion.label_version_id === initialLabelVersion.label_version_id,
    "初始 PromptVersion 未绑定本轮 LabelVersion",
    { initialLabelVersion, initialPromptVersion }
  );
  await openTab(page, "智能抽取");
  assert(await page.getByRole("button", { name: "运行抽取", exact: true }).isEnabled(), "LabelVersion/PromptVersion 保存后抽取仍不可用");
  const extractionResponsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/label-extraction-runs") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  const observationResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/label-observations?") && response.request().method() === "GET",
    { timeout: 10000 }
  );
  const aggregateResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/label-aggregates?") && response.request().method() === "GET",
    { timeout: 10000 }
  );
  const aggregationRunResponsePromise = page.waitForResponse(
    (response) => response.url().endsWith(`/api/v1/label-aggregation-runs/${labelAggregationRunId}`) && response.request().method() === "GET",
    { timeout: 10000 }
  );
  await page.getByRole("button", { name: "运行抽取", exact: true }).click();
  assert((await extractionResponsePromise).status() === 202, "LabelExtractionRun 未创建");
  assert((await observationResponsePromise).status() === 200, "抽取完成后未读取 LabelObservation");
  assert((await aggregationRunResponsePromise).status() === 200, "抽取完成后未优先读取后端已物化的 LabelAggregationRun");
  assert((await aggregateResponsePromise).status() === 200, "抽取完成后未读取 LabelAggregate");
  await expectBodyText(page, "3 条 LabelCandidate", 8000);
  assert(await page.getByTestId("label-candidate-LC-stale-same-subject").count() === 0, "同 subject 的旧 AggregationRun Aggregate 串入当前候选");
  const runRailAfterExtraction = page.getByTestId("label-optimization-run-rail");
  assert((await runRailAfterExtraction.locator("ol > li").allTextContents()).map((text) => text.match(/抽取|聚合|人审|评测|发布|监控/)?.[0]).filter(Boolean).join("→") === "抽取→聚合→人审→评测→发布→监控", "标签优化运行轨道阶段不完整");
  assert((await runRailAfterExtraction.getAttribute("data-trace-id")) === initialLabelVersion.trace_id, "运行轨道未沿用 LabelVersion root trace");

  await openTab(page, "标签体系");
  const taxonomyReviewResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/label-taxonomy-suggestions/taxonomy-ui-1/review-submissions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByTestId("taxonomy-review-taxonomy-ui-1").click();
  assert((await taxonomyReviewResponse).status() === 201, "Taxonomy 建议未通过专用密封审核接口");
  await expectBodyText(page, "Taxonomy 密封审核已提交", 8000);
  await openTab(page, "智能抽取");
  await page.getByTestId("label-candidate-LC-quote-high").click();
  const highRiskReviewResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/label-aggregates/LC-quote-high/review-submissions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByRole("button", { name: "接受当前", exact: true }).click();
  assert((await highRiskReviewResponse).status() === 201, "高风险 Aggregate 未通过专用双盲接口提交");
  await expectBodyText(page, "密封结论已提交", 8000);
  const firstCandidate = page.getByTestId("label-candidate-LC-quote-01");
  const secondCandidate = page.getByTestId("label-candidate-LC-quote-02");
  await page.getByLabel("选择候选 LC-quote-01").check();
  await page.getByLabel("选择候选 LC-quote-02").check();
  await page.getByTestId("label-batch-preflight").filter({ hasText: "前端预检通过" }).waitFor({ state: "visible", timeout: 6000 });
  const batchDecisionResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/human-review-decision-batches") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByTestId("label-batch-accept").click();
  assert((await batchDecisionResponse).status() === 200, "候选批量裁决未提交服务端");
  const batchReceipt = page.getByTestId("label-batch-receipt");
  await batchReceipt.filter({ hasText: "hrb-ui-partial-1 · partial" }).waitFor({ state: "visible", timeout: 8000 });
  await batchReceipt.filter({ hasText: "success 1 / skipped 1 / failed 0" }).waitFor({ state: "visible", timeout: 8000 });

  await firstCandidate.click();
  await page.getByTestId("label-active-review-state").filter({ hasText: "已接受" }).waitFor({ state: "visible", timeout: 8000 });
  await secondCandidate.click();
  await page.getByTestId("label-active-review-state").filter({ hasText: "待人工" }).waitFor({ state: "visible", timeout: 6000 });
  await firstCandidate.click();
  await page.getByTestId("label-active-review-state").filter({ hasText: "已接受" }).waitFor({ state: "visible", timeout: 6000 });

  await openTab(page, "评测人审");
  const reviewWorkspace = page.getByTestId("label-review-workspace");
  await page.getByTestId("label-review-task-review-LC-quote-02").click();
  await reviewWorkspace.focus();
  await page.keyboard.press("a");
  const acceptDraftButton = reviewWorkspace.getByRole("button", { name: "接受候选", exact: true });
  assert((await acceptDraftButton.getAttribute("class"))?.includes("active"), "A 快捷键未选择接受决策");
  const reviewNote = reviewWorkspace.locator("textarea").last();
  await reviewNote.focus();
  await page.keyboard.press("r");
  assert((await acceptDraftButton.getAttribute("class"))?.includes("active"), "输入框聚焦时错误截获 R 快捷键");
  const secondDecisionResponse = page.waitForResponse(
    (response) => response.url().includes("/api/v1/human-review-tasks/") && response.url().endsWith("/decisions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByTestId("label-review-save-next").click();
  assert((await secondDecisionResponse).status() === 201, "保存并下一条未写回当前强绑定任务");
  await page.getByTestId("label-review-task-review-LC-quote-02").filter({ hasText: "已接受" }).waitFor({ state: "visible", timeout: 8000 });

  await openTab(page, "智能抽取");

  const saveCandidateResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/label-versions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.locator(".label-v2-hero-actions").getByRole("button", { name: "保存", exact: true }).click();
  const firstLabelVersionReceipt = await saveCandidateResponse;
  assert(firstLabelVersionReceipt.status() === 201, "标签候选版本未保存");
  const firstLabelVersion = await responseData(firstLabelVersionReceipt, "首轮候选 LabelVersion 回执缺少强 ID");
  await openTab(page, "规则/Prompt");
  const evaluationPromptVersionResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/prompt-versions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByRole("button", { name: "保存 PromptVersion 草稿", exact: true }).click();
  const firstPromptVersionReceipt = await evaluationPromptVersionResponse;
  assert(firstPromptVersionReceipt.status() === 201, "候选评测前未绑定新的 PromptVersion 草稿");
  const firstPromptVersion = await responseData(firstPromptVersionReceipt, "首轮 PromptVersion 回执缺少强 ID");
  assert(
    firstPromptVersion.label_version_id === firstLabelVersion.label_version_id,
    "首轮 PromptVersion 未绑定首轮 LabelVersion",
    { firstLabelVersion, firstPromptVersion }
  );
  await page.getByTestId("prompt-review-binding").filter({ hasText: "尚无 PromptVersionCandidate" }).waitFor({ state: "visible", timeout: 8000 });
  assert(await page.getByRole("button", { name: "锁定并运行评测", exact: true }).isDisabled(), "首轮 OptimizationRun 前错误继承旧审批");
  const optimizationResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/label-optimization-runs") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.locator(".label-v2-hero-actions").getByRole("button", { name: "启动智能创建", exact: true }).click();
  const firstOptimizationReceipt = await optimizationResponse;
  assert(firstOptimizationReceipt.status() === 202, "标签优化运行未创建或候选未物化");
  const firstOptimizationRun = await responseData(firstOptimizationReceipt, "首轮 OptimizationRun 回执缺少强 ID");
  const firstPromptCandidateId = firstOptimizationRun.prompt_candidate_ids?.[0];
  assert(
    firstOptimizationRun.label_version_id === firstLabelVersion.label_version_id &&
      firstOptimizationRun.prompt_version_id === firstPromptVersion.prompt_version_id &&
      typeof firstPromptCandidateId === "string" && Boolean(firstPromptCandidateId),
    "首轮 OptimizationRun 未锁定本轮 LabelVersion/PromptVersion/Candidate",
    { firstLabelVersion, firstPromptVersion, firstOptimizationRun }
  );
  await expectBodyText(page, "场景 Agent已完成", 8000);
  await openTab(page, "规则/Prompt");
  await completePromptCandidateGate(page, firstPromptCandidateId);
  await openTab(page, "评测人审");
  const evalResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/eval-runs") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  const firstEvalButton = page.locator("button.label-v2-wide-action");
  await firstEvalButton.click();
  const firstEvalReceipt = await evalResponse;
  const firstEvalJson = await firstEvalReceipt.json().catch(() => ({}));
  assert(firstEvalReceipt.status() === 202, `标签候选版本未创建锁定 EvalRun：${JSON.stringify(firstEvalJson)}`);
  await page.locator('[data-label-eval-status="pending"]').waitFor({ state: "visible", timeout: 5000 });
  assert(await firstEvalButton.isDisabled(), "EvalRun pending 时仍允许重复提交");
  await page.locator('[data-label-eval-status="success"][data-label-eval-backend-status="success"]').waitFor({ state: "visible", timeout: 10000 });
  await expectBodyText(page, "label-eval-ui-1", 8000);

  await openTab(page, "版本发布");
  await page.locator('label:has-text("回滚部署 ID") input').last().fill("release-stable-ui");
  const firstPublishResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/release-deployments") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByTestId("label-publish-candidate").click();
  assert((await firstPublishResponse).status() === 201, "ReleaseDeployment Bundle 未创建");
  await page.locator('[data-label-publish-status="blocked"]').waitFor({ state: "visible", timeout: 10000 });
  await expectBodyText(page, "RELEASE_FACTS_CHANGED");
  assert(await page.getByTestId("label-publish-candidate").isDisabled(), "blocked 后发布按钮仍可点击");

  const resetCandidateResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/label-versions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.locator(".label-v2-hero-actions").getByRole("button", { name: "保存", exact: true }).click();
  const secondLabelVersionReceipt = await resetCandidateResponse;
  assert(secondLabelVersionReceipt.status() === 201, "阻断修复后候选版本未重新保存");
  const secondLabelVersion = await responseData(secondLabelVersionReceipt, "第二轮 LabelVersion 回执缺少强 ID");
  assert(secondLabelVersion.label_version_id !== firstLabelVersion.label_version_id, "第二轮错误复用首轮 LabelVersion 强 ID");
  await openTab(page, "规则/Prompt");
  const secondPromptVersionResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/prompt-versions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByRole("button", { name: "保存 PromptVersion 草稿", exact: true }).click();
  const secondPromptVersionReceipt = await secondPromptVersionResponse;
  assert(secondPromptVersionReceipt.status() === 201, "新 LabelVersion 未绑定新的 PromptVersion 草稿");
  const secondPromptVersion = await responseData(secondPromptVersionReceipt, "第二轮 PromptVersion 回执缺少强 ID");
  assert(
    secondPromptVersion.prompt_version_id !== firstPromptVersion.prompt_version_id &&
      secondPromptVersion.label_version_id === secondLabelVersion.label_version_id,
    "第二轮 PromptVersion 未唯一绑定第二轮 LabelVersion",
    { firstPromptVersion, secondPromptVersion, secondLabelVersion }
  );
  await page.getByTestId("prompt-review-binding").filter({ hasText: "尚无 PromptVersionCandidate" }).waitFor({ state: "visible", timeout: 8000 });
  assert(await page.getByRole("button", { name: "锁定并运行评测", exact: true }).isDisabled(), "第二轮 OptimizationRun 前错误继承首轮审批");
  const secondOptimizationResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/label-optimization-runs") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.locator(".label-v2-hero-actions").getByRole("button", { name: "启动智能创建", exact: true }).click();
  const secondOptimizationReceipt = await secondOptimizationResponse;
  assert(secondOptimizationReceipt.status() === 202, "新 LabelVersion 未重新物化 Prompt 候选");
  const secondOptimizationRun = await responseData(secondOptimizationReceipt, "第二轮 OptimizationRun 回执缺少强 ID");
  const secondPromptCandidateId = secondOptimizationRun.prompt_candidate_ids?.[0];
  assert(
    secondOptimizationRun.id !== firstOptimizationRun.id &&
      secondOptimizationRun.label_version_id === secondLabelVersion.label_version_id &&
      secondOptimizationRun.prompt_version_id === secondPromptVersion.prompt_version_id &&
      typeof secondPromptCandidateId === "string" &&
      secondPromptCandidateId !== firstPromptCandidateId,
    "第二轮 OptimizationRun/Candidate 未唯一绑定第二轮强版本",
    { firstOptimizationRun, secondOptimizationRun }
  );
  await expectBodyText(page, "场景 Agent已完成", 8000);
  await openTab(page, "规则/Prompt");
  await completePromptCandidateGate(page, secondPromptCandidateId, firstPromptCandidateId);
  await openTab(page, "评测人审");
  const secondEvalResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/eval-runs") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.locator("button.label-v2-wide-action").click();
  assert((await secondEvalResponse).status() === 202, "阻断修复后 EvalRun 未重建");
  await page.locator('[data-label-eval-status="failed"][data-label-eval-backend-status="failed"]').waitFor({ state: "visible", timeout: 10000 });
  const evalRetryResponse = page.waitForResponse(
    (response) => response.url().includes("/api/v1/runs/label-eval-ui-2/retries") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.locator('[data-label-eval-retry="true"]').click();
  assert((await evalRetryResponse).status() === 202, "失败 EvalRun 未按原意图调用 retry");
  await page.locator('[data-label-eval-status="success"][data-label-eval-backend-status="success"]').waitFor({ state: "visible", timeout: 10000 });
  await expectBodyText(page, "label-eval-ui-2-retry-1", 8000);
  await openTab(page, "版本发布");
  const secondPublishResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/release-deployments") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByTestId("label-publish-candidate").click();
  const secondPublishReceipt = await secondPublishResponse;
  assert(secondPublishReceipt.status() === 201, "第二个 ReleaseDeployment 未进入 shadow");
  const secondDeployment = (await secondPublishReceipt.json()).data;
  await page.locator('[data-label-backend-status="shadowing"]').waitFor({ state: "visible", timeout: 8000 });
  const hardRollbackGrayResponse = page.waitForResponse(
    (response) => response.url().includes("/api/v1/release-deployments/") && response.url().endsWith("/transitions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByRole("button", { name: "灰度发布", exact: true }).last().click();
  assert((await hardRollbackGrayResponse).status() === 200, "自动回滚场景未进入人工 10% gray");
  await page.locator('[data-label-backend-status="gray-releasing"]').waitFor({ state: "visible", timeout: 8000 });
  const hardMonitorResponse = await page.request.post(
    new URL(`/api/v1/release-deployments/${secondDeployment.deployment_id}/monitor-samples`, page.url()).toString(),
    {
      headers: {
        Authorization: `Bearer ${smokeSessionToken}`,
        "Idempotency-Key": "ui-smoke-hard-monitor",
        "X-Auris-System-Worker": "ui-smoke-monitor"
      },
      data: {
        sample_id: "monitor-hard-regression-ui",
        expected_status: "gray-releasing",
        window_started_at: "2026-07-15T01:00:00Z",
        window_ended_at: "2026-07-15T01:05:00Z",
        sample_size: 500,
        stable_window_complete: false,
        metrics: {
          json_valid_rate: 0.98,
          conflict_rate: 0.02,
          critical_recall_delta_pp: -0.2,
          human_override_delta_pp: 0.4,
          cost_ratio: 1.02,
          latency_ratio: 1.04
        }
      }
    }
  );
  assert(hardMonitorResponse.status() === 200, "system 硬退化监控样本未写入");
  await page.locator('[data-label-publish-refresh="true"]').click();
  await page.locator('[data-label-publish-status="failed"][data-label-backend-status="rolled-back"]').waitFor({ state: "visible", timeout: 10000 });
  await expectBodyText(page, "在线保护已自动回滚", 8000);

  const postRollbackCandidateResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/label-versions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.locator(".label-v2-hero-actions").getByRole("button", { name: "保存", exact: true }).click();
  const thirdLabelVersionReceipt = await postRollbackCandidateResponse;
  assert(thirdLabelVersionReceipt.status() === 201, "自动回滚回流后候选版本未重新保存");
  const thirdLabelVersion = await responseData(thirdLabelVersionReceipt, "第三轮 LabelVersion 回执缺少强 ID");
  assert(
    ![firstLabelVersion.label_version_id, secondLabelVersion.label_version_id].includes(thirdLabelVersion.label_version_id),
    "第三轮错误复用历史 LabelVersion 强 ID",
    { firstLabelVersion, secondLabelVersion, thirdLabelVersion }
  );
  await openTab(page, "规则/Prompt");
  const thirdPromptVersionResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/prompt-versions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByRole("button", { name: "保存 PromptVersion 草稿", exact: true }).click();
  const thirdPromptVersionReceipt = await thirdPromptVersionResponse;
  assert(thirdPromptVersionReceipt.status() === 201, "回滚后的 LabelVersion 未绑定新的 PromptVersion 草稿");
  const thirdPromptVersion = await responseData(thirdPromptVersionReceipt, "第三轮 PromptVersion 回执缺少强 ID");
  assert(
    ![firstPromptVersion.prompt_version_id, secondPromptVersion.prompt_version_id].includes(thirdPromptVersion.prompt_version_id) &&
      thirdPromptVersion.label_version_id === thirdLabelVersion.label_version_id,
    "第三轮 PromptVersion 未唯一绑定第三轮 LabelVersion",
    { firstPromptVersion, secondPromptVersion, thirdPromptVersion, thirdLabelVersion }
  );
  await page.getByTestId("prompt-review-binding").filter({ hasText: "尚无 PromptVersionCandidate" }).waitFor({ state: "visible", timeout: 8000 });
  assert(await page.getByRole("button", { name: "锁定并运行评测", exact: true }).isDisabled(), "第三轮 OptimizationRun 前错误继承第二轮审批");
  const thirdOptimizationResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/label-optimization-runs") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.locator(".label-v2-hero-actions").getByRole("button", { name: "启动智能创建", exact: true }).click();
  const thirdOptimizationReceipt = await thirdOptimizationResponse;
  assert(thirdOptimizationReceipt.status() === 202, "回滚后的新 LabelVersion 未重新物化 Prompt 候选");
  const thirdOptimizationRun = await responseData(thirdOptimizationReceipt, "第三轮 OptimizationRun 回执缺少强 ID");
  const thirdPromptCandidateId = thirdOptimizationRun.prompt_candidate_ids?.[0];
  assert(
    ![firstOptimizationRun.id, secondOptimizationRun.id].includes(thirdOptimizationRun.id) &&
      thirdOptimizationRun.label_version_id === thirdLabelVersion.label_version_id &&
      thirdOptimizationRun.prompt_version_id === thirdPromptVersion.prompt_version_id &&
      typeof thirdPromptCandidateId === "string" &&
      ![firstPromptCandidateId, secondPromptCandidateId].includes(thirdPromptCandidateId),
    "第三轮 OptimizationRun/Candidate 未唯一绑定第三轮强版本",
    { firstOptimizationRun, secondOptimizationRun, thirdOptimizationRun }
  );
  await expectBodyText(page, "场景 Agent已完成", 8000);
  await openTab(page, "规则/Prompt");
  await completePromptCandidateGate(page, thirdPromptCandidateId, secondPromptCandidateId);
  await openTab(page, "评测人审");
  const thirdEvalResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/eval-runs") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.locator("button.label-v2-wide-action").click();
  assert((await thirdEvalResponse).status() === 202, "自动回滚回流后 EvalRun 未重建");
  await page.locator('[data-label-eval-status="success"][data-label-eval-backend-status="success"]').waitFor({ state: "visible", timeout: 10000 });
  await openTab(page, "版本发布");
  const thirdPublishResponse = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/release-deployments") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByTestId("label-publish-candidate").click();
  const thirdPublishReceipt = await thirdPublishResponse;
  assert(thirdPublishReceipt.status() === 201, "稳定窗口场景 ReleaseDeployment 未进入 shadow");
  const thirdDeployment = (await thirdPublishReceipt.json()).data;
  await page.locator('[data-label-backend-status="shadowing"]').waitFor({ state: "visible", timeout: 8000 });
  const stableGrayResponse = page.waitForResponse(
    (response) => response.url().includes("/api/v1/release-deployments/") && response.url().endsWith("/transitions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByRole("button", { name: "灰度发布", exact: true }).last().click();
  assert((await stableGrayResponse).status() === 200, "稳定窗口场景未进入人工 10% gray");
  await page.locator('[data-label-backend-status="gray-releasing"]').waitFor({ state: "visible", timeout: 8000 });
  const stableMonitorResponse = await page.request.post(
    new URL(`/api/v1/release-deployments/${thirdDeployment.deployment_id}/monitor-samples`, page.url()).toString(),
    {
      headers: {
        Authorization: `Bearer ${smokeSessionToken}`,
        "Idempotency-Key": "ui-smoke-stable-monitor",
        "X-Auris-System-Worker": "ui-smoke-monitor"
      },
      data: {
        sample_id: "monitor-stable-window-ui",
        expected_status: "gray-releasing",
        window_started_at: "2026-07-15T02:00:00Z",
        window_ended_at: "2026-07-15T02:30:00Z",
        sample_size: 2000,
        stable_window_complete: true,
        metrics: {
          json_valid_rate: 0.999,
          conflict_rate: 0.02,
          critical_recall_delta_pp: 0.1,
          human_override_delta_pp: 0.2,
          cost_ratio: 1.03,
          latency_ratio: 1.05
        }
      }
    }
  );
  assert(stableMonitorResponse.status() === 200, "system 稳定窗口监控样本未写入");
  await page.locator('[data-label-publish-refresh="true"]').click();
  await page.locator('[data-label-backend-status="monitoring"]').waitFor({ state: "visible", timeout: 10000 });
  const promoteResponse = page.waitForResponse(
    (response) => response.url().includes("/api/v1/release-deployments/") && response.url().endsWith("/transitions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByRole("button", { name: "执行发布动作", exact: true }).last().click();
  assert((await promoteResponse).status() === 200, "稳定窗口后人工晋级未成功");
  await page.locator('[data-label-publish-status="success"][data-label-backend-status="published"]').waitFor({ state: "visible", timeout: 10000 });
  await expectBodyText(page, "只有此终态显示成功");
}

async function runModalCloseSmoke(page) {
  await openModule(page, "项目", "项目管理");
  const openButton = page.getByRole("button", { name: "新建项目" });
  assert(await openButton.isEnabled(), "项目管理员无法打开新建项目弹窗");
  await openButton.click();
  const dialog = page.getByRole("dialog", { name: "新建项目" });
  await dialog.waitFor({ state: "visible", timeout: 6000 });
  await dialog.getByRole("button", { name: "关闭" }).click();
  await dialog.waitFor({ state: "hidden", timeout: 6000 });

  await openButton.click();
  await dialog.waitFor({ state: "visible", timeout: 6000 });
  await page.locator(".entity-modal-scrim").click({ position: { x: 4, y: 4 } });
  await dialog.waitFor({ state: "hidden", timeout: 6000 });
}

async function runHotwordGovernanceSmoke(page) {
  await openModule(page, "洞察", "业务洞察");
  const insightScope = page.locator(".insight-scope-panel");
  await insightScope.filter({ hasText: "lmb_to_lv_19_20250531" }).waitFor({ state: "visible", timeout: 8000 });
  await insightScope.filter({ hasText: "Generation 42" }).waitFor({ state: "visible", timeout: 8000 });
  await insightScope.filter({ hasText: "MAPPING_RECOMPUTE_REQUIRED" }).waitFor({ state: "visible", timeout: 8000 });
  await insightScope.getByText("涨跌已隐藏", { exact: true }).first().waitFor({ state: "visible", timeout: 8000 });
  await openTab(page, "模型质量");
  const hotwordPanel = page.getByTestId("hotword-statistics-panel");
  await hotwordPanel.waitFor({ state: "visible", timeout: 10000 });
  for (const metric of ["热词覆盖率", "热词召回率", "易错率", "误增强率", "影响会话数"]) {
    await hotwordPanel.filter({ hasText: metric }).waitFor({ state: "visible", timeout: 6000 });
  }
  await expectBodyText(page, "93.7%");
  await expectBodyText(page, "53");
  await expectBodyText(page, "汽车销售热词包 v1.8");
  await expectBodyText(page, "Provider");
  const analysisResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/hotword-analysis-runs") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByTestId("hotword-analysis-button").click();
  const analysisResponse = await analysisResponsePromise;
  assert(analysisResponse.status() === 202, "hotword analysis expected 202");
  try {
    await expectBodyText(page, "热词分析已完成", 12000);
  } catch (error) {
    error.detail = {
      ...(error.detail ?? {}),
      hotwordAnalysisPollRequests,
      notices: await hotwordPanel.locator(".operation-toast").allTextContents()
    };
    throw error;
  }
  await expectBodyText(page, "hotword-analysis-ui-1");
  await expectBodyText(page, "trace_hotword_analysis");

  const governedDrilldown = page.getByTestId("hotword-drilldown-A-4107");
  await governedDrilldown.waitFor({ state: "visible", timeout: 10000 });
  assert(
    await page.getByTestId("hotword-drilldown-A-OTHER-VERSION").count() === 0,
    "同标准词的其他词包版本 Badcase 不得覆盖统计返回的精确 badcase_id"
  );
  await governedDrilldown.click();
  await expectBodyText(page, "评测中心");
  await openTab(page, "badcase");
  await page.getByTestId("badcase-capability-asr-hotword").click();
  await expectBodyText(page, "A-4107");
  await expectBodyText(page, "标准词");
  await expectBodyText(page, "识别结果");
  await expectBodyText(page, "misrecognition");
  await expectBodyText(page, "证据等级");
  const hotwordBadcaseProfile = page.getByTestId("hotword-badcase-profile");
  await hotwordBadcaseProfile.filter({ hasText: "trace-hotword-a-4107" }).waitFor({ state: "visible", timeout: 6000 });
  assert(
    await hotwordBadcaseProfile.filter({ hasText: "trace-hotword-other-version" }).count() === 0,
    "同标准词的其他词包版本 root_trace_id 不得污染洞察下钻"
  );
  const decisionResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/badcases/A-4107/decisions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByTestId("hotword-confirm-decision").click();
  const decisionResponse = await decisionResponsePromise;
  assert(decisionResponse.status() === 201, "hotword badcase decision expected 201");
  await expectBodyText(page, "易错词确认已落账");
  await expectBodyText(page, "trace_hotword_decision");
  const candidateVersionResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/hotword-packs/hotword_pack_auto_sales/versions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  const candidateResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/hotword-pack-versions/hwpv-ui-candidate-42/items/hotword-item-xingyue-l") && response.request().method() === "PATCH",
    { timeout: 10000 }
  );
  await page.getByTestId("hotword-candidate-add").click();
  const candidateVersionResponse = await candidateVersionResponsePromise;
  assert(candidateVersionResponse.status() === 201, "hotword candidate version expected 201");
  const candidateResponse = await candidateResponsePromise;
  assert(candidateResponse.status() === 200, "inherited hotword candidate item expected 200 PATCH");
  await expectBodyText(page, "hwpv-ui-candidate-42");
  await expectBodyText(page, "候选词包构建完成", 12000);
  await expectBodyText(page, "hotword-build-ui-1");

  await openTab(page, "模型对比");
  await page.getByTestId("model-compare-asr-hotword").click();
  await expectBodyText(page, "固定评测集");
  for (const gate of ["热词召回", "误增强", "全局 CER/WER", "下游 F1", "P95 延迟", "分钟成本"]) {
    await expectBodyText(page, gate);
  }
  await expectBodyText(page, "不可判定");
  const evalResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/hotword-pack-versions/hwpv-ui-candidate-42/eval-runs") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByTestId("hotword-shadow-eval").click();
  const evalResponse = await evalResponsePromise;
  assert(evalResponse.status() === 202, "hotword shadow eval expected 202");
  await expectBodyText(page, "影子评测运行已创建");
  await expectBodyText(page, "hotword-eval-ui-1");
  await expectBodyText(page, "影子评测门禁通过", 12000);
  await expectBodyText(page, "88.1%");
  await expectBodyText(page, "93.4%");

  const approvalResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/hotword-pack-versions/hwpv-ui-candidate-42") && response.request().method() === "PATCH",
    { timeout: 10000 }
  );
  await page.getByTestId("hotword-model-approve").click();
  const approvalResponse = await approvalResponsePromise;
  assert(approvalResponse.status() === 200, "hotword model approval expected 200");
  await expectBodyText(page, "模型负责人审批已完成");

  await page.locator("button.sidebar-user-logout").click();
  await page.getByRole("button", { name: "登录" }).waitFor({ state: "visible", timeout: 10000 });
  await page.getByPlaceholder("name@company.com").fill("demo.operator@auris.local");
  await page.getByPlaceholder("至少 6 位").fill("auris-demo");
  await page.locator("button.auth-submit").click();
  await expectBodyText(page, "Demo Operator");
  await openModule(page, "评测", "评测中心");
  await openTab(page, "模型对比");
  await page.getByTestId("model-compare-asr-hotword").click();
  await expectBodyText(page, "hwpv-ui-candidate-42", 10000);
  await expectBodyText(page, "approved", 10000);
  const publishResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/hotword-pack-versions/hwpv-ui-candidate-42/publish") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByTestId("hotword-manual-publish").click();
  const publishResponse = await publishResponsePromise;
  assert(publishResponse.status() === 202, "hotword publish expected 202 pending");
  await expectBodyText(page, "词包发布运行已创建");
  await expectBodyText(page, "词包发布运行失败", 12000);
  await page.locator(".evaluation-operation-toast")
    .filter({ hasText: "hotword-publish-ui-1" })
    .waitFor({ state: "visible", timeout: 6000 });
  const retryPublishResponsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/runs/hotword-publish-ui-1/retries") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  const retryPublishButton = page.getByTestId("hotword-manual-publish");
  await retryPublishButton.filter({ hasText: "重试发布运行" }).waitFor({ state: "visible", timeout: 6000 });
  await retryPublishButton.click();
  const retryPublishResponse = await retryPublishResponsePromise;
  assert(retryPublishResponse.status() === 202, "hotword publish retry expected 202 pending");
  await expectBodyText(page, "发布重试已创建");
  await expectBodyText(page, "词包已人工发布");
  await expectBodyText(page, "task-version-hotword-ui-1");

  await openModule(page, "调听", "调听工作台");
  await clickButtonContaining(page.locator(".listening-mode-switch"), "证据审查");
  await clickExactButton(page.locator(".panel-tabs"), "字段差异");
  const unmatchedCorrectionPanel = page.getByTestId("asr-hotword-correction");
  await unmatchedCorrectionPanel.waitFor({ state: "visible", timeout: 8000 });
  await page.getByTestId("asr-hotword-authority-binding")
    .filter({ hasText: "权威绑定恢复已阻断" })
    .waitFor({ state: "visible", timeout: 10000 });
  assert(await page.getByTestId("asr-hotword-badcase-submit").isDisabled(), "未匹配受控证据对象的样本必须阻断 Badcase 创建");
  await page.getByTestId("review-head-queue-低置信").click();
  await clickExactButton(page.locator(".panel-tabs"), "字段差异");
  const correctionPanel = page.getByTestId("asr-hotword-correction");
  await correctionPanel.waitFor({ state: "visible", timeout: 8000 });
  const authorityBinding = page.getByTestId("asr-hotword-authority-binding");
  await authorityBinding.filter({ hasText: "已有 A-4107" }).waitFor({ state: "visible", timeout: 10000 });
  await authorityBinding.filter({ hasText: "hwpv-auto-sales-v1-8" }).waitFor({ state: "visible", timeout: 6000 });
  await authorityBinding.filter({ hasText: "storage_evidence_af_131_hotword_diff" }).waitFor({ state: "visible", timeout: 6000 });
  await authorityBinding.filter({ hasText: "trace-hotword-a-4107" }).waitFor({ state: "visible", timeout: 6000 });
  assert(!(await correctionPanel.getByLabel("错误类型").isDisabled()), "记录修正前错误类型应可确认");
  assert(!(await correctionPanel.getByLabel("识别文本").isDisabled()), "记录修正前识别文本应可确认");
  assert(!(await correctionPanel.getByLabel("正确文本").isDisabled()), "记录修正前正确文本应可确认");
  assert(await correctionPanel.getByLabel("识别文本").inputValue() === "星月L", "应从 A-4107 恢复识别文本");
  assert(await correctionPanel.getByLabel("正确文本").inputValue() === "星越L", "应从 A-4107 恢复标准词");
  const badcasePostCountBeforeReuse = hotwordWriteRequests.filter(
    (request) => request.path === "/api/v1/badcases" && request.method === "POST"
  ).length;
  const correctionResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/audio-sessions/S20250526-000131/annotations") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  const reuseBadcaseButton = page.getByTestId("asr-hotword-badcase-submit");
  await reuseBadcaseButton.filter({ hasText: "记录修正并关联 A-4107" }).waitFor({ state: "visible", timeout: 6000 });
  await reuseBadcaseButton.click();
  const correctionResponse = await correctionResponsePromise;
  const correctionJson = await correctionResponse.json().catch(() => ({}));
  assert(correctionResponse.status() === 201, "ASR 标注修正 expected 201", correctionJson);
  const correctionPayload = correctionResponse.request().postDataJSON();
  assert(
    correctionPayload?.annotation_kind === "asr-transcript-correction" &&
      correctionPayload?.recognized_text === "星月L" &&
      correctionPayload?.corrected_text === "星越L" &&
      correctionPayload?.hotword_pack_version_id === "hwpv-auto-sales-v1-8" &&
      correctionPayload?.source_badcase_id === "A-4107",
    "ASR Diff 未提交受控标注修正与历史证据版本",
    correctionPayload
  );
  await expectBodyText(page, "标注修正已计入发现统计");
  assert(await correctionPanel.getByLabel("识别文本").isDisabled(), "修正记录成功后识别文本应只读");
  await reuseBadcaseButton.filter({ hasText: "已记录 · 查看 A-4107" }).waitFor({ state: "visible", timeout: 6000 });
  await reuseBadcaseButton.click();
  await expectBodyText(page, "评测中心");
  await expectBodyText(page, "A-4107");
  const badcasePostCountAfterReuse = hotwordWriteRequests.filter(
    (request) => request.path === "/api/v1/badcases" && request.method === "POST"
  ).length;
  assert(
    badcasePostCountAfterReuse === badcasePostCountBeforeReuse,
    "证据已绑定 A-4107 时 ASR Diff 不得重复 POST /badcases",
    { badcasePostCountBeforeReuse, badcasePostCountAfterReuse, hotwordWriteRequests }
  );

  await openModule(page, "任务", "任务配置");
  await openTab(page, "流程配置");
  await clickExactButton(page.locator(".canvas-level-switch"), "节点画布");
  await clickButtonContaining(page.locator(".connector-canvas"), "ASR 输出");
  const bindingPanel = page.getByTestId("hotword-version-binding");
  await bindingPanel.waitFor({ state: "visible", timeout: 8000 });
  await expectBodyText(page, "hotword_pack_version_id");
  await expectBodyText(page, "hwpv-ui-candidate-42");
  await expectBodyText(page, "生产运行仅允许已发布版本");
  const taskDraftResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/task-versions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await bindingPanel.getByRole("button", { name: "保存热词版本绑定" }).click();
  const taskDraftResponse = await taskDraftResponsePromise;
  assert(taskDraftResponse.status() === 201, "TaskVersion hotword binding expected 201");
  await expectBodyText(page, "任务草稿已保存");
  const taskRunResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/task-runs") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await clickExactButton(page.locator(".canvas-toolbar-actions"), "运行");
  const taskRunResponse = await taskRunResponsePromise;
  assert(taskRunResponse.status() === 202, "TaskRun expected 202");
  await expectBodyText(page, "task-run-ui-hotword-1");

  await openModule(page, "资产", "数据资产");
  await openTab(page, "资产血缘");
  const lineage = page.getByTestId("hotword-governance-lineage");
  await lineage.waitFor({ state: "visible", timeout: 8000 });
  for (const node of ["原 ASR 资产", "词级证据", "A-4107 Badcase", "词包候选版本", "EvalRun", "新转写资产", "受控回填"]) {
    await lineage.filter({ hasText: node }).waitFor({ state: "visible", timeout: 6000 });
  }
  await expectBodyText(page, "root_trace_id");
  const controlledBackfill = page.getByTestId("hotword-controlled-backfill");
  assert(await controlledBackfill.isDisabled(), "TaskVersion 尚未发布时受控回填必须 blocked");
  assert(assetBackfillRequests.length === 0, "TaskVersion 尚未发布时不应创建回填请求", { assetBackfillRequests });
  await expectBodyText(page, "TaskVersion task-version-hotword-ui-1 当前为 draft");
  await page.getByTestId("hotword-open-task-publish").click();
  await expectBodyText(page, "任务配置");
  const recoveredTaskVersion = page.getByTestId("recovered-task-version");
  await recoveredTaskVersion.waitFor({ state: "visible", timeout: 10000 });
  await recoveredTaskVersion.filter({ hasText: "task-version-hotword-ui-1" }).waitFor({ state: "visible", timeout: 6000 });
  await recoveredTaskVersion.filter({ hasText: "draft" }).waitFor({ state: "visible", timeout: 6000 });
  const taskPublishResponsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/task-versions/task-version-hotword-ui-1/publish") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByTestId("task-version-publish").click();
  const taskPublishResponse = await taskPublishResponsePromise;
  assert(taskPublishResponse.status() === 202, "hotword TaskVersion publish gate expected 202");
  await expectBodyText(page, "发布门禁已创建");
  const taskApprovalResponsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/runs/task-version-publish-ui-1/decisions") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByTestId("task-version-approve-release").click();
  const taskApprovalResponse = await taskApprovalResponsePromise;
  assert(taskApprovalResponse.status() === 201, "hotword TaskVersion release approval expected 201");
  await expectBodyText(page, "任务版本已发布", 12000);

  await openModule(page, "资产", "数据资产");
  await openTab(page, "资产血缘");
  const publishedLineage = page.getByTestId("hotword-governance-lineage");
  await publishedLineage.waitFor({ state: "visible", timeout: 8000 });
  const publishedBackfill = page.getByTestId("hotword-controlled-backfill");
  await publishedBackfill.waitFor({ state: "visible", timeout: 8000 });
  assert(await publishedBackfill.isEnabled(), "TaskVersion 发布并审批后受控回填应解锁");
  await page.getByTestId("hotword-source-materialization")
    .filter({ hasText: "mat_asr_20250526_122300" })
    .waitFor({ state: "visible", timeout: 6000 });
  await publishedBackfill.click();
  await expectBodyText(page, "回填草稿已生成");
  const backfillResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/data-assets/") && response.url().endsWith("/backfills") && response.request().method() === "POST",
    { timeout: 10000 }
  );
  await page.getByTestId("asset-backfill-submit").click();
  const backfillResponse = await backfillResponsePromise;
  assert(backfillResponse.status() === 202, "controlled asset backfill expected 202");
  await expectBodyText(page, "受控回填已完成", 12000);
  await expectBodyText(page, "asset-backfill-hotword-ui-1");
  await expectBodyText(page, "trace_hotword_pack_auto_sales");
}

async function assertBootFailureFallback(browser, baseUrl) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1
  });
  const page = await context.newPage();
  const pageErrors = [];
  const requestFailures = [];
  const failedResponses = [];

  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("requestfailed", (request) => {
    requestFailures.push({
      error: request.failure()?.errorText ?? "unknown request failure",
      method: request.method(),
      url: request.url()
    });
  });
  page.on("response", (response) => {
    if (response.status() >= 400) {
      failedResponses.push({
        status: response.status(),
        method: response.request().method(),
        url: response.url()
      });
    }
  });

  await page.route(/\/src\/catalogs\/(?:production\/)?module-catalog\.json(?:\?.*)?$/, async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ error: "catalog temporarily unavailable" })
    });
  });

  try {
    await page.goto(baseUrl, { waitUntil: "networkidle", timeout: 30000 });
    const alert = page.getByRole("alert");
    await alert.waitFor({ state: "visible", timeout: 8000 });
    await expectBodyText(page, "工作区加载失败");
    await expectBodyText(page, "模块 catalog 加载失败：503");
    const retry = page.getByRole("button", { name: "重新加载" });
    await retry.waitFor({ state: "visible", timeout: 3000 });
    assert(await retry.isEnabled(), "启动失败页的重新加载按钮不可用");
    assert(pageErrors.length === 0, "启动失败兜底仍产生未捕获页面错误", { pageErrors });
    assert(requestFailures.length === 0, "启动失败兜底存在预期外的网络传输失败", { requestFailures });
    assert(
      failedResponses.length >= 1 &&
        failedResponses.every(
          (failure) => failure.status === 503 && failure.url.includes("/src/catalogs/") && failure.url.includes("module-catalog.json")
        ),
      "启动失败场景出现预期外的失败响应",
      { failedResponses }
    );
  } finally {
    await context.close();
  }
}

async function assertProjectionSourceStates(page, failedResponses, browserErrors) {
  const projectsPattern = /\/api\/v1\/projects(?:\?.*)?$/;
  let scenario = "synced";
  const scenarioItems = [
    {
      project_id: "project_bff_alpha",
      name: "BFF 同步项目 Alpha",
      owner_name: "后端负责人 A",
      status: "active",
      today_added: 11,
      pending_count: 2,
      pass_rate: 97.5,
      asset_status: "healthy"
    },
    {
      project_id: "project_bff_beta",
      name: "BFF 同步项目 Beta",
      owner_name: "后端负责人 B",
      status: "evaluating",
      today_added: 7,
      pending_count: 1,
      pass_rate: 95,
      asset_status: "backfill_pending"
    }
  ];
  const routeHandler = async (route) => {
    if (scenario === "degraded") {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: { message: "projection scenario unavailable" } })
      });
      return;
    }
    const items = scenario === "empty" ? [] : scenarioItems;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: { items },
        meta: { trace_id: `trace_projection_${scenario}`, request_id: `projection-${scenario}` }
      })
    });
  };
  const openProjectsForScenario = async (nextScenario) => {
    scenario = nextScenario;
    const tenantNav = page.locator('button[aria-label="导航：租户"]').first();
    await tenantNav.click();
    await expectBodyText(page, "租户管理");
    const requestPromise = page.waitForResponse(
      (response) => projectsPattern.test(response.url()) && response.request().method() === "GET",
      { timeout: 10000 }
    );
    await page.locator('button[aria-label="导航：项目"]').first().click();
    await expectBodyText(page, "项目管理");
    return requestPromise;
  };

  await page.route(projectsPattern, routeHandler);
  try {
    await openProjectsForScenario("synced");
    await page.locator('[data-testid="module-projection-state"][data-state="synced"][data-source="bff"]').waitFor({ state: "visible", timeout: 8000 });
    const syncedMetrics = page.locator('.module-metrics[data-source="bff"] .module-metric');
    assert(await syncedMetrics.count() === 4, "synced projection 指标数量异常");
    await syncedMetrics.first().filter({ hasText: "1" }).waitFor({ state: "visible", timeout: 5000 });
    assert((await syncedMetrics.filter({ hasText: "—" }).count()) === 3, "synced projection 仍混入 fixture 指标值");
    const syncedRows = page.locator('[data-testid="project-projection-row"]');
    assert(await syncedRows.count() === 2, "synced projection 未使用 BFF 项目列表");
    await expectBodyText(page, "BFF 同步项目 Alpha");
    assert(await page.locator(".project-work-card").filter({ hasText: "销售话术质检" }).count() === 0, "synced projection 静默混入静态项目 fixture");

    await openProjectsForScenario("empty");
    await page.locator('[data-testid="module-projection-state"][data-state="empty"][data-source="bff"]').waitFor({ state: "visible", timeout: 8000 });
    await page.getByTestId("module-projection-empty").waitFor({ state: "visible", timeout: 5000 });
    await expectBodyText(page, "当前范围暂无 BFF 投影数据");
    assert(await page.locator(".project-work-card").count() === 0, "empty projection 回落并展示了静态项目 fixture");
    assert((await page.locator('.module-metrics[data-source="bff-empty"] .module-metric').filter({ hasText: "—" }).count()) === 4, "empty projection 未展示真实空指标态");

    const degradedResponse = await openProjectsForScenario("degraded");
    assert(degradedResponse.status() === 503, "degraded projection 场景未返回预期 503");
    await page.locator('[data-testid="module-projection-state"][data-state="degraded"][data-source="mock"]').waitFor({ state: "visible", timeout: 8000 });
    await expectBodyText(page, "降级模式 · Mock fixture");
    await expectBodyText(page, "销售话术质检");
    assert(await page.locator('.module-metrics[data-source="mock"] .module-metric').filter({ hasText: "Mock fixture" }).count() === 4, "degraded projection 未标明 fixture 指标来源");

    const expectedFailureIndex = failedResponses.findIndex(
      (failure) => failure.status === 503 && projectsPattern.test(failure.url)
    );
    assert(expectedFailureIndex >= 0, "degraded projection 的预期失败响应未被观测");
    failedResponses.splice(expectedFailureIndex, 1);
    const expectedConsoleErrorIndex = browserErrors.findIndex(
      (message) => message.includes("status of 503") && message.includes("Service Unavailable")
    );
    assert(expectedConsoleErrorIndex >= 0, "degraded projection 的预期浏览器 503 日志未被观测");
    browserErrors.splice(expectedConsoleErrorIndex, 1);
  } finally {
    await page.unroute(projectsPattern, routeHandler);
  }
}

const projectionFixtures = {
  "/api/v1/insights/ops-summary": {
    data: {
      metrics: [
        { metric_key: "projects", value: 12 },
        { metric_key: "today_audio", value: 9421 },
        { metric_key: "auto_pass_rate", value: 86.4 },
        { metric_key: "human_review", value: 319 },
        { metric_key: "asset_risk", value: 17 },
        { metric_key: "model_anomaly", value: 2 }
      ],
      audio_count: 9421,
      pending_count: 319,
      anomaly_count: 17,
      recent_asset_count: 23
    }
  },
  "/api/v1/tenants": { data: { items: [{ id: "aurora_auto", name: "极光汽车" }] } },
  "/api/v1/projects": { data: { items: [{ id: "sales_qa", name: "销售话术质检" }] } },
  "/api/v1/task-versions": { data: { items: [{ id: "task_version_v3", status: "draft" }] } },
  "/api/v1/audio-sessions/aggregations": {
    data: { items: [{ id: "people", total: 3 }, { id: "events", total: 46 }] }
  },
  "/api/v1/audio-sessions": {
    data: {
      items: [
        { audio_session_id: "S20250526-000128", status: "pending_review", confidence: 0.86 },
        { audio_session_id: "S20250526-000131", status: "pending_review", confidence: 0.64 }
      ]
    }
  },
  "/api/v1/human-review-tasks": {
    data: {
      items: [
        { id: "hrt_amount_001", queue: "amount_conflict", evidence_pack_id: "AF-128", status: "pending" },
        { id: "hrt_crosstalk_001", queue: "crosstalk_candidate", evidence_pack_id: "AF-129", status: "pending" },
        { id: "hrt_low_confidence_draft", queue: "low_confidence", evidence_pack_id: "AF-131", status: "pending" }
      ]
    }
  },
  "/api/v1/knowledge-sources": {
    data: { items: [{ id: "ks_sales_sop", status: "synced" }] }
  },
  "/api/v1/label-versions": { data: { items: [{ id: "label_v1_8_4", status: "published" }] } },
  "/api/v1/insights/metrics": {
    data: {
      items: [{
        id: "metric_quote_consistency_42",
        metric_result_id: "metric_quote_consistency_42",
        metric_key: "quoteConsistency",
        label: "报价一致率",
        value: 86.2,
        unit: "%",
        sample_size: 128,
        status: "materialized",
        source_run_id: "insight_metric_run_42",
        snapshot_role: "aggregation",
        immutable: true,
        label_version_applicability: "required",
        label_scope: {
          taxonomy_mode: "normalized",
          source_label_version_ids: ["lv_18", "lv_19"],
          target_label_version_id: "lv_19",
          mapping_bundle_id: "lmb_to_lv_19_20250531",
          fact_set_generation: 42,
          fact_as_of: "2025-05-31T23:59:59Z"
        },
        comparability_status: "structural-break",
        comparability_reason_codes: ["MAPPING_RECOMPUTE_REQUIRED"]
      }]
    }
  },
  "/api/v1/eval-runs": { data: { items: [{ id: "eval_run_seed", status: "success" }] } },
  "/api/v1/data-assets/recent": {
    data: { items: [{ id: "asset_event_tags", asset_key: "auris/label/event_tags" }] }
  },
  "/api/v1/settings": { data: { items: [{ id: "model_provider", status: "enabled" }] } }
};

const projectionHits = new Set();
let exportRequests = 0;
let authLoginRequests = 0;
let authLogoutRequests = 0;
const authEmails = [];
let activeSmokeEmail = "";
let hotwordReadRequests = 0;
const hotwordWriteRequests = [];
const hotwordStatisticsRequests = [];
const hotwordBadcaseReadRequests = [];
const taskWriteRequests = [];
const taskReleaseRequests = [];
const assetBackfillRequests = [];
const labelReviewRequests = [];
const labelReviewTasks = new Map();
for (const task of projectionFixtures["/api/v1/human-review-tasks"].data.items) {
  const audioSessionId = task.evidence_pack_id === "AF-131" ? "S20250526-000131" : "S20250526-000128";
  labelReviewTasks.set(task.id, {
    ...task,
    title: task.queue === "amount_conflict" ? "金额冲突待复核" : task.queue === "crosstalk_candidate" ? "串音候选待确认" : "低置信转写待确认",
    asset_key: task.evidence_pack_id === "AF-128" ? "auris/label/event_tags" : task.evidence_pack_id === "AF-129" ? "auris/audio/raw_recordings" : "auris/model/asr_transcripts",
    evidence_pack: {
      evidence_pack_id: task.evidence_pack_id,
      audio_session_id: audioSessionId,
      title: `${task.evidence_pack_id} 后端证据包`,
      window_start_ms: task.evidence_pack_id === "AF-131" ? 68000 : 227000,
      window_end_ms: task.evidence_pack_id === "AF-131" ? 100000 : 270000
    },
    trace_id: `trace_${task.id}`
  });
}

const listeningAudioSessions = new Map([
  [
    "S20250526-000128",
    {
      audio_session_id: "S20250526-000128",
      recording_id: "rec_A_1001_20250526_122300",
      store_id: "BJ-AURORA-001",
      primary_employee_id: "u_sales_a",
      started_at: "2025-05-26T12:23:30+08:00",
      ended_at: "2025-05-26T12:34:34+08:00",
      status: "pending_review",
      confidence: 0.86,
      trace_id: "trace_20250526_122718",
      recording: { file_name: "A-1001_20250526_122300.wav" },
      evidence_packs: [
        { evidence_pack_id: "AF-128", audio_session_id: "S20250526-000128" },
        { evidence_pack_id: "AF-129", audio_session_id: "S20250526-000128" }
      ],
      asr_segments: [
        { segment_id: "asr_s128_001", speaker: "销售A", start_ms: 206000, end_ms: 258000, text: "这台 325Li 指导价 31.69 万，可以给到 3.5 万优惠。", confidence: 0.92 },
        { segment_id: "asr_s128_002", speaker: "客户", start_ms: 258000, end_ms: 286000, text: "那落地是不是二十八万多一点？", confidence: 0.82 }
      ],
      event_links: [
        {
          id: "event_quote_122718",
          relation_type: "quote",
          document_ref: "BJ-041",
          status: "pending",
          match_score: 0.92,
          diffs: [{ field: "amount", audio_value: "落地 28.19 万", document_value: "指导价 31.69 万" }]
        }
      ],
      boundaries: [],
      listening_annotations: []
    }
  ],
  [
    "S20250526-000131",
    {
      audio_session_id: "S20250526-000131",
      recording_id: "rec_SH_A012_20250526_091500",
      store_id: "SH-JINGAN-001",
      primary_employee_id: "u_sales_a",
      started_at: "2025-05-26T09:15:00+08:00",
      ended_at: "2025-05-26T09:18:00+08:00",
      status: "pending_review",
      confidence: 0.64,
      trace_id: "trace_20250526_091608",
      recording: { file_name: "SH-A012_20250526_091500.wav" },
      evidence_packs: [{ evidence_pack_id: "AF-131", audio_session_id: "S20250526-000131" }],
      asr_segments: [{ segment_id: "asr_s131_001", speaker: "销售A", start_ms: 68000, end_ms: 100000, text: "车型热词未命中，当前片段等待人工修正。", confidence: 0.64 }],
      event_links: [],
      boundaries: [],
      listening_annotations: []
    }
  ]
]);
const labelPublishRequests = [];
const labelPublishRunPolls = new Map();
const labelReleaseDeployments = new Map();
const labelEvalRuns = new Map();
const labelEvalRequests = [];
const labelEvalRetryRequests = [];
const labelMonitorSampleRequests = [];
const labelAggregationRequests = [];
const labelSpecializedReviewRequests = [];
const labelVersionRequests = [];
const labelPromptVersionRequests = [];
const labelOptimizationRequests = [];
const labelOptimizationRuns = new Map();
const labelPromptCandidates = new Map();
let labelExtractionRunId = "";
let labelAggregationRunId = "label-aggregation-ui-1";
let labelStrongVersionId = "";
let labelPromptDraftId = "";
let labelVersionSequence = 0;
let labelPromptDraftSequence = 0;
let labelObservationReadRequests = 0;
let labelAggregateReadRequests = 0;
let labelEvalRunSequence = 0;
let labelPublishSequence = 0;
let labelVersionStatus = "candidate";
const candidateVersionId = "hwpv-ui-candidate-42";
let candidateVersionCreated = false;
let candidateVersionStatus = "missing";
let candidateResourceVersion = 0;
let candidateBuildRunId = null;
let candidateEvalRunId = null;
let candidatePublishRunId = null;
let candidateModelApprovedBy = null;
let hotwordBuildPollRequests = 0;
let hotwordEvalPollRequests = 0;
let hotwordPublishPollRequests = 0;
let hotwordPublishRetryRequests = 0;
let hotwordPublishRetryPollRequests = 0;
let hotwordAnalysisPollRequests = 0;
let assetBackfillPollRequests = 0;
let hotwordTaskVersionStatus = "draft";
let taskVersionPublishPollRequests = 0;
let badcaseResourceVersion = 3;
let candidateItemResourceVersion = 1;
let candidateItemAliases = ["星越 L"];
const smokeSessionToken = "auris.v1.ui-smoke.server-issued";
const uiSmokeSceneManifestSha256 = "4c15a212be8c1ebc466e1c8845412737d8be38df85980a057dbccf489303581f";
const uiSmokeSceneManifest = {
  schema_version: "scene-profile/1",
  scene_key: "ui-smoke-audio-intelligence",
  display_name: "UI Smoke 音频智能场景",
  description: "UI 冒烟使用的确定性生产场景绑定。",
  locales: ["zh-CN"],
  capabilities: ["audio-intelligence", "labeling", "insight"],
  roles: [
    { role_key: "operator", display_name: "质检运营", description: "执行音频任务与人工复核" }
  ],
  entities: [
    { object_key: "audio-session", display_name: "音频会话", schema_ref: "schema:audio-session/v1", required: true }
  ],
  events: [
    { object_key: "quote-event", display_name: "报价事件", schema_ref: "schema:quote-event/v1", required: false }
  ],
  document_types: [],
  data_contract_refs: ["contract:audio-session/v1"],
  task_type_refs: ["audio-intelligence-flow"],
  label_version_refs: ["label_v1_8_4"],
  prompt_version_refs: ["prompt_quote_v3"],
  knowledge_index_refs: ["ki_sales_policy_v1"],
  eval_dataset_version_refs: ["evalset_quote_risk_v12"],
  connector_refs: ["conn_platform_auth"],
  model_service_refs: ["model_asr_prod"],
  hotword_pack_version_refs: ["hwpv-auto-sales-v1-8"],
  rubric_refs: [],
  output_sink_refs: ["sink_platform_callback"],
  dimensions: [],
  action_bindings: [],
  metrics: [
    {
      metric_key: "audio-transcript-quality",
      display_name: "音频转写质量",
      unit: "ratio",
      calculator_ref: "metric:audio-transcript-quality/v1",
      evidence_refs: ["audio", "transcript"]
    }
  ],
  release_requirements: [
    {
      requirement_key: "core-audio-gate",
      gate_kind: "core_capability",
      metric_key: "audio-transcript-quality",
      operator: "gte",
      threshold_ppm: 850000
    }
  ],
  governance: {
    human_review_required: true,
    model_may_publish: false,
    retention_policy_ref: "policy:retention/default-v1",
    privacy_policy_ref: "policy:privacy/audio-v1"
  }
};

const uiSmokeSceneProfileVersion = () => ({
  scene_profile_version_id: "scenev_ui_smoke_audio_v1",
  scene_profile_id: "scene_ui_smoke_audio",
  version: "v1.0.0",
  status: "published",
  source_type: "human",
  manifest: uiSmokeSceneManifest,
  manifest_sha256: uiSmokeSceneManifestSha256,
  resource_version: 1,
  requested_by: "u_admin_001",
  reviewed_by: "u_release_admin_001",
  published_by: "u_release_admin_001",
  trace_id: "trace_ui_smoke_scene_profile"
});

const smokeUser = () => activeSmokeEmail === "model@auris.local"
  ? {
      user_id: "u_model_001",
      name: "Model Owner",
      role: "模型负责人",
      roles: ["model_engineer"],
      initials: "M"
    }
  : {
      user_id: "u_admin_001",
      name: "Demo Operator",
      role: "平台管理员",
      roles: ["project_admin", "asset_manager"],
      initials: "D"
    };

const inheritedHotwordItem = () => ({
  id: "hotword-item-xingyue-l",
  item_id: "hotword-item-xingyue-l",
  canonical_term: "星越L",
  normalized_term: "星越l",
  aliases: candidateItemAliases,
  category: "vehicle-model",
  weight: 80,
  source_badcase_id: null,
  source_type: "seed",
  resource_version: candidateItemResourceVersion
});

const hotwordVersionPayload = () => ({
  id: candidateVersionId,
  version_id: candidateVersionId,
  pack_id: "hotword_pack_auto_sales",
  version: "v1.9",
  baseline_version_id: "hwpv-auto-sales-v1-8",
  status: candidateVersionStatus,
  content_sha256: ["missing", "draft"].includes(candidateVersionStatus) ? null : "sha256-ui-candidate",
  manifest_storage_object_id: ["missing", "draft", "validating"].includes(candidateVersionStatus) ? null : "storage-hwpv-ui-candidate-manifest",
  build_run_id: candidateBuildRunId,
  eval_run_id: candidateEvalRunId,
  eval_locked: ["review_required", "approved", "published"].includes(candidateVersionStatus),
  model_approved_by: candidateModelApprovedBy,
  project_admin_confirmed_by: candidateVersionStatus === "published" ? "u_admin_001" : null,
  provider_artifact_ref: ["missing", "draft", "validating"].includes(candidateVersionStatus) ? null : "storage://compiled/hwpv-ui-candidate-42.json",
  compiled_provider: candidateVersionStatus === "missing" || candidateVersionStatus === "draft" ? null : "auris-audio-stack",
  resource_version: candidateResourceVersion,
  root_trace_id: "trace_hotword_pack_auto_sales",
  current_trace_id: "trace_hotword_candidate",
  publish_run_id: candidatePublishRunId,
  task_version_id: candidateVersionStatus === "published" ? "task-version-hotword-ui-1" : null,
  items: [inheritedHotwordItem()]
});

const bffStub = createHttpServer((request, response) => {
  if (request.url?.startsWith("/healthz") || request.url?.startsWith("/readyz")) {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ status: "ok", data: { status: "success", service: "ui-smoke-stub" } }));
    return;
  }
  const path = request.url?.split("?")[0] ?? "";
  if (path === "/api/v1/auth/dev-login" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      if (!["model@auris.local", "demo.operator@auris.local"].includes(payload.email) || payload.password !== "auris-demo") {
        response.writeHead(401, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ error: { code: "INVALID_CREDENTIALS", message: "邮箱或密码错误" } }));
        return;
      }
      authLoginRequests += 1;
      authEmails.push(payload.email);
      activeSmokeEmail = payload.email;
      const user = smokeUser();
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: {
          access_token: smokeSessionToken,
          token_type: "Bearer",
          expires_at: "2099-01-01T00:00:00+00:00",
          user: {
            user_id: user.user_id,
            name: user.name,
            email: payload.email,
            role: user.role,
            roles: user.roles,
            initials: user.initials,
            tenant_id: "aurora_auto",
            tenant_name: "极光汽车",
            project_id: "sales_qa",
            project_name: "销售话术质检"
          }
        },
        meta: { trace_id: "trace_ui_smoke_login", request_id: "ui-smoke-login" }
      }));
    });
    return;
  }
  if (path === "/api/v1/auth/session" && request.method === "GET") {
    const user = smokeUser();
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        user_id: user.user_id,
        name: user.name,
        email: activeSmokeEmail,
        role: user.role,
        roles: user.roles,
        initials: user.initials,
        tenant_id: "aurora_auto",
        tenant_name: "极光汽车",
        project_id: "sales_qa",
        project_name: "销售话术质检",
        provider: "dev_session"
      },
      meta: { trace_id: "trace_ui_smoke_session", request_id: "ui-smoke-session" }
    }));
    return;
  }
  if (path === "/api/v1/auth/logout" && request.method === "POST") {
    if (request.headers.authorization !== `Bearer ${smokeSessionToken}`) {
      response.writeHead(401, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: { code: "UNAUTHORIZED", message: "缺少服务端会话" } }));
      return;
    }
    authLogoutRequests += 1;
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        status: "revoked",
        session_id: "session-ui-smoke",
        revoked_at: "2026-07-13T00:00:00+00:00"
      },
      meta: { trace_id: "trace_ui_smoke_logout", request_id: "ui-smoke-logout" }
    }));
    return;
  }
  if (path.startsWith("/api/v1/") && request.headers.authorization !== `Bearer ${smokeSessionToken}`) {
    response.writeHead(401, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ error: { code: "UNAUTHORIZED", message: "缺少服务端会话" } }));
    return;
  }
  if (path === "/api/v1/scene-profiles" && request.method === "GET") {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        items: [{
          scene_profile_id: "scene_ui_smoke_audio",
          scene_key: "ui-smoke-audio-intelligence",
          name: "UI Smoke 音频智能场景",
          description: "UI 冒烟使用的确定性生产场景绑定。",
          status: "published",
          current_published_version_id: "scenev_ui_smoke_audio_v1",
          version_count: 1,
          trace_id: "trace_ui_smoke_scene_profile"
        }]
      },
      meta: { total: 1, limit: 50, next_cursor: null, trace_id: "trace_ui_smoke_scene_profile" }
    }));
    return;
  }
  if (path === "/api/v1/scene-profiles/scene_ui_smoke_audio" && request.method === "GET") {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        scene_profile_id: "scene_ui_smoke_audio",
        scene_key: "ui-smoke-audio-intelligence",
        name: "UI Smoke 音频智能场景",
        description: "UI 冒烟使用的确定性生产场景绑定。",
        status: "published",
        current_published_version_id: "scenev_ui_smoke_audio_v1",
        version_count: 1,
        trace_id: "trace_ui_smoke_scene_profile",
        versions: [uiSmokeSceneProfileVersion()]
      },
      meta: { trace_id: "trace_ui_smoke_scene_profile", request_id: "ui-smoke-scene-profile-detail" }
    }));
    return;
  }
  if (path === "/api/v1/experiments" && request.method === "GET") {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: { items: [] },
      meta: { total: 0, limit: 100, next_cursor: null, trace_id: "trace_ui_smoke_experiments" }
    }));
    return;
  }
  const sceneProfileBindingMatch = path.match(/^\/api\/v1\/projects\/([^/]+)\/scene-profile$/);
  if (sceneProfileBindingMatch && request.method === "GET") {
    const projectId = decodeURIComponent(sceneProfileBindingMatch[1]);
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        binding_id: `sceneb_ui_smoke_${projectId}_production`,
        project_id: projectId,
        environment: "production",
        scene_profile_id: "scene_ui_smoke_audio",
        scene_profile_version_id: "scenev_ui_smoke_audio_v1",
        manifest_sha256: uiSmokeSceneManifestSha256,
        status: "active",
        resource_version: 1,
        trace_id: "trace_ui_smoke_scene_profile",
        version: uiSmokeSceneProfileVersion()
      },
      meta: { trace_id: "trace_ui_smoke_scene_profile", request_id: "ui-smoke-scene-profile" }
    }));
    return;
  }
  if (path === "/api/v1/label-extraction-runs" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      labelExtractionRunId = payload.extraction_run_id || "label-extract-ui-1";
      response.writeHead(202, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: {
          ...payload,
          extraction_run_id: labelExtractionRunId,
          status: "queued",
          observation_count: 0,
          trace_id: "trace_label_fact_chain_ui"
        },
        meta: { trace_id: "trace_label_fact_chain_ui", request_id: "ui-smoke-label-extraction-create" }
      }));
    });
    return;
  }
  if (path.startsWith("/api/v1/label-extraction-runs/") && request.method === "GET") {
    const runId = decodeURIComponent(path.split("/").pop() || "");
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        extraction_run_id: runId,
        label_version_id: labelStrongVersionId,
        prompt_version_id: labelPromptDraftId,
        model_version: "tagger-llm-2026.06",
        schema_version: "label-output-v1",
        subject_scope: "conversation",
        subject_refs: [{ subject_key: "subject-quote-01" }],
        observation_count: 2,
        aggregation_run_id: labelAggregationRunId,
        aggregate_ids: ["LC-quote-01", "LC-quote-02", "LC-quote-high"],
        status: "materialized",
        trace_id: "trace_label_fact_chain_ui"
      },
      meta: { trace_id: "trace_label_fact_chain_ui", request_id: "ui-smoke-label-extraction-read" }
    }));
    return;
  }
  if (path === "/api/v1/label-observations" && request.method === "GET") {
    labelObservationReadRequests += 1;
    const observations = [
      ["obs-quote-ui-1", "quote_commitment", "转人工复核", 0.91],
      ["obs-quote-ui-2", "quality_gate", "发布阻断", 0.84]
    ].map(([observationId, labelId, value, confidence]) => ({
      observation_id: observationId,
      extraction_run_id: labelExtractionRunId,
      subject_scope: "conversation",
      subject_key: "subject-quote-01",
      evidence_ref: { type: "trace", id: "evidence-quote-ui", sha256: "a".repeat(64) },
      label_version_id: labelStrongVersionId,
      raw_label: labelId,
      label_id: labelId,
      value,
      value_type: "categorical",
      source_family: "tagger-llm",
      source_type: "llm",
      model_version: "tagger-llm-2026.06",
      prompt_version_id: labelPromptDraftId,
      schema_version: "label-output-v1",
      raw_confidence: confidence,
      calibrated_confidence: confidence,
      input_sha256: "b".repeat(64),
      output_sha256: "c".repeat(64),
      status: "materialized",
      trace_id: "trace_label_fact_chain_ui"
    }));
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ data: { items: observations }, meta: { total: 2, trace_id: "trace_label_fact_chain_ui" } }));
    return;
  }
  if (path === "/api/v1/label-aggregation-policies/label-aggregation-v1.9.0-rc2" && request.method === "GET") {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        policy_version_id: "label-aggregation-v1.9.0-rc2",
        label_version_id: labelStrongVersionId,
        mode: "l1",
        status: "active"
      },
      meta: { trace_id: "trace_label_policy_ui", request_id: "ui-smoke-label-policy" }
    }));
    return;
  }
  if (path === "/api/v1/label-aggregation-runs" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      labelAggregationRunId = payload.aggregation_run_id;
      const run = {
        ...payload,
        status: "awaiting-review",
        observation_count: payload.observation_ids?.length ?? 0,
        aggregate_count: 3,
        input_sha256: "e".repeat(64),
        result_sha256: "f".repeat(64),
        aggregate_ids: ["LC-quote-01", "LC-quote-02", "LC-quote-high"],
        taxonomy_suggestion_ids: ["taxonomy-ui-1"],
        review_task_ids: ["review-LC-quote-01", "review-LC-quote-02", "review-taxonomy-ui-1"],
        trace_id: "trace_label_aggregation_ui"
      };
      labelAggregationRequests.push({ path, method: request.method, payload, correlationId: request.headers["x-correlation-id"] });
      response.writeHead(202, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: run, meta: { trace_id: run.trace_id, request_id: "ui-smoke-label-aggregation" } }));
    });
    return;
  }
  if (path.startsWith("/api/v1/label-aggregation-runs/") && request.method === "GET") {
    const runId = decodeURIComponent(path.split("/").pop() || "");
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        aggregation_run_id: runId,
        label_version_id: labelStrongVersionId,
        policy_version_id: "label-aggregation-v1.9.0-rc2",
        mode: "l1",
        status: "awaiting-review",
        observation_count: 2,
        aggregate_count: 3,
        input_sha256: "e".repeat(64),
        result_sha256: "f".repeat(64),
        aggregate_ids: ["LC-quote-01", "LC-quote-02", "LC-quote-high"],
        taxonomy_suggestion_ids: ["taxonomy-ui-1"],
        review_task_ids: ["review-LC-quote-01", "review-LC-quote-02", "review-taxonomy-ui-1"],
        trace_id: "trace_label_aggregation_ui"
      },
      meta: { trace_id: "trace_label_aggregation_ui", request_id: "ui-smoke-label-aggregation-read" }
    }));
    return;
  }
  if (path === "/api/v1/label-taxonomy-suggestions" && request.method === "GET") {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        items: [{
          suggestion_id: "taxonomy-ui-1",
          label_version_id: labelStrongVersionId,
          normalized_label: "unknown_quote_variant",
          raw_labels: ["超值锁价"],
          observation_ids: ["obs-quote-ui-1"],
          proposed_action: "create",
          canonical_target_label_id: null,
          status: "awaiting-review",
          review_task_id: "review-taxonomy-ui-1",
          trace_id: "trace_label_aggregation_ui"
        }]
      },
      meta: { total: 1, trace_id: "trace_label_aggregation_ui" }
    }));
    return;
  }
  if (path === "/api/v1/label-aggregates" && request.method === "GET") {
    labelAggregateReadRequests += 1;
    const aggregates = [
      ["LC-quote-01", "quote_commitment", "转人工复核", 0.93, "obs-quote-ui-1", "low"],
      ["LC-quote-02", "quote_commitment", "发布阻断", 0.87, "obs-quote-ui-2", "low"],
      ["LC-quote-high", "quote_amount_conflict", "金额冲突", 0.89, "obs-quote-ui-1", "high"]
    ].map(([aggregateId, labelId, value, score, observationId, riskLevel]) => ({
      aggregate_id: aggregateId,
      aggregation_run_id: labelAggregationRunId,
      label_version_id: labelStrongVersionId,
      policy_version_id: "label-aggregation-v1.9.0-rc2",
      subject_scope: "conversation",
      subject_key: "subject-quote-01",
      label_id: labelId,
      value_type: "categorical",
      value,
      score,
      margin: 0.08,
      risk_level: riskLevel,
      decision: "require-review",
      status: "awaiting-review",
      reason_codes: ["SOURCE_CONFLICT"],
      deterministic_hash: "d".repeat(64),
      review_task_id: `review-${aggregateId}`,
      trace_id: "trace_label_fact_chain_ui",
      members: [{
        aggregate_member_id: `member-${aggregateId}`,
        observation_id: observationId,
        included: true,
        source_family: "tagger-llm",
        evidence_sha256: "a".repeat(64),
        calibrated_confidence: score,
        contribution_score: 1.2
      }]
    }));
    aggregates.push({
      ...aggregates[0],
      aggregate_id: "LC-stale-same-subject",
      aggregation_run_id: "label-aggregation-stale",
      review_task_id: "review-LC-stale-same-subject"
    });
    aggregates.forEach((aggregate) => {
      if (!labelReviewTasks.has(aggregate.review_task_id)) {
        labelReviewTasks.set(aggregate.review_task_id, {
          id: aggregate.review_task_id,
          review_task_id: aggregate.review_task_id,
          status: "pending",
          queue: aggregate.risk_level === "high" ? "high_risk" : "low_risk_sample",
          risk_level: aggregate.risk_level,
          review_mode: aggregate.risk_level === "high" ? "double-blind" : "single",
          required_reviews: aggregate.risk_level === "high" ? 2 : 1,
          target_refs: [{ type: "label_aggregate", id: aggregate.aggregate_id }],
          trace_id: aggregate.trace_id
        });
      }
    });
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ data: { items: aggregates }, meta: { total: 2, trace_id: "trace_label_fact_chain_ui" } }));
    return;
  }
  if (path === "/api/v1/human-review-decision-batches" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      const results = (payload.items || []).map((item, index) => {
        const task = labelReviewTasks.get(item.review_task_id);
        const aggregateId = task?.target_refs?.[0]?.id;
        if (index === 0 && task) {
          labelReviewTasks.set(item.review_task_id, { ...task, status: item.decision, decision: item.decision });
          return {
            review_task_id: item.review_task_id,
            aggregate_id: aggregateId,
            status: "success",
            decision_id: `decision-batch-${index + 1}`,
            decision: item.decision
          };
        }
        return {
          review_task_id: item.review_task_id,
          aggregate_id: aggregateId,
          status: "skipped",
          reason_code: "BATCH_SERVER_POLICY_RECHECK"
        };
      });
      const receipt = {
        batch_id: "hrb-ui-partial-1",
        status: "partial",
        cohort: { label_id: "quote_commitment", risk_level: "low", policy_version_id: "label-aggregation-v1.9.0-rc2" },
        counts: {
          success: results.filter((item) => item.status === "success").length,
          skipped: results.filter((item) => item.status === "skipped").length,
          failed: results.filter((item) => item.status === "failed").length
        },
        results,
        trace_id: "trace_label_batch_ui"
      };
      labelReviewRequests.push({ path, method: request.method, payload, receipt });
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: receipt, meta: { trace_id: receipt.trace_id, request_id: "ui-smoke-label-review-batch" } }));
    });
    return;
  }
  if (path === "/api/v1/label-aggregates/LC-quote-high/review-submissions" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      const receipt = {
        target_id: "LC-quote-high",
        aggregate_id: "LC-quote-high",
        submission_id: "aggregate-review-sealed-ui-1",
        review_task_id: "review-LC-quote-high",
        status: "in-review",
        received_reviews: 1,
        trace_id: "trace_aggregate_review_ui"
      };
      labelSpecializedReviewRequests.push({ path, method: request.method, payload, correlationId: request.headers["x-correlation-id"] });
      response.writeHead(201, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: receipt, meta: { trace_id: receipt.trace_id, request_id: "ui-smoke-aggregate-review" } }));
    });
    return;
  }
  if (path === "/api/v1/label-taxonomy-suggestions/taxonomy-ui-1/review-submissions" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      const receipt = {
        target_id: "taxonomy-ui-1",
        suggestion_id: "taxonomy-ui-1",
        submission_id: "taxonomy-review-sealed-ui-1",
        review_task_id: "review-taxonomy-ui-1",
        status: "in-review",
        received_reviews: 1,
        trace_id: "trace_taxonomy_review_ui"
      };
      labelSpecializedReviewRequests.push({ path, method: request.method, payload, correlationId: request.headers["x-correlation-id"] });
      response.writeHead(201, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: receipt, meta: { trace_id: receipt.trace_id, request_id: "ui-smoke-taxonomy-review" } }));
    });
    return;
  }
  if (path === "/api/v1/label-optimization-runs" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      const runId = String(payload.optimization_run_id || "");
      const labelVersion = labelVersionRequests.find((item) => item.id === payload.label_version_id);
      const promptDraft = labelPromptVersionRequests.find((item) => item.id === payload.prompt_version_id);
      const correlationId = request.headers["x-correlation-id"];
      if (
        !runId ||
        labelOptimizationRuns.has(runId) ||
        !labelVersion ||
        !promptDraft ||
        promptDraft.labelVersionId !== labelVersion.id ||
        correlationId !== labelVersion.traceId
      ) {
        response.writeHead(409, { "Content-Type": "application/json" });
        response.end(JSON.stringify({
          error: {
            code: "LABEL_OPTIMIZATION_BINDING_CONFLICT",
            message: "OptimizationRun 必须唯一绑定同轮 LabelVersion/PromptVersion/root trace"
          }
        }));
        return;
      }
      const sequence = labelOptimizationRequests.length + 1;
      const candidateId = `prompt-version-candidate-ui-${sequence}`;
      const traceId = `trace_label_optimization_ui_${sequence}`;
      const candidate = {
        id: candidateId,
        candidate_id: candidateId,
        prompt_version_id: candidateId,
        parent_version_id: promptDraft.id,
        source_prompt_version_id: promptDraft.id,
        label_version_id: labelVersion.id,
        optimization_run_id: runId,
        source_run_id: runId,
        status: "awaiting-review",
        initial_status: "awaiting-review",
        review_task_id: `review-${candidateId}`,
        sealed_review_count: 0,
        review_submission_count: 0,
        adjudication_count: 0,
        root_trace_id: labelVersion.traceId,
        trace_id: traceId
      };
      const run = {
        ...payload,
        id: runId,
        run_id: runId,
        status: "success",
        stage: "awaiting-review",
        business_status: "awaiting-review",
        prompt_candidate_ids: [candidateId],
        trace_id: traceId
      };
      labelPromptCandidates.set(candidateId, candidate);
      labelOptimizationRuns.set(runId, run);
      labelOptimizationRequests.push({
        path,
        method: request.method,
        payload,
        runId,
        candidateId,
        candidatePromptVersionId: candidateId,
        labelVersionId: labelVersion.id,
        promptDraftId: promptDraft.id,
        rootTraceId: labelVersion.traceId,
        correlationId
      });
      response.writeHead(202, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: run,
        meta: { trace_id: traceId, request_id: `ui-smoke-label-optimization-${sequence}` }
      }));
    });
    return;
  }
  if (path.startsWith("/api/v1/label-optimization-runs/") && request.method === "GET") {
    const runId = decodeURIComponent(path.split("/").pop() || "");
    const run = labelOptimizationRuns.get(runId);
    if (!run) {
      response.writeHead(404, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: { code: "NOT_FOUND", message: runId } }));
      return;
    }
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: run,
      meta: { trace_id: run.trace_id, request_id: "ui-smoke-label-optimization-read" }
    }));
    return;
  }
  if (path.startsWith("/api/v1/prompt-version-candidates/") && request.method === "GET") {
    const candidateId = decodeURIComponent(path.split("/").pop() || "");
    const candidate = labelPromptCandidates.get(candidateId);
    if (!candidate) {
      response.writeHead(404, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: { code: "NOT_FOUND", message: candidateId } }));
      return;
    }
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: candidate,
      meta: { trace_id: candidate.trace_id, request_id: "ui-smoke-prompt-candidate-read" }
    }));
    return;
  }
  if (path.startsWith("/api/v1/prompt-version-candidates/") && path.endsWith("/review-submissions") && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      const candidateId = decodeURIComponent(path.split("/").at(-2) || "");
      const candidate = labelPromptCandidates.get(candidateId);
      if (!candidate || candidate.status !== "awaiting-review") {
        response.writeHead(409, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ error: { code: "PROMPT_REVIEW_STATE_CONFLICT", message: candidateId } }));
        return;
      }
      candidate.status = "awaiting-adjudication";
      candidate.sealed_review_count = 2;
      candidate.review_submission_count += 1;
      labelPromptCandidates.set(candidateId, candidate);
      const receipt = {
        candidate_id: candidateId,
        submission_id: `prompt-review-sealed-${candidateId}`,
        review_task_id: candidate.review_task_id,
        status: "awaiting-adjudication",
        received_reviews: 2,
        next_action: "independent-adjudication",
        trace_id: `trace_prompt_review_${candidateId}`
      };
      labelSpecializedReviewRequests.push({
        path,
        method: request.method,
        payload,
        candidateId,
        phase: "double-review",
        correlationId: request.headers["x-correlation-id"]
      });
      response.writeHead(201, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: receipt, meta: { trace_id: receipt.trace_id, request_id: "ui-smoke-prompt-review" } }));
    });
    return;
  }
  if (path.startsWith("/api/v1/prompt-version-candidates/") && path.endsWith("/adjudications") && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      const candidateId = decodeURIComponent(path.split("/").at(-2) || "");
      const candidate = labelPromptCandidates.get(candidateId);
      if (!candidate || candidate.status !== "awaiting-adjudication") {
        response.writeHead(409, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ error: { code: "PROMPT_ADJUDICATION_STATE_CONFLICT", message: candidateId } }));
        return;
      }
      candidate.status = "approved";
      candidate.adjudication_count += 1;
      labelPromptCandidates.set(candidateId, candidate);
      const receipt = {
        candidate_id: candidateId,
        adjudication_id: `prompt-adjudication-${candidateId}`,
        review_task_id: candidate.review_task_id,
        status: "approved",
        received_reviews: candidate.sealed_review_count,
        trace_id: `trace_prompt_adjudication_${candidateId}`
      };
      labelSpecializedReviewRequests.push({
        path,
        method: request.method,
        payload,
        candidateId,
        phase: "adjudication",
        correlationId: request.headers["x-correlation-id"]
      });
      response.writeHead(201, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: receipt, meta: { trace_id: receipt.trace_id, request_id: "ui-smoke-prompt-adjudication" } }));
    });
    return;
  }
  if (
    path.startsWith("/api/v1/audio-sessions/") &&
    path !== "/api/v1/audio-sessions/aggregations" &&
    request.method === "GET" &&
    !path.endsWith("/annotations")
  ) {
    const sessionId = decodeURIComponent(path.split("/").pop() || "");
    const session = listeningAudioSessions.get(sessionId);
    if (!session) {
      response.writeHead(404, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: { code: "AUDIO_SESSION_NOT_FOUND", message: sessionId } }));
      return;
    }
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ data: session, meta: { trace_id: session.trace_id, request_id: "ui-smoke-audio-session-read" } }));
    return;
  }
  if (path === "/api/v1/human-review-tasks" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      const id = payload.id || payload.review_task_id;
      const task = {
        ...payload,
        id,
        review_task_id: id,
        status: "pending",
        trace_id: `trace_${id}`
      };
      labelReviewTasks.set(id, task);
      labelReviewRequests.push({ path, method: request.method, payload });
      response.writeHead(201, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: task, meta: { trace_id: task.trace_id, request_id: "ui-smoke-label-review-create" } }));
    });
    return;
  }
  if (path.startsWith("/api/v1/human-review-tasks/") && path.endsWith("/decisions") && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      const id = decodeURIComponent(path.split("/").at(-2) || "");
      const task = labelReviewTasks.get(id);
      if (!task) {
        response.writeHead(404, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ error: { code: "REVIEW_TASK_NOT_FOUND", message: id } }));
        return;
      }
      const updated = { ...task, status: payload.decision, decision: payload.decision };
      labelReviewTasks.set(id, updated);
      labelReviewRequests.push({ path, method: request.method, payload, candidate_id: task.candidate_id ?? task.target_refs?.[0]?.id });
      response.writeHead(201, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: { id, status: payload.decision, trace_id: `trace_decision_${id}` }, meta: { trace_id: `trace_decision_${id}`, request_id: "ui-smoke-label-review-decision" } }));
    });
    return;
  }
  if (path.startsWith("/api/v1/human-review-tasks/") && request.method === "GET") {
    const id = decodeURIComponent(path.split("/").pop() || "");
    const task = labelReviewTasks.get(id);
    if (!task) {
      response.writeHead(404, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: { code: "REVIEW_TASK_NOT_FOUND", message: id } }));
      return;
    }
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ data: task, meta: { trace_id: task.trace_id, request_id: "ui-smoke-label-review-read" } }));
    return;
  }
  if (path === "/api/v1/label-versions" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      labelVersionSequence += 1;
      const id = `label-v1-9-ui-${labelVersionSequence}`;
      const traceId = `trace_label_version_ui_${labelVersionSequence}`;
      labelVersionStatus = "candidate";
      labelStrongVersionId = id;
      labelVersionRequests.push({
        path,
        method: request.method,
        payload,
        id,
        traceId,
        idempotencyKey: request.headers["idempotency-key"]
      });
      response.writeHead(201, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: { id, label_version_id: id, status: "candidate", payload, trace_id: traceId },
        meta: { trace_id: traceId, request_id: `ui-smoke-label-version-create-${labelVersionSequence}` }
      }));
    });
    return;
  }
  if (path === "/api/v1/prompt-versions" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      const labelVersion = labelVersionRequests.find((item) => item.id === payload.label_version_id);
      const correlationId = request.headers["x-correlation-id"];
      const requestedId = String(payload.prompt_version_id || "");
      if (
        !requestedId ||
        labelPromptVersionRequests.some((item) => item.id === requestedId) ||
        !labelVersion ||
        correlationId !== labelVersion.traceId
      ) {
        response.writeHead(409, { "Content-Type": "application/json" });
        response.end(JSON.stringify({
          error: {
            code: "PROMPT_VERSION_BINDING_CONFLICT",
            message: "PromptVersion 必须使用唯一强 ID 并绑定当前 LabelVersion/root trace"
          }
        }));
        return;
      }
      labelPromptDraftSequence += 1;
      labelPromptDraftId = requestedId;
      const traceId = `trace_prompt_version_ui_${labelPromptDraftSequence}`;
      labelPromptVersionRequests.push({
        path,
        method: request.method,
        payload,
        id: labelPromptDraftId,
        labelVersionId: labelVersion.id,
        traceId,
        correlationId,
        idempotencyKey: request.headers["idempotency-key"]
      });
      response.writeHead(201, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: {
          ...payload,
          id: labelPromptDraftId,
          prompt_version_id: labelPromptDraftId,
          status: "draft",
          trace_id: traceId
        },
        meta: { trace_id: traceId, request_id: `ui-smoke-prompt-version-create-${labelPromptDraftSequence}` }
      }));
    });
    return;
  }
  if (path === "/api/v1/eval-runs" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      const optimization = labelOptimizationRequests.find((item) => item.runId === payload.optimization_run_id);
      const candidate = labelPromptCandidates.get(String(payload.prompt_version_id || ""));
      const correlationId = request.headers["x-correlation-id"];
      if (
        !optimization ||
        !candidate ||
        candidate.status !== "approved" ||
        payload.label_version_id !== optimization.labelVersionId ||
        payload.prompt_version_id !== optimization.candidatePromptVersionId
      ) {
        response.writeHead(409, { "Content-Type": "application/json" });
        response.end(JSON.stringify({
          error: {
            code: "LABEL_EVAL_LOCK_CONFLICT",
            message: "EvalRun 必须锁定本轮已双复核/裁决的 LabelVersion/PromptVersion/OptimizationRun",
            detail: {
              payload,
              optimization,
              candidate,
              correlationId
            }
          }
        }));
        return;
      }
      labelEvalRunSequence += 1;
      const runId = `label-eval-ui-${labelEvalRunSequence}`;
      const run = {
        id: runId,
        run_id: runId,
        status: "queued",
        payload,
        poll_count: 0,
        fail_on_readback: labelEvalRunSequence === 2,
        trace_id: `trace_label_eval_${labelEvalRunSequence}`
      };
      labelEvalRuns.set(runId, run);
      labelEvalRequests.push({
        path,
        method: request.method,
        payload,
        runId,
        candidateId: candidate.candidate_id,
        correlationId,
        idempotencyKey: request.headers["idempotency-key"]
      });
      response.writeHead(202, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: run,
        meta: { trace_id: `trace_label_eval_${labelEvalRunSequence}`, request_id: "ui-smoke-label-eval" }
      }));
    });
    return;
  }
  if (path.startsWith("/api/v1/eval-runs/") && request.method === "GET") {
    const runId = decodeURIComponent(path.split("/").pop() || "");
    const run = labelEvalRuns.get(runId);
    if (!run) {
      response.writeHead(404, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: { code: "NOT_FOUND", message: runId } }));
      return;
    }
    run.poll_count += 1;
    if (run.fail_on_readback) {
      run.status = "failed";
      run.failure_reason = "模拟评测 worker 失败";
    } else if (run.poll_count === 1) {
      run.status = "running";
    } else {
      run.status = "success";
      run.metrics = [
        { metric: "macro-F1", current: "0.86", candidate: "0.90", delta: "+0.04", verdict: "passed" },
        { metric: "JSON 合法率", current: "99.5%", candidate: "99.8%", delta: "+0.3pp", verdict: "passed" },
        { metric: "关键标签 Recall", current: "0.91", candidate: "0.91", delta: "0", verdict: "passed" }
      ];
    }
    labelEvalRuns.set(runId, run);
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: run,
      meta: { trace_id: run.trace_id, request_id: "ui-smoke-label-eval-read" }
    }));
    return;
  }
  if (path.startsWith("/api/v1/runs/label-eval-ui-") && path.endsWith("/retries") && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const sourceRunId = decodeURIComponent(path.split("/").at(-2) || "");
      const source = labelEvalRuns.get(sourceRunId);
      if (!source || source.status !== "failed") {
        response.writeHead(409, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ error: { code: "RUN_NOT_RETRYABLE", message: sourceRunId } }));
        return;
      }
      const retryPayload = body ? JSON.parse(body) : {};
      const retryRunId = `${sourceRunId}-retry-1`;
      const retryRun = {
        ...source,
        id: retryRunId,
        run_id: retryRunId,
        status: "queued",
        poll_count: 0,
        fail_on_readback: false,
        retry_of_run_id: sourceRunId,
        trace_id: `${source.trace_id}_retry`
      };
      labelEvalRuns.set(retryRunId, retryRun);
      labelEvalRetryRequests.push({
        path,
        method: request.method,
        payload: retryPayload,
        sourceRunId,
        retryRunId,
        idempotencyKey: request.headers["idempotency-key"]
      });
      response.writeHead(202, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: retryRun,
        meta: { trace_id: retryRun.trace_id, request_id: "ui-smoke-label-eval-retry" }
      }));
    });
    return;
  }
  if (path === "/api/v1/release-deployments" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      const deploymentId = String(payload.deployment_id || "");
      const evalRun = labelEvalRuns.get(String(payload.eval_run_id || ""));
      const evalPayload = evalRun?.payload || {};
      const optimization = labelOptimizationRequests.find((item) => item.runId === evalPayload.optimization_run_id);
      const candidate = labelPromptCandidates.get(String(payload.prompt_version_id || ""));
      const correlationId = request.headers["x-correlation-id"];
      if (
        !deploymentId ||
        labelReleaseDeployments.has(deploymentId) ||
        !evalRun ||
        evalRun.status !== "success" ||
        !optimization ||
        !candidate ||
        candidate.status !== "approved" ||
        payload.label_version_id !== evalPayload.label_version_id ||
        payload.prompt_version_id !== evalPayload.prompt_version_id ||
        correlationId !== optimization.rootTraceId
      ) {
        response.writeHead(409, { "Content-Type": "application/json" });
        response.end(JSON.stringify({
          error: {
            code: "RELEASE_BUNDLE_LOCK_CONFLICT",
            message: "ReleaseDeployment 必须锁定本轮成功 EvalRun 及其强版本"
          }
        }));
        return;
      }
      labelPublishSequence += 1;
      const status = labelPublishSequence === 1 ? "blocked" : "shadowing";
      const deployment = {
        ...payload,
        id: deploymentId,
        deployment_id: deploymentId,
        status,
        stage: status,
        rollout_percentage: 0,
        blocked_reasons: status === "blocked"
          ? [{ code: "RELEASE_FACTS_CHANGED", message: "发布事实已变化，请重新评测后提交" }]
          : [],
        trace_id: `trace_${deploymentId}`
      };
      labelReleaseDeployments.set(deploymentId, deployment);
      labelPublishRequests.push({
        path,
        method: request.method,
        payload,
        runId: deploymentId,
        optimizationRunId: optimization.runId,
        candidateId: candidate.candidate_id,
        correlationId
      });
      response.writeHead(201, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: deployment,
        meta: { trace_id: deployment.trace_id, request_id: "ui-smoke-release-create" }
      }));
    });
    return;
  }
  if (path.startsWith("/api/v1/release-deployments/") && path.endsWith("/monitor-samples") && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      const deploymentId = decodeURIComponent(path.split("/").at(-2) || "");
      const current = labelReleaseDeployments.get(deploymentId);
      if (!current) {
        response.writeHead(404, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ error: { code: "NOT_FOUND", message: deploymentId } }));
        return;
      }
      if (request.headers["x-auris-system-worker"] !== "ui-smoke-monitor" || payload.expected_status !== current.status) {
        response.writeHead(409, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ error: { code: "RELEASE_STATUS_CONFLICT", message: current.status } }));
        return;
      }
      const metrics = { ...(payload.metrics || {}), stable_window_complete: payload.stable_window_complete === true };
      const hardRegression = Number(metrics.json_valid_rate) < 0.995
        || Number(metrics.conflict_rate) > 0.05
        || Number(metrics.critical_recall_delta_pp) < -2
        || Number(metrics.human_override_delta_pp) >= 3
        || Number(metrics.cost_ratio) > 1.1
        || Number(metrics.latency_ratio) > 1.2;
      const updated = {
        ...current,
        status: hardRegression ? "rolled-back" : "monitoring",
        stage: hardRegression ? "rolled-back" : "monitoring",
        rollout_percentage: hardRegression ? 0 : current.rollout_percentage,
        monitor_metrics: { ...(current.monitor_metrics || {}), ...metrics },
        blocked_reasons: hardRegression
          ? [{ code: "JSON_VALID_RATE_HARD_REGRESSION", message: "在线监控指标触发自动回滚硬阈值" }]
          : []
      };
      labelReleaseDeployments.set(deploymentId, updated);
      labelMonitorSampleRequests.push({ path, method: request.method, payload, deploymentId });
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: updated,
        meta: { trace_id: updated.trace_id, request_id: "ui-smoke-release-monitor" }
      }));
    });
    return;
  }
  if (path.startsWith("/api/v1/release-deployments/") && path.endsWith("/transitions") && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      const deploymentId = decodeURIComponent(path.split("/").at(-2) || "");
      const current = labelReleaseDeployments.get(deploymentId);
      if (!current) {
        response.writeHead(404, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ error: { code: "NOT_FOUND", message: deploymentId } }));
        return;
      }
      if (Object.prototype.hasOwnProperty.call(payload, "monitor_metrics")) {
        response.writeHead(422, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ error: { code: "RELEASE_MONITOR_METRICS_SYSTEM_OWNED", message: "监控事实只能由 system monitor-samples 写入" } }));
        return;
      }
      if (payload.expected_status !== current.status) {
        response.writeHead(409, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ error: { code: "RELEASE_STATUS_CONFLICT", message: current.status } }));
        return;
      }
      const isPromote = payload.action === "promote";
      if (isPromote && (current.status !== "monitoring" || current.monitor_metrics?.stable_window_complete !== true)) {
        response.writeHead(409, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ error: { code: "RELEASE_MONITOR_GATE_BLOCKED", message: "缺少系统稳定窗口事实" } }));
        return;
      }
      const status = payload.action === "approve-gray" ? "gray-releasing" : "completed";
      const updated = {
        ...current,
        status,
        stage: status,
        rollout_percentage: status === "completed" ? 100 : 10
      };
      labelReleaseDeployments.set(deploymentId, updated);
      labelPublishRequests.push({ path, method: request.method, payload, runId: deploymentId });
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: updated,
        meta: { trace_id: updated.trace_id, request_id: "ui-smoke-release-transition" }
      }));
    });
    return;
  }
  if (path.startsWith("/api/v1/release-deployments/") && request.method === "GET") {
    const deploymentId = decodeURIComponent(path.split("/").pop() || "");
    const deployment = labelReleaseDeployments.get(deploymentId);
    if (!deployment) {
      response.writeHead(404, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: { code: "NOT_FOUND", message: deploymentId } }));
      return;
    }
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: deployment,
      meta: { trace_id: deployment.trace_id, request_id: "ui-smoke-release-read" }
    }));
    return;
  }
  if (path === "/api/v1/label-versions/label-v1-9-ui/publish" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      labelPublishSequence += 1;
      const payload = body ? JSON.parse(body) : {};
      const runId = labelPublishSequence === 1 ? "label-publish-blocked-ui" : "label-publish-published-ui";
      labelPublishRequests.push({ path, method: request.method, payload, runId });
      labelPublishRunPolls.set(runId, 0);
      response.writeHead(202, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: { id: runId, run_id: runId, status: "pending", trace_id: `trace_${runId}` },
        meta: { trace_id: `trace_${runId}`, request_id: "ui-smoke-label-publish" }
      }));
    });
    return;
  }
  if (path.startsWith("/api/v1/label-versions/") && path.endsWith("/evaluation-lock") && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      const labelVersionId = decodeURIComponent(path.split("/").at(-2) || "");
      const labelVersion = labelVersionRequests.find((item) => item.id === labelVersionId);
      const optimization = labelOptimizationRequests.find((item) => item.runId === payload.optimization_run_id);
      const candidate = labelPromptCandidates.get(String(payload.prompt_version_id || ""));
      const correlationId = request.headers["x-correlation-id"];
      const idempotencyKey = request.headers["idempotency-key"];
      if (
        !labelVersion ||
        !optimization ||
        !candidate ||
        candidate.status !== "approved" ||
        optimization.labelVersionId !== labelVersion.id ||
        optimization.candidatePromptVersionId !== candidate.candidate_id ||
        payload.expected_resource_version !== 1 ||
        payload.confirmation !== "lock-for-evaluation" ||
        correlationId !== labelVersion.traceId ||
        !idempotencyKey
      ) {
        response.writeHead(409, { "Content-Type": "application/json" });
        response.end(JSON.stringify({
          error: {
            code: "LABEL_EVALUATION_LOCK_CONFLICT",
            message: "评测锁必须绑定同轮已审批 Prompt 候选、LabelVersion、OptimizationRun 与 root trace"
          }
        }));
        return;
      }
      const snapshotSha256 = `${"0".repeat(63)}${labelVersionSequence.toString(16)}`;
      const lock = {
        label_version_id: labelVersion.id,
        status: "locked",
        resource_version: 1,
        label_resource_version: 1,
        prompt_version_id: candidate.candidate_id,
        model_version: payload.model_version,
        aggregation_policy_version_id: payload.aggregation_policy_version_id,
        eval_dataset_version_id: payload.eval_dataset_version_id,
        optimization_run_id: optimization.runId,
        snapshot_sha256: snapshotSha256,
        locked_at: "2026-07-17T08:00:00.000Z",
        locked_by: "u_admin_001",
        materialized: true,
        trace_id: labelVersion.traceId,
        next_action: "create-eval-run"
      };
      response.writeHead(201, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: lock,
        meta: { trace_id: labelVersion.traceId, request_id: `ui-smoke-label-evaluation-lock-${labelVersionSequence}` }
      }));
    });
    return;
  }
  if (path.startsWith("/api/v1/label-versions/") && request.method === "GET") {
    const labelVersionId = decodeURIComponent(path.split("/").pop() || "");
    const labelVersion = labelVersionRequests.find((item) => item.id === labelVersionId);
    if (!labelVersion) {
      response.writeHead(404, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: { code: "NOT_FOUND", message: labelVersionId } }));
      return;
    }
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        id: labelVersion.id,
        label_version_id: labelVersion.id,
        status: labelVersionStatus,
        resource_version: 1,
        trace_id: labelVersion.traceId
      },
      meta: { trace_id: labelVersion.traceId, request_id: "ui-smoke-label-version-read" }
    }));
    return;
  }
  if (path === "/api/v1/runs/label-publish-blocked-ui" && request.method === "GET") {
    const poll = (labelPublishRunPolls.get("label-publish-blocked-ui") || 0) + 1;
    labelPublishRunPolls.set("label-publish-blocked-ui", poll);
    const status = poll >= 2 ? "blocked" : "pending";
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        id: "label-publish-blocked-ui",
        run_id: "label-publish-blocked-ui",
        status,
        reason_code: status === "blocked" ? "RELEASE_FACTS_CHANGED" : undefined,
        failure_reason: status === "blocked" ? "发布事实已变化，请重新评测后提交" : undefined,
        trace_id: "trace_label_publish_blocked"
      },
      meta: { trace_id: "trace_label_publish_blocked", request_id: "ui-smoke-label-publish-blocked" }
    }));
    return;
  }
  if (path === "/api/v1/runs/label-publish-published-ui" && request.method === "GET") {
    const poll = (labelPublishRunPolls.get("label-publish-published-ui") || 0) + 1;
    labelPublishRunPolls.set("label-publish-published-ui", poll);
    const status = poll >= 2 ? "success" : "pending";
    if (status === "success") labelVersionStatus = "published";
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: { id: "label-publish-published-ui", run_id: "label-publish-published-ui", status, trace_id: "trace_label_publish_published" },
      meta: { trace_id: "trace_label_publish_published", request_id: "ui-smoke-label-publish-published" }
    }));
    return;
  }
  if (path === "/api/v1/hotword-statistics" && request.method === "GET") {
    hotwordReadRequests += 1;
    hotwordStatisticsRequests.push(request.url);
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        summary: {
          coverage_rate: 0.937,
          recall_rate: 0.881,
          error_rate: 0.119,
          false_boost_rate: 0.004,
          impacted_session_count: 53,
          trusted_expected_count: 21,
          correct_hit_count: 14,
          weighted_error_count: 7,
          recognized_hotword_count: 166,
          false_insertion_count: 1
        },
        discovery_summary: {
          annotation_correction_count: 0,
          unique_terms: 0,
          impacted_session_count: 0,
          threshold_met_term_count: 0,
          evidence_level: "discovery",
          eligible_for_release_gate: false
        },
        items: [
          {
            standard_term: "星越L",
            recognized_forms: ["星月L"],
            error_type: "misrecognition",
            expected_count: 21,
            human_correction_count: 4,
            error_rate: 0.333,
            evidence_level: "human-confirmed",
            evidence_confidence: 1,
            business_weight: 1.2,
            priority: 94,
            suspected: false,
            impacted_session_count: 8,
            badcase_ids: ["A-4107"],
            root_trace_id: "trace-hotword-a-4107"
          }
        ],
        discovery_items: [],
        dimensions: {
          date_from: "2025-05-20",
          date_to: "2025-05-28",
          store_id: "BJ-AURORA-001",
          provider: "auris-audio-stack",
          model_version: "audio-v2.3.1",
          hotword_pack_version_id: "hwpv-auto-sales-v1-8"
        }
      },
      meta: { trace_id: "trace_hotword_statistics", request_id: "ui-smoke-hotword-statistics" }
    }));
    return;
  }
  if (path === "/api/v1/badcases" && request.method === "GET") {
    hotwordReadRequests += 1;
    hotwordBadcaseReadRequests.push(request.url);
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        items: [
          {
            id: "A-4107",
            badcase_id: "A-4107",
            capability: "asr-hotword",
            standard_term: "星越L",
            recognized_text: "星月L",
            error_type: "misrecognition",
            expected_count: 18,
            correct_count: 11,
            weighted_error_count: 7,
            manual_correction_count: 3,
            evidence_ref: "storage-object:storage_evidence_af_131_hotword_diff",
            evidence_storage_object_id: "storage_evidence_af_131_hotword_diff",
            evidence_level: "human-confirmed",
            hotword_pack_version_id: "hwpv-auto-sales-v1-8",
            candidate_state: "confirmed",
            downstream_impact: {
              source_asset_key: "auris/model/asr_transcripts",
              source_data_asset_id: "AF-131",
              entities: ["vehicle-model"],
              labels: ["报价跟进"]
            },
            priority_score: 91,
            status: "pending-review",
            resource_version: badcaseResourceVersion,
            root_trace_id: "trace-hotword-a-4107"
          },
          {
            id: "A-OTHER-VERSION",
            badcase_id: "A-OTHER-VERSION",
            capability: "asr-hotword",
            standard_term: "星越L",
            recognized_text: "星悦L",
            error_type: "misrecognition",
            expected_count: 99,
            correct_count: 1,
            weighted_error_count: 98,
            manual_correction_count: 9,
            evidence_ref: "storage-object:storage_evidence_other_version",
            evidence_storage_object_id: "storage_evidence_other_version",
            evidence_level: "human-confirmed",
            hotword_pack_version_id: "hwpv-other-version",
            candidate_state: "confirmed",
            downstream_impact: { entities: ["wrong-version"] },
            priority_score: 99,
            status: "pending-review",
            resource_version: 7,
            root_trace_id: "trace-hotword-other-version"
          }
        ]
      },
      meta: { total: 2, limit: 20, next_cursor: null, trace_id: "trace_hotword_badcases" }
    }));
    return;
  }
  if (path === "/api/v1/hotword-packs" && request.method === "GET") {
    hotwordReadRequests += 1;
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        items: [{
          id: "hotword_pack_auto_sales",
          pack_id: "hotword_pack_auto_sales",
          name: "汽车销售热词包",
          language: "zh-CN",
          domain: "auto-sales",
          current_version_id: candidateVersionStatus === "published" ? candidateVersionId : "hwpv-auto-sales-v1-8",
          status: "active",
          resource_version: candidateVersionStatus === "published" ? 2 : 1
        }]
      },
      meta: { total: 1, limit: 50, next_cursor: null, trace_id: "trace_hotword_packs" }
    }));
    return;
  }
  if (path === "/api/v1/hotword-packs/hotword_pack_auto_sales/versions" && request.method === "GET") {
    hotwordReadRequests += 1;
    const baseline = {
      id: "hwpv-auto-sales-v1-8",
      version_id: "hwpv-auto-sales-v1-8",
      pack_id: "hotword_pack_auto_sales",
      version: "v1.8",
      baseline_version_id: null,
      status: "published",
      resource_version: 9,
      root_trace_id: "trace_hotword_pack_auto_sales"
    };
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: { items: candidateVersionCreated ? [baseline, hotwordVersionPayload()] : [baseline] },
      meta: { total: candidateVersionCreated ? 2 : 1, limit: 50, next_cursor: null, trace_id: "trace_hotword_versions" }
    }));
    return;
  }
  if (path === "/api/v1/hotword-pack-versions/hwpv-auto-sales-v1-8" && request.method === "GET") {
    hotwordReadRequests += 1;
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        id: "hwpv-auto-sales-v1-8",
        version_id: "hwpv-auto-sales-v1-8",
        pack_id: "hotword_pack_auto_sales",
        version: "v1.8",
        baseline_version_id: null,
        status: "published",
        resource_version: 9,
        root_trace_id: "trace_hotword_pack_auto_sales",
        items: [inheritedHotwordItem()]
      },
      meta: { trace_id: "trace_hotword_v18", request_id: "ui-smoke-hotword-v18" }
    }));
    return;
  }
  if (path === `/api/v1/hotword-pack-versions/${candidateVersionId}` && request.method === "GET") {
    hotwordReadRequests += 1;
    if (!candidateVersionCreated) {
      response.writeHead(404, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: { code: "HOTWORD_VERSION_NOT_FOUND", message: "候选版本不存在" } }));
      return;
    }
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: hotwordVersionPayload(),
      meta: { trace_id: "trace_hotword_candidate_get", request_id: "ui-smoke-hotword-candidate-get" }
    }));
    return;
  }
  if (path === "/api/v1/runs/hotword-build-ui-1" && request.method === "GET") {
    hotwordBuildPollRequests += 1;
    if (hotwordBuildPollRequests >= 2 && candidateVersionStatus === "validating") {
      candidateVersionStatus = "ready_for_eval";
      candidateResourceVersion += 1;
    }
    const complete = hotwordBuildPollRequests >= 2;
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        id: "hotword-build-ui-1",
        run_id: "hotword-build-ui-1",
        run_type: "hotword_build",
        status: complete ? "success" : "pending",
        hotword_pack_version_id: candidateVersionId,
        provider: "auris-audio-stack",
        version_status: complete ? "ready_for_eval" : "validating",
        root_trace_id: "trace_hotword_pack_auto_sales",
        trace_id: "trace_hotword_build"
      },
      meta: { trace_id: "trace_hotword_build", request_id: `ui-smoke-hotword-build-poll-${hotwordBuildPollRequests}` }
    }));
    return;
  }
  if (path === `/api/v1/runs/hotword-eval-ui-1` && request.method === "GET") {
    hotwordEvalPollRequests += 1;
    if (hotwordEvalPollRequests >= 2 && candidateVersionStatus === "evaluating") {
      candidateVersionStatus = "review_required";
      candidateResourceVersion += 1;
    }
    const complete = hotwordEvalPollRequests >= 2;
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        id: "hotword-eval-ui-1",
        run_id: "hotword-eval-ui-1",
        run_type: "hotword_eval",
        status: complete ? "success" : "pending",
        hotword_pack_version_id: candidateVersionId,
        provider: "auris-audio-stack",
        locked: complete,
        gate: complete ? { passed: true, blocked_reasons: [] } : null,
        baseline_metrics: complete ? {
          trusted_occurrences: 40,
          recall_rate: 0.881,
          false_boost_rate: 0.004,
          cer: 0.079,
          wer: 0.124,
          downstream_f1: 0.918,
          p95_latency_ms: 4800,
          cost_per_minute: 0.012
        } : null,
        candidate_metrics: complete ? {
          trusted_occurrences: 40,
          recall_rate: 0.934,
          false_boost_rate: 0.0042,
          cer: 0.0798,
          wer: 0.1251,
          downstream_f1: 0.917,
          p95_latency_ms: 4960,
          cost_per_minute: 0.0124
        } : null,
        version_status: complete ? "review_required" : "evaluating",
        root_trace_id: "trace_hotword_pack_auto_sales",
        trace_id: "trace_hotword_eval"
      },
      meta: { trace_id: "trace_hotword_eval", request_id: `ui-smoke-hotword-eval-poll-${hotwordEvalPollRequests}` }
    }));
    return;
  }
  if (path === "/api/v1/runs/hotword-analysis-ui-1" && request.method === "GET") {
    hotwordAnalysisPollRequests += 1;
    const complete = hotwordAnalysisPollRequests >= 2;
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        id: "hotword-analysis-ui-1",
        run_id: "hotword-analysis-ui-1",
        run_type: "hotword_analysis",
        status: complete ? "success" : "pending",
        root_trace_id: "trace_hotword_analysis",
        trace_id: "trace_hotword_analysis"
      },
      meta: { trace_id: "trace_hotword_analysis", request_id: `ui-smoke-hotword-analysis-poll-${hotwordAnalysisPollRequests}` }
    }));
    return;
  }
  if (path === "/api/v1/runs/hotword-publish-ui-1" && request.method === "GET") {
    hotwordPublishPollRequests += 1;
    const failed = hotwordPublishPollRequests >= 2;
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        id: "hotword-publish-ui-1",
        run_id: "hotword-publish-ui-1",
        run_type: "hotword_publish",
        status: failed ? "failed" : "pending",
        hotword_pack_version_id: candidateVersionId,
        task_version_id: null,
        root_trace_id: "trace_hotword_pack_auto_sales",
        trace_id: "trace_hotword_publish"
      },
      meta: { trace_id: "trace_hotword_publish", request_id: `ui-smoke-hotword-publish-poll-${hotwordPublishPollRequests}` }
    }));
    return;
  }
  if (path === "/api/v1/runs/hotword-publish-retry-ui-1" && request.method === "GET") {
    hotwordPublishRetryPollRequests += 1;
    if (hotwordPublishRetryPollRequests >= 2 && candidateVersionStatus === "approved") {
      candidateVersionStatus = "published";
      candidateResourceVersion += 1;
    }
    const complete = hotwordPublishRetryPollRequests >= 2;
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        id: "hotword-publish-retry-ui-1",
        run_id: "hotword-publish-retry-ui-1",
        run_type: "hotword_publish",
        status: complete ? "success" : "pending",
        hotword_pack_version_id: candidateVersionId,
        task_version_id: complete ? "task-version-hotword-ui-1" : null,
        root_trace_id: "trace_hotword_pack_auto_sales",
        trace_id: "trace_hotword_publish_retry"
      },
      meta: { trace_id: "trace_hotword_publish_retry", request_id: `ui-smoke-hotword-publish-retry-poll-${hotwordPublishRetryPollRequests}` }
    }));
    return;
  }
  if (path === "/api/v1/runs/hotword-publish-ui-1/retries" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      hotwordPublishRetryRequests += 1;
      hotwordWriteRequests.push({ path, method: request.method, payload });
      candidatePublishRunId = "hotword-publish-retry-ui-1";
      response.writeHead(202, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: {
          id: "hotword-publish-retry-ui-1",
          run_id: "hotword-publish-retry-ui-1",
          run_type: "hotword_publish",
          status: "pending",
          retry_of_run_id: "hotword-publish-ui-1",
          trace_id: "trace_hotword_publish_retry"
        },
        meta: { trace_id: "trace_hotword_publish_retry", request_id: "ui-smoke-hotword-publish-retry" }
      }));
    });
    return;
  }
  const hotwordWriteRoute =
    (path === "/api/v1/hotword-analysis-runs" && request.method === "POST") ||
    (path === "/api/v1/audio-sessions/S20250526-000131/annotations" && request.method === "POST") ||
    (path === "/api/v1/badcases/A-4107/decisions" && request.method === "POST") ||
    (path === "/api/v1/hotword-packs/hotword_pack_auto_sales/versions" && request.method === "POST") ||
    (path === `/api/v1/hotword-pack-versions/${candidateVersionId}/items` && request.method === "POST") ||
    (path === `/api/v1/hotword-pack-versions/${candidateVersionId}/items/hotword-item-xingyue-l` && request.method === "PATCH") ||
    (path === `/api/v1/hotword-pack-versions/${candidateVersionId}` && request.method === "PATCH") ||
    (path === `/api/v1/hotword-pack-versions/${candidateVersionId}/eval-runs` && request.method === "POST") ||
    (path === `/api/v1/hotword-pack-versions/${candidateVersionId}/publish` && request.method === "POST");
  if (hotwordWriteRoute) {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      hotwordWriteRequests.push({ path, method: request.method, payload });
      const isAnalysis = path.endsWith("hotword-analysis-runs");
      const isCorrection = path.endsWith("/annotations");
      const isDecision = path.endsWith("/decisions");
      const isVersionCreate = path.endsWith("hotword_pack_auto_sales/versions");
      const isItemCreate = path.endsWith("/items") && request.method === "POST";
      const isItemPatch = path.endsWith("/items/hotword-item-xingyue-l") && request.method === "PATCH";
      const isItem = isItemCreate || isItemPatch;
      const isVersionPatch = path === `/api/v1/hotword-pack-versions/${candidateVersionId}` && request.method === "PATCH";
      const isEval = path.endsWith("eval-runs");
      const isPublish = path.endsWith("publish");
      if (isDecision) badcaseResourceVersion += 1;
      if (isVersionCreate) {
        candidateVersionCreated = true;
        candidateVersionStatus = "draft";
        candidateResourceVersion = 1;
        candidateBuildRunId = null;
        candidateEvalRunId = null;
        candidatePublishRunId = null;
        candidateItemResourceVersion = 1;
        candidateItemAliases = ["星越 L"];
      } else if (isItemPatch) {
        if (payload.expected_resource_version !== candidateItemResourceVersion) {
          response.writeHead(409, { "Content-Type": "application/json" });
          response.end(JSON.stringify({ error: { code: "RESOURCE_VERSION_CONFLICT", message: "候选词项乐观锁冲突" } }));
          return;
        }
        candidateItemAliases = Array.isArray(payload.aliases) ? payload.aliases : candidateItemAliases;
        candidateItemResourceVersion += 1;
        candidateResourceVersion += 1;
      } else if (isItemCreate) {
        candidateResourceVersion += 1;
      } else if (isVersionPatch) {
        if (payload.expected_resource_version !== candidateResourceVersion) {
          response.writeHead(409, { "Content-Type": "application/json" });
          response.end(JSON.stringify({ error: { code: "RESOURCE_VERSION_CONFLICT", message: "候选版本乐观锁冲突" } }));
          return;
        }
        candidateVersionStatus = payload.status ?? candidateVersionStatus;
        candidateResourceVersion += 1;
        if (payload.status === "validating") candidateBuildRunId = "hotword-build-ui-1";
        if (payload.status === "approved") candidateModelApprovedBy = "u_model_001";
      } else if (isEval) {
        if (payload.expected_resource_version !== candidateResourceVersion) {
          response.writeHead(409, { "Content-Type": "application/json" });
          response.end(JSON.stringify({ error: { code: "RESOURCE_VERSION_CONFLICT", message: "评测乐观锁冲突" } }));
          return;
        }
        candidateEvalRunId = "hotword-eval-ui-1";
        candidateVersionStatus = "evaluating";
        candidateResourceVersion += 1;
      } else if (isPublish) {
        if (payload.expected_resource_version !== candidateResourceVersion) {
          response.writeHead(409, { "Content-Type": "application/json" });
          response.end(JSON.stringify({ error: { code: "RESOURCE_VERSION_CONFLICT", message: "发布乐观锁冲突" } }));
          return;
        }
        candidatePublishRunId = "hotword-publish-ui-1";
      }
      const id = isCorrection
        ? "asr-correction-ui-1"
        : isAnalysis
        ? "hotword-analysis-ui-1"
        : isDecision
            ? "A-4107"
          : isVersionCreate || isVersionPatch
            ? candidateVersionId
          : isItem
            ? isItemPatch ? "hotword-item-xingyue-l" : "hotword-item-ui-1"
            : isEval
              ? "hotword-eval-ui-1"
              : "hotword-publish-ui-1";
      const traceId = isCorrection
        ? "trace_asr_correction_ui_1"
        : isAnalysis
        ? "trace_hotword_analysis"
        : isDecision
            ? "trace_hotword_decision"
          : isVersionCreate
            ? "trace_hotword_version_create"
          : isVersionPatch
            ? payload.status === "approved" ? "trace_hotword_approval" : "trace_hotword_version_patch"
          : isItem
            ? "trace_hotword_item"
            : isEval
              ? "trace_hotword_eval"
              : "trace_hotword_publish";
      response.writeHead(isAnalysis || isEval || isPublish ? 202 : isVersionPatch || isItemPatch ? 200 : 201, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: {
          ...(isVersionCreate || isVersionPatch ? hotwordVersionPayload() : {}),
          id,
          correction_id: isCorrection ? id : undefined,
          annotation_id: isCorrection ? payload.annotation_id : undefined,
          source_badcase_id: isCorrection ? "A-4107" : undefined,
          hotword_pack_version_id: isCorrection ? "hwpv-auto-sales-v1-8" : isEval ? candidateVersionId : undefined,
          stat_eligibility: isCorrection ? "discovery-only" : undefined,
          eligible_for_release_gate: isCorrection ? false : undefined,
          current_trace_id: isCorrection ? traceId : undefined,
          badcase_id: isDecision ? id : undefined,
          run_id: isAnalysis || isEval || isPublish ? id : undefined,
          status: isCorrection ? "submitted" : isDecision ? "pending-backflow" : isItem ? "created" : isVersionCreate || isVersionPatch ? candidateVersionStatus : "pending",
          trace_id: traceId,
          resource_version: isDecision
            ? badcaseResourceVersion
            : isVersionCreate || isVersionPatch
              ? candidateResourceVersion
              : isItemPatch
                ? candidateItemResourceVersion
                : undefined,
          locked: isEval ? false : undefined,
          gate: isEval ? null : undefined,
          version_status: isEval ? "evaluating" : undefined,
          task_version_draft_id: undefined
        },
        meta: { trace_id: traceId, request_id: `ui-smoke-${id}` }
      }));
    });
    return;
  }
  if (path === "/api/v1/task-versions/task-version-hotword-ui-1" && request.method === "GET") {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        id: "task-version-hotword-ui-1",
        task_version_id: "task-version-hotword-ui-1",
        task_type_id: "audio-intelligence",
        status: hotwordTaskVersionStatus,
        execution_mode: "production",
        provider: "auris-audio-stack",
        hotword_pack_version_id: candidateVersionId,
        language: "zh-CN",
        audio_intelligence: {
          execution_mode: "production",
          provider: "auris-audio-stack",
          hotword_pack_version_id: candidateVersionId,
          language: "zh-CN"
        },
        source: "hotword_pack_publish",
        source_publish_run_id: "hotword-publish-ui-1",
        root_trace_id: "trace_hotword_pack_auto_sales"
      },
      meta: { trace_id: "trace_hotword_pack_auto_sales", request_id: "ui-smoke-hotword-task-version" }
    }));
    return;
  }
  if (path === "/api/v1/task-versions/task-version-hotword-ui-1/publish" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      taskReleaseRequests.push({ path, method: request.method, payload });
      response.writeHead(202, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: {
          id: "task-version-publish-ui-1",
          run_id: "task-version-publish-ui-1",
          run_type: "task_version_publish",
          status: "blocked",
          task_version_id: "task-version-hotword-ui-1",
          trace_id: "trace_task_version_publish"
        },
        meta: { trace_id: "trace_task_version_publish", request_id: "ui-smoke-task-version-publish" }
      }));
    });
    return;
  }
  if (path === "/api/v1/runs/task-version-publish-ui-1/decisions" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      taskReleaseRequests.push({ path, method: request.method, payload });
      response.writeHead(201, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: {
          id: "task-version-publish-ui-1",
          run_id: "task-version-publish-ui-1",
          run_type: "task_version_publish",
          status: "pending",
          task_version_id: "task-version-hotword-ui-1",
          trace_id: "trace_task_version_publish"
        },
        meta: { trace_id: "trace_task_version_publish", request_id: "ui-smoke-task-version-publish-decision" }
      }));
    });
    return;
  }
  if (path === "/api/v1/runs/task-version-publish-ui-1" && request.method === "GET") {
    taskVersionPublishPollRequests += 1;
    hotwordTaskVersionStatus = "published";
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        id: "task-version-publish-ui-1",
        run_id: "task-version-publish-ui-1",
        run_type: "task_version_publish",
        status: "success",
        task_version_id: "task-version-hotword-ui-1",
        trace_id: "trace_task_version_publish"
      },
      meta: { trace_id: "trace_task_version_publish", request_id: `ui-smoke-task-version-publish-poll-${taskVersionPublishPollRequests}` }
    }));
    return;
  }
  if ((path === "/api/v1/task-versions" || path === "/api/v1/task-runs") && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      taskWriteRequests.push({ path, method: request.method, payload });
      const isRun = path.endsWith("task-runs");
      const id = isRun ? "task-run-ui-hotword-1" : "task-version-ui-hotword-1";
      response.writeHead(isRun ? 202 : 201, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: {
          id,
          run_id: isRun ? id : undefined,
          task_version_id: isRun ? payload.task_version_id : id,
          status: isRun ? "pending" : "draft",
          trace_id: isRun ? "trace_task_run_hotword" : "trace_task_version_hotword"
        },
        meta: { trace_id: isRun ? "trace_task_run_hotword" : "trace_task_version_hotword", request_id: `ui-smoke-${id}` }
      }));
    });
    return;
  }
  if (
    decodeURIComponent(path) === "/api/v1/data-assets/auris/model/asr_transcripts/materializations" &&
    request.method === "GET"
  ) {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        items: [{
          id: "mat_asr_20250526_122300",
          materialization_id: "mat_asr_20250526_122300",
          asset_key: "auris/model/asr_transcripts",
          status: "success",
          trace_id: "trace_asr_source_materialization"
        }]
      },
      meta: { trace_id: "trace_asr_materializations", request_id: "ui-smoke-asr-materializations" }
    }));
    return;
  }
  if (path.startsWith("/api/v1/data-assets/") && path.endsWith("/backfills") && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = body ? JSON.parse(body) : {};
      assetBackfillRequests.push({ path, method: request.method, payload, taskVersionStatus: hotwordTaskVersionStatus });
      response.writeHead(202, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        data: {
          id: "asset-backfill-hotword-ui-1",
          run_id: "asset-backfill-hotword-ui-1",
          run_type: "asset_backfill",
          status: "pending",
          asset_key: "auris/model/asr_transcripts",
          trace_id: "trace_asset_backfill_hotword"
        },
        meta: { trace_id: "trace_asset_backfill_hotword", request_id: "ui-smoke-asset-backfill-hotword" }
      }));
    });
    return;
  }
  if (path === "/api/v1/runs/asset-backfill-hotword-ui-1" && request.method === "GET") {
    assetBackfillPollRequests += 1;
    const complete = assetBackfillPollRequests >= 2;
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      data: {
        id: "asset-backfill-hotword-ui-1",
        run_id: "asset-backfill-hotword-ui-1",
        run_type: "asset_backfill",
        status: complete ? "success" : "pending",
        asset_key: "auris/model/asr_transcripts",
        root_trace_id: "trace_hotword_pack_auto_sales",
        trace_id: "trace_asset_backfill_hotword"
      },
      meta: { trace_id: "trace_asset_backfill_hotword", request_id: `ui-smoke-asset-backfill-poll-${assetBackfillPollRequests}` }
    }));
    return;
  }
  if (path === "/api/v1/exports" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      exportRequests += 1;
      const payload = body ? JSON.parse(body) : {};
      const runId = `export_ui_smoke_${exportRequests}`;
      response.writeHead(202, { "Content-Type": "application/json" });
      response.end(
        JSON.stringify({
          data: {
            id: runId,
            run_id: runId,
            run_type: "export",
            status: "pending",
            target: payload.target,
            object_id: payload.object_id,
            trace_id: "trace_ui_smoke_export"
          },
          meta: {
            trace_id: "trace_ui_smoke_export",
            request_id: "ui-smoke-export"
          }
        })
      );
    });
    return;
  }
  if (
    (path.startsWith("/api/v1/runs/") || path.startsWith("/api/v1/task-runs/")) &&
    request.method === "GET"
  ) {
    const runId = decodeURIComponent(path.split("/").pop() || "run_pending");
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(
      JSON.stringify({
        data: {
          id: runId,
          run_id: runId,
          run_type: runId.startsWith("export_") ? "export" : "task_run",
          status: "pending",
          trace_id: "trace_ui_smoke_export"
        },
        meta: {
          trace_id: "trace_ui_smoke_export_detail",
          request_id: "ui-smoke-run-detail"
        }
      })
    );
    return;
  }
  const fixture = projectionFixtures[path];
  if (fixture) {
    projectionHits.add(path);
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(
      JSON.stringify({
        ...fixture,
        meta: {
          trace_id: `trace_ui_smoke_${path.replace(/[^a-z0-9]+/gi, "_").replace(/^_|_$/g, "")}`,
          request_id: "ui-smoke"
        }
      })
    );
    return;
  }
  response.writeHead(404, { "Content-Type": "application/json" });
  response.end(JSON.stringify({ error: "ui-smoke-stub only supports /healthz" }));
});

await new Promise((resolve) => {
  bffStub.listen(0, "127.0.0.1", resolve);
});
const bffAddress = bffStub.address();
if (typeof bffAddress !== "object" || bffAddress === null) {
  throw new Error("Failed to start UI smoke BFF stub");
}
process.env.VITE_API_PROXY_TARGET = `http://127.0.0.1:${bffAddress.port}`;

const server = await createServer({
  root,
  logLevel: "error",
  server: {
    host: "127.0.0.1",
    port,
    strictPort: Boolean(configuredPort)
  }
});

await server.listen();
const serverAddress = server.httpServer?.address();
const actualPort =
  typeof serverAddress === "object" && serverAddress !== null ? serverAddress.port : port;
const baseUrl = server.resolvedUrls?.local?.[0] ?? `http://127.0.0.1:${actualPort}/`;

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
const browserErrors = [];
const requestFailures = [];
const failedResponses = [];
page.on("pageerror", (error) => browserErrors.push(error.message));
page.on("console", (message) => {
  if (message.type() === "error") browserErrors.push(message.text());
});
page.on("requestfailed", (request) => {
  requestFailures.push({
    error: request.failure()?.errorText ?? "unknown request failure",
    method: request.method(),
    url: request.url()
  });
});
page.on("response", (response) => {
  if (response.status() >= 400) {
    failedResponses.push({
      status: response.status(),
      method: response.request().method(),
      url: response.url()
    });
  }
});

try {
  await page.goto(baseUrl, { waitUntil: "networkidle", timeout: 30000 });
  if (await page.locator("button.auth-submit").count()) {
    await page.getByPlaceholder("name@company.com").fill("model@auris.local");
    await page.getByPlaceholder("至少 6 位").fill("auris-demo");
    await page.locator("button.auth-submit").click();
  }
  await expectBodyText(page, "运营首页");

  const visited = [];
  for (const check of moduleChecks) {
    await openModule(page, check.nav, check.title);
    if (check.nav === "首页") {
      await expectMetricText(page, "9,421");
      await expectMetricText(page, "319");
      await expectMetricText(page, "17");
      await expectMetricText(page, "后端投影");
    }
    if (check.nav === "调听") {
      await assertListeningTranscriptLayout(page);
    }
    visited.push(check.nav);
    for (const tab of check.tabs ?? []) {
      await openTab(page, tab);
    }
  }

  await assertProjectionSourceStates(page, failedResponses, browserErrors);
  await runLabelGovernanceTruthSmoke(page);
  await runHotwordGovernanceSmoke(page);
  await runModalCloseSmoke(page);
  await runModuleCommandSmoke(page);
  assert(browserErrors.length === 0, "浏览器控制台存在错误", { browserErrors, failedResponses });
  assert(requestFailures.length === 0, "UI smoke 存在网络请求失败", { requestFailures });
  assert(failedResponses.length === 0, "UI smoke 存在失败响应", { failedResponses });
  const missingProjectionHits = Object.keys(projectionFixtures).filter(
    (path) => !projectionHits.has(path)
  );
  assert(missingProjectionHits.length === 0, "UI smoke 未命中全部 BFF 投影", {
    missingProjectionHits,
    projectionHits: [...projectionHits]
  });
  assert(exportRequests === 1, "UI smoke 未通过全局导出创建后端导出运行", { exportRequests });
  assert(authLoginRequests === 2, "UI smoke 未完成模型负责人到项目管理员的分权登录", { authLoginRequests });
  assert(authEmails.join(",") === "model@auris.local,demo.operator@auris.local", "UI smoke 登录身份顺序不符合审批 RBAC", { authEmails });
  assert(hotwordReadRequests >= 1, "UI smoke 未读取热词统计投影", { hotwordReadRequests });
  const labelTaskCreates = labelReviewRequests.filter((item) => item.path === "/api/v1/human-review-tasks" && item.method === "POST");
  assert(labelTaskCreates.length === 0, "非 demo 模式错误创建了通用 HumanReviewTask", { labelReviewRequests });
  const labelBatchDecisions = labelReviewRequests.filter((item) => item.path === "/api/v1/human-review-decision-batches");
  assert(
    labelBatchDecisions.length === 1 && labelBatchDecisions[0].receipt.status === "partial" && labelBatchDecisions[0].receipt.counts.success === 1 && labelBatchDecisions[0].receipt.counts.skipped === 1,
    "批量决策未保留逐项 success/skipped 回执",
    { labelReviewRequests }
  );
  const labelDecisions = labelReviewRequests.filter((item) => item.path.endsWith("/decisions"));
  assert(
    labelDecisions.length === 1 && labelDecisions[0].candidate_id === "LC-quote-02" && labelDecisions[0].path.includes("review-LC-quote-02"),
    "低风险键盘保存并下一条串用了其他候选的审核任务",
    { labelReviewRequests }
  );
  assert(
    labelVersionRequests.length === 4 &&
      new Set(labelVersionRequests.map((item) => item.id)).size === 4 &&
      new Set(labelVersionRequests.map((item) => item.traceId)).size === 4 &&
      labelVersionRequests.every((item) => item.idempotencyKey) &&
      new Set(labelVersionRequests.map((item) => item.idempotencyKey)).size === 4,
    "每次保存必须创建唯一 LabelVersion/root trace/幂等意图",
    { labelVersionRequests }
  );
  assert(
    labelPromptVersionRequests.length === 4 &&
      new Set(labelPromptVersionRequests.map((item) => item.id)).size === 4 &&
      labelPromptVersionRequests.every((item, index) =>
        item.labelVersionId === labelVersionRequests[index].id &&
        item.correlationId === labelVersionRequests[index].traceId &&
        Boolean(item.idempotencyKey)
      ) &&
      new Set(labelPromptVersionRequests.map((item) => item.idempotencyKey)).size === 4,
    "每次 PromptVersion 必须唯一绑定同轮 LabelVersion/root trace",
    { labelVersionRequests, labelPromptVersionRequests }
  );
  assert(
    labelOptimizationRequests.length === 3 &&
      new Set(labelOptimizationRequests.map((item) => item.runId)).size === 3 &&
      new Set(labelOptimizationRequests.map((item) => item.candidateId)).size === 3 &&
      labelOptimizationRequests.every((item, index) =>
        item.labelVersionId === labelVersionRequests[index + 1].id &&
        item.promptDraftId === labelPromptVersionRequests[index + 1].id &&
        item.payload.label_version_id === item.labelVersionId &&
        item.payload.prompt_version_id === item.promptDraftId &&
        item.correlationId === item.rootTraceId
      ),
    "每轮 OptimizationRun/Candidate 未唯一绑定本轮 LabelVersion/PromptVersion",
    { labelVersionRequests, labelPromptVersionRequests, labelOptimizationRequests }
  );
  const promptCandidates = labelOptimizationRequests.map((item) => labelPromptCandidates.get(item.candidateId));
  assert(
    promptCandidates.every((candidate, index) =>
      candidate?.initial_status === "awaiting-review" &&
      candidate.status === "approved" &&
      candidate.label_version_id === labelOptimizationRequests[index].labelVersionId &&
      candidate.parent_version_id === labelOptimizationRequests[index].promptDraftId &&
      candidate.prompt_version_id === labelOptimizationRequests[index].candidatePromptVersionId &&
      candidate.optimization_run_id === labelOptimizationRequests[index].runId &&
      candidate.sealed_review_count === 2 &&
      candidate.review_submission_count === 1 &&
      candidate.adjudication_count === 1
    ),
    "每轮 PromptVersionCandidate 必须重置审批并完成双复核/独立裁决",
    { promptCandidates, labelOptimizationRequests }
  );
  const initialClosedLoopReviews = labelSpecializedReviewRequests.filter((item) => !item.candidateId);
  assert(
    initialClosedLoopReviews.some((item) => item.path.endsWith("/label-aggregates/LC-quote-high/review-submissions")) &&
      initialClosedLoopReviews.some((item) => item.path.endsWith("/label-taxonomy-suggestions/taxonomy-ui-1/review-submissions")) &&
      initialClosedLoopReviews.every((item) => item.correlationId === labelVersionRequests[0].traceId),
    "高风险 Aggregate/Taxonomy 未通过专用双盲接口或未沿用初始闭环 root trace",
    { initialClosedLoopReviews, labelVersionRequests }
  );
  assert(
    labelOptimizationRequests.every((item) => {
      const candidateReviews = labelSpecializedReviewRequests.filter((review) => review.candidateId === item.candidateId);
      return candidateReviews.length === 2 &&
        candidateReviews.some((review) => review.phase === "double-review" && review.path.endsWith(`/${item.candidateId}/review-submissions`)) &&
        candidateReviews.some((review) => review.phase === "adjudication" && review.path.endsWith(`/${item.candidateId}/adjudications`)) &&
        candidateReviews.every((review) => review.correlationId === item.rootTraceId);
    }),
    "每轮 Prompt 候选未通过本轮 root trace 执行双复核与裁决",
    { labelSpecializedReviewRequests, labelOptimizationRequests }
  );
  assert(
    labelAggregationRequests.length === 0,
    "ExtractionRun 已返回 aggregation_run_id 时前端仍重复创建 AggregationRun",
    { labelAggregationRequests }
  );
  assert(labelExtractionRunId && labelObservationReadRequests >= 1 && labelAggregateReadRequests >= 1, "标签抽取完成后未闭环读回 Observation/Aggregate", {
    labelExtractionRunId,
    labelObservationReadRequests,
    labelAggregateReadRequests
  });
  assert(labelEvalRequests.length === 3, "EvalRun 重复提交或缺少回滚后的重新评测", { labelEvalRequests });
  assert(
    labelEvalRequests.every((item, index) => {
      const optimization = labelOptimizationRequests[index];
      return item.payload.label_version_id === optimization.labelVersionId &&
        item.payload.prompt_version_id === optimization.candidatePromptVersionId &&
        item.payload.optimization_run_id === optimization.runId &&
        item.candidateId === optimization.candidateId;
    }),
    "EvalRun 未使用本轮锁定的 LabelVersion/候选 PromptVersion/OptimizationRun",
    { labelEvalRequests, labelOptimizationRequests }
  );
  assert(
    labelEvalRequests.every((item) => item.idempotencyKey) && new Set(labelEvalRequests.map((item) => item.idempotencyKey)).size === 3,
    "每个 EvalRun 用户意图必须有独立稳定幂等键",
    { labelEvalRequests }
  );
  assert(
    labelEvalRetryRequests.length === 1 &&
      labelEvalRetryRequests[0].sourceRunId === "label-eval-ui-2" &&
      !labelEvalRetryRequests[0].payload.payload_overrides,
    "失败 EvalRun 未保持原锁定输入走 retry",
    { labelEvalRetryRequests }
  );
  assert(labelMonitorSampleRequests.length === 2, "系统稳定窗口与硬退化监控样本未完整上报", { labelMonitorSampleRequests });
  assert(labelPublishRequests.length === 6, "ReleaseDeployment 阻断恢复、自动回滚、稳定灰度和晋级请求数量异常", { labelPublishRequests });
  const labelDeploymentCreates = labelPublishRequests.filter((item) => item.path === "/api/v1/release-deployments");
  assert(
    labelDeploymentCreates.length === 3 &&
      new Set(labelDeploymentCreates.map((item) => item.runId)).size === 3 &&
      labelPublishRequests.filter((item) => item.payload.action === "approve-gray").length === 2 &&
      labelPublishRequests.some((item) => item.payload.action === "promote"),
    "标签发布未完成 Bundle → 10% gray → system monitoring → 自动回滚/人工晋级状态机",
    { labelPublishRequests }
  );
  const expectedDeploymentEvalRunIds = [
    labelEvalRequests[0].runId,
    labelEvalRetryRequests[0].retryRunId,
    labelEvalRequests[2].runId
  ];
  assert(
    labelDeploymentCreates.every((item, index) => {
      const optimization = labelOptimizationRequests[index];
      return item.payload.label_version_id === optimization.labelVersionId &&
        item.payload.prompt_version_id === optimization.candidatePromptVersionId &&
        item.payload.eval_run_id === expectedDeploymentEvalRunIds[index] &&
        item.optimizationRunId === optimization.runId &&
        item.candidateId === optimization.candidateId &&
        item.correlationId === optimization.rootTraceId;
    }),
    "ReleaseDeployment Bundle 未锁定本轮 LabelVersion/候选 PromptVersion/EvalRun",
    { labelDeploymentCreates, labelOptimizationRequests, expectedDeploymentEvalRunIds }
  );
  assert(
    labelPublishRequests.filter((item) => item.path.endsWith("/transitions")).every((item) => !Object.prototype.hasOwnProperty.call(item.payload, "monitor_metrics")),
    "人工发布转换不得注入任何在线监控事实",
    { labelPublishRequests }
  );
  for (const route of [
    "/api/v1/hotword-analysis-runs",
    "/api/v1/audio-sessions/S20250526-000131/annotations",
    "/api/v1/badcases/A-4107/decisions",
    "/api/v1/hotword-packs/hotword_pack_auto_sales/versions",
    `/api/v1/hotword-pack-versions/${candidateVersionId}/items/hotword-item-xingyue-l`,
    `/api/v1/hotword-pack-versions/${candidateVersionId}/eval-runs`,
    `/api/v1/hotword-pack-versions/${candidateVersionId}/publish`,
    "/api/v1/runs/hotword-publish-ui-1/retries"
  ]) {
    assert(hotwordWriteRequests.some((request) => request.path === route), `UI smoke 未调用热词闭环接口 ${route}`, { hotwordWriteRequests });
  }
  const analysisWrite = hotwordWriteRequests.find((request) => request.path === "/api/v1/hotword-analysis-runs");
  assert(analysisWrite?.payload?.date_from && analysisWrite?.payload?.date_to, "热词分析未使用 date_from/date_to 契约", analysisWrite);
  assert(analysisWrite?.payload?.store_id === "BJ-AURORA-001" && analysisWrite?.payload?.model_version === "audio-v2.3.1" && analysisWrite?.payload?.provider === "auris-audio-stack", "热词分析未发送稳定门店/模型/provider ID", analysisWrite);
  assert(!("start_date" in analysisWrite.payload) && !("evidence_threshold" in analysisWrite.payload) && !("source" in analysisWrite.payload), "热词分析包含后端 strict schema 禁止字段", analysisWrite);
  assert(hotwordStatisticsRequests.some((url) => url.includes("store_id=BJ-AURORA-001") && url.includes("model_version=audio-v2.3.1") && url.includes("provider=auris-audio-stack")), "热词统计未使用稳定筛选 ID", hotwordStatisticsRequests);
  assert(
    hotwordBadcaseReadRequests.some((url) => url.includes("hotword_pack_version_id=hwpv-auto-sales-v1-8")),
    "洞察热词统计恢复 Badcase 时未携带当前 hotword_pack_version_id",
    hotwordBadcaseReadRequests
  );
  assert(
    !hotwordWriteRequests.some((request) => request.path === "/api/v1/badcases" && request.method === "POST"),
    "ASR Diff 必须复用证据已绑定的 A-4107，不得制造重复 Badcase 血缘",
    hotwordWriteRequests
  );
  const correctionWrite = hotwordWriteRequests.find(
    (request) => request.path === "/api/v1/audio-sessions/S20250526-000131/annotations"
  );
  assert(
    correctionWrite?.payload?.annotation_kind === "asr-transcript-correction" &&
      correctionWrite.payload.confirmation === "record_correction" &&
      correctionWrite.payload.evidence_storage_object_id === "storage_evidence_af_131_hotword_diff" &&
      correctionWrite.payload.hotword_pack_version_id === "hwpv-auto-sales-v1-8",
    "ASR 标注修正未绑定受控证据、历史词包版本或显式确认",
    correctionWrite
  );
  const decisionWrite = hotwordWriteRequests.find((request) => request.path === "/api/v1/badcases/A-4107/decisions");
  assert(decisionWrite?.payload?.decision === "confirmed" && decisionWrite.payload.expected_resource_version === 3, "易错词确认未从 GET 携带 confirmed 决策或实时乐观锁版本", decisionWrite);
  const versionCreateWrite = hotwordWriteRequests.find((request) => request.path === "/api/v1/hotword-packs/hotword_pack_auto_sales/versions");
  assert(versionCreateWrite?.payload?.version === "v1.9" && versionCreateWrite?.payload?.baseline_version_id === "hwpv-auto-sales-v1-8", "首次候选版本未基于已发布 v1.8 创建", versionCreateWrite);
  const itemPatchWrite = hotwordWriteRequests.find((request) => request.path.endsWith("/items/hotword-item-xingyue-l"));
  assert(itemPatchWrite?.payload?.expected_resource_version === 1, "候选继承词项更新未携带词项乐观锁版本", itemPatchWrite);
  assert(itemPatchWrite?.payload?.aliases?.includes("星月L") && itemPatchWrite.payload.aliases.includes("星越 L"), "候选继承词项未合并显式别名", itemPatchWrite);
  assert(itemPatchWrite?.payload?.weight === 91 && itemPatchWrite.payload.source_badcase_id === "A-4107" && itemPatchWrite.payload.source_type === "badcase", "候选继承词项未更新权重或 Badcase 来源", itemPatchWrite);
  assert(!hotwordWriteRequests.some((request) => request.path.endsWith(`/${candidateVersionId}/items`) && request.method === "POST"), "已存在规范词时不应重复 POST 词项", hotwordWriteRequests);
  const buildWrite = hotwordWriteRequests.find((request) => request.method === "PATCH" && request.payload.status === "validating");
  assert(buildWrite?.payload?.expected_resource_version === 2 && buildWrite?.payload?.provider === "auris-audio-stack" && !("manifest_storage_object_id" in buildWrite.payload), "候选构建未使用实时版本触发服务端 build，或客户端伪造了 manifest", buildWrite);
  const evalWrite = hotwordWriteRequests.find((request) => request.path.endsWith("/eval-runs"));
  assert(evalWrite?.payload?.provider === "auris-audio-stack" && evalWrite?.payload?.expected_resource_version === 4, "热词 EvalRun 未使用构建完成后的实时版本", evalWrite);
  assert(Object.keys(evalWrite?.payload ?? {}).sort().join(",") === "eval_dataset_id,expected_resource_version,provider", "热词 EvalRun 请求不得由客户端提交 metrics/lock/provider artifact", evalWrite);
  const approvalWrite = hotwordWriteRequests.find((request) => request.method === "PATCH" && request.payload.status === "approved");
  assert(approvalWrite?.payload?.expected_resource_version === 6 && approvalWrite?.payload?.eval_run_id === "hotword-eval-ui-1", "模型负责人审批未使用评测完成后的实时版本", approvalWrite);
  const publishWrite = hotwordWriteRequests.find((request) => request.path.endsWith("/publish"));
  assert(publishWrite?.payload?.expected_resource_version === 7 && publishWrite?.payload?.eval_run_id === "hotword-eval-ui-1", "项目管理员发布未使用审批后的实时版本", publishWrite);
  assert(hotwordWriteRequests.filter((request) => request.path.endsWith("/publish")).length === 1, "发布运行失败后不应重复创建 /publish 请求", hotwordWriteRequests);
  const publishRetryWrite = hotwordWriteRequests.find((request) => request.path === "/api/v1/runs/hotword-publish-ui-1/retries");
  assert(hotwordPublishRetryRequests === 1 && publishRetryWrite?.payload?.reason === "项目管理员重试词包发布", "发布运行失败后未通过受控 retries 接口重试", publishRetryWrite);
  assert(hotwordBuildPollRequests >= 2, "候选构建未完成 pending 到 success 的有限轮询", { hotwordBuildPollRequests });
  assert(hotwordEvalPollRequests >= 2, "影子评测未完成 pending 到 success 的有限轮询", { hotwordEvalPollRequests });
  assert(hotwordPublishPollRequests >= 2, "人工发布失败运行未完成 pending 到 failed 的有限轮询", { hotwordPublishPollRequests });
  assert(hotwordPublishRetryPollRequests >= 2, "人工发布重试未完成 pending 到 success 的有限轮询", { hotwordPublishRetryPollRequests });
  assert(hotwordAnalysisPollRequests >= 2, "热词分析未完成 pending 到 success 的有限轮询", { hotwordAnalysisPollRequests });
  assert(assetBackfillPollRequests >= 2, "受控回填未完成 pending 到 success 的有限轮询", { assetBackfillPollRequests });
  const taskDraftWrite = taskWriteRequests.find((request) => request.path === "/api/v1/task-versions");
  assert(taskDraftWrite?.payload?.audio_intelligence?.hotword_pack_version_id === candidateVersionId, "TaskVersion 新写入未绑定后端当前已发布 hotword_pack_version_id", taskDraftWrite);
  assert(!JSON.stringify(taskDraftWrite?.payload ?? {}).includes("legacy_hotwords_ref") && !JSON.stringify(taskDraftWrite?.payload ?? {}).includes("hotwords_ref"), "TaskVersion 新写入仍包含 legacy hotwords_ref", taskDraftWrite);
  const taskRunWrite = taskWriteRequests.find((request) => request.path === "/api/v1/task-runs");
  assert(taskRunWrite && !("audio_intelligence" in taskRunWrite.payload), "TaskRun 嵌套请求不得覆盖 TaskVersion 的 audio_intelligence/hotword 绑定", taskRunWrite);
  const taskPublishWrite = taskReleaseRequests.find((request) => request.path.endsWith("/task-version-hotword-ui-1/publish"));
  assert(taskPublishWrite?.payload?.source === "canvas_module", "热词发布生成的 TaskVersion 未经过现有任务发布接口", taskPublishWrite);
  const taskReleaseDecision = taskReleaseRequests.find((request) => request.path.endsWith("/task-version-publish-ui-1/decisions"));
  assert(taskReleaseDecision?.payload?.decision === "approved", "热词 TaskVersion 发布未经过项目管理员人工审批", taskReleaseDecision);
  assert(taskVersionPublishPollRequests >= 1 && hotwordTaskVersionStatus === "published", "TaskVersion 发布审批后未读取成功回执或未物化 published 状态", { taskVersionPublishPollRequests, hotwordTaskVersionStatus });
  const hotwordBackfillWrite = assetBackfillRequests.find((request) => request.path.includes("auris%2Fmodel%2Fasr_transcripts") || request.path.includes("auris/model/asr_transcripts"));
  assert(hotwordBackfillWrite?.taskVersionStatus === "published", "受控回填请求只能在真实 TaskVersion 状态 published 后创建", hotwordBackfillWrite);
  assert(hotwordBackfillWrite?.payload?.partition_key && hotwordBackfillWrite?.payload?.reason, "受控回填缺少 partition_key/reason", hotwordBackfillWrite);
  assert(hotwordBackfillWrite?.payload?.impact_scope?.hotword_pack_version_id === candidateVersionId, "热词回填缺少已发布词包版本绑定", hotwordBackfillWrite);
  assert(hotwordBackfillWrite?.payload?.impact_scope?.eval_run_id === "hotword-eval-ui-1" && hotwordBackfillWrite.payload.impact_scope.task_version_id === "task-version-hotword-ui-1", "热词回填缺少 EvalRun 或 TaskVersion 绑定", hotwordBackfillWrite);
  assert(hotwordBackfillWrite?.payload?.impact_scope?.source_asset === "auris/model/asr_transcripts" && hotwordBackfillWrite.payload.impact_scope.source_materialization_id === "mat_asr_20250526_122300", "热词回填缺少源资产或权威物化记录", hotwordBackfillWrite);
  assert(hotwordBackfillWrite?.payload?.impact_scope?.materialization_id === "mat_asr_20250526_122300", "热词回填仍在使用静态 MAT 物化 ID", hotwordBackfillWrite);
  assert(hotwordBackfillWrite?.payload?.impact_scope?.root_trace_id === "trace_hotword_pack_auto_sales" && hotwordBackfillWrite?.payload?.impact_scope?.overwrite_history === false, "热词回填未携带后端根 Trace 或错误允许覆盖历史资产", hotwordBackfillWrite);
  await page.locator("button.sidebar-user-logout").click();
  await page.getByRole("button", { name: "登录" }).waitFor({ state: "visible", timeout: 10000 });
  assert(authLogoutRequests === 2, "UI smoke 两次退出登录未撤销服务端会话", { authLogoutRequests });
  await assertBootFailureFallback(browser, baseUrl);
  await page.waitForTimeout(250);
  assert(browserErrors.length === 0, "UI smoke 收口时捕获到迟到的浏览器错误", { browserErrors, failedResponses, requestFailures });
  assert(requestFailures.length === 0, "UI smoke 收口时捕获到迟到的网络请求失败", { requestFailures });
  assert(failedResponses.length === 0, "UI smoke 收口时捕获到迟到的 HTTP 4xx/5xx", { failedResponses });
  console.log(
    JSON.stringify(
      { baseUrl, visited, status: "ok", failedResponses, projectionHits: [...projectionHits], exportRequests, hotwordReadRequests, hotwordWriteRequests, authLoginRequests, authLogoutRequests },
      null,
      2
    )
  );
} finally {
  await browser.close();
  await server.close();
  await new Promise((resolve) => bffStub.close(resolve));
}
