import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const baseUrl = process.env.AURIS_AUDIT_URL || "http://127.0.0.1:5180/";
const outDir = new URL("./screenshots/", import.meta.url).pathname;
mkdirSync(outDir, { recursive: true });
const blockedExposedTerms = new Set(["Dagster", "DAG", "mock"]);

const modules = [
  { label: "首页", file: "01-home" },
  { label: "租户", file: "02-tenants", tabs: ["概览", "项目", "成员", "资源配额", "审计日志"] },
  { label: "项目", file: "03-projects", tabs: ["项目概览", "数据源", "成员", "标签体系", "质量目标"] },
  { label: "任务", file: "04-canvas", tabs: ["流程配置", "流程模板", "编排版本", "输入输出", "触发与调度", "AB实验", "运行记录", "版本发布"] },
  { label: "数据", file: "05-data", tabs: ["音频数据", "人物/声纹", "事件", "关联视图"] },
  { label: "知识库", file: "06-knowledge", tabs: ["知识总览", "知识连接器", "索引构建", "知识可视化", "质量管理", "效果展示", "运行记录"] },
  { label: "调听", file: "07-listening" },
  { label: "标签", file: "08-labels", tabs: ["标签体系", "智能抽取", "规则/Prompt", "评测人审", "版本发布"] },
  { label: "洞察", file: "09-insights", tabs: ["业务大盘", "门店归因", "销售绩效", "标签资产", "模型质量", "报告中心"] },
  { label: "评测", file: "10-evaluation" },
  { label: "资产", file: "11-assets" },
  { label: "设置", file: "12-settings", tabs: ["模型配置", "工具配置", "阈值配置", "权限配置", "存储配置", "通知配置"] }
];

const highRiskShots = [
  { module: "任务", tab: "输入输出", file: "13-task-io" },
  { module: "任务", tab: "触发与调度", file: "14-task-schedule" },
  { module: "任务", tab: "AB实验", file: "15-task-ab" },
  { module: "数据", tab: "人物/声纹", file: "16-data-voiceprint" },
  { module: "数据", tab: "关联视图", file: "17-data-relation" },
  { module: "知识库", tab: "知识可视化", file: "18-knowledge-graph" },
  { module: "知识库", tab: "效果展示", file: "19-knowledge-effects" },
  { module: "标签", tab: "版本发布", file: "20-label-release" },
  { module: "洞察", tab: "标签资产", file: "21-insight-tag-assets" },
  { module: "洞察", tab: "报告中心", file: "22-insight-reports" },
  { module: "设置", tab: "模型配置", file: "23-settings-model" }
];

const actionFeedbackCoverage = [
  { label: "任务", tab: "输入输出", min: 3 },
  { label: "任务", tab: "触发与调度", min: 3 },
  { label: "任务", tab: "AB实验", min: 3 },
  { label: "调听", tab: "", min: 4 },
  { label: "设置", tab: "模型配置", min: 6 },
  { label: "洞察", tab: "报告中心", min: 6 }
];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const exact = (text) => new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`);

function clean(text) {
  return (text || "").replace(/\s+/g, " ").trim();
}

async function clickNav(page, label) {
  const target = page.locator(`button[aria-label="导航：${label}"]`).first();
  if (!(await target.count())) {
    throw new Error(
      `Navigation not found: ${label}. Audit target may not be loaded or BFF login failed. URL=${page.url()}`
    );
  }
  await target.click();
  await sleep(220);
}

async function clickTab(page, label) {
  const target = page.locator(".module-tabs button").filter({ hasText: exact(label) }).first();
  if (!(await target.count())) throw new Error(`Tab not found: ${label}`);
  await target.click();
  await sleep(220);
  const selected = await target.getAttribute("aria-selected");
  if (selected !== "true") {
    throw new Error(`Clicked tab ${label}, but aria-selected=${selected}`);
  }
}

async function capture(page, file, label, tab = "") {
  const path = join(outDir, `${file}.png`);
  await page.screenshot({ path, fullPage: false });
  const diagnostics = await page.evaluate(() => {
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return rect.width > 1 && rect.height > 1 && style.visibility !== "hidden" && style.display !== "none";
    };
    const inViewport = (el) => {
      const rect = el.getBoundingClientRect();
      return rect.right > 0 && rect.bottom > 0 && rect.left < innerWidth && rect.top < innerHeight;
    };
    const text = document.body.innerText;
    const viewport = { width: innerWidth, height: innerHeight };
    const largeEmpty = [...document.querySelectorAll("section, article, div")]
      .filter(visible)
      .map((el) => {
        const rect = el.getBoundingClientRect();
        const t = (el.textContent || "").replace(/\s+/g, " ").trim();
        return {
          tag: el.tagName.toLowerCase(),
          cls: typeof el.className === "string" ? el.className.slice(0, 120) : "",
          w: Math.round(rect.width),
          h: Math.round(rect.height),
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          textLen: t.length,
          sample: t.slice(0, 80)
        };
      })
      .filter((item) => item.w > viewport.width * 0.22 && item.h > 180 && item.textLen < 35)
      .slice(0, 8);
    const blockingLargeEmpty = largeEmpty.filter(
      (item) => item.w > viewport.width * 0.32 && item.h > viewport.height * 0.24 && item.textLen < 24
    );
    const documentOverflow = {
      viewportWidth: viewport.width,
      scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
      overflowX: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) > viewport.width + 2
    };
    const clippedControls = [...document.querySelectorAll("button, h1, h2, h3, [role='tab']")]
      .filter((el) => visible(el) && inViewport(el))
      .filter((el) => !el.matches(".bell"))
      .filter((el) => {
        const style = getComputedStyle(el);
        const clipped = el.scrollWidth > el.clientWidth + 2 || el.scrollHeight > el.clientHeight + 2;
        const intentionallyScrollable = ["auto", "scroll"].includes(style.overflowX) || ["auto", "scroll"].includes(style.overflowY);
        const intentionalEllipsis = style.textOverflow === "ellipsis";
        return clipped && !intentionallyScrollable && !intentionalEllipsis;
      })
      .map((el) => {
        const rect = el.getBoundingClientRect();
        return {
          tag: el.tagName.toLowerCase(),
          cls: typeof el.className === "string" ? el.className.slice(0, 120) : "",
          w: Math.round(rect.width),
          h: Math.round(rect.height),
          text: (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 100)
        };
      })
      .slice(0, 12);
    const buttons = [...document.querySelectorAll("button")].filter(visible).map((button) => (button.textContent || "").replace(/\s+/g, " ").trim()).filter(Boolean);
    const headings = [...document.querySelectorAll("h1,h2,h3,strong")].filter(visible).map((node) => (node.textContent || "").replace(/\s+/g, " ").trim()).filter(Boolean);
    const duplicateButtons = [...new Set(buttons.filter((label, index) => buttons.indexOf(label) !== index))].slice(0, 12);
    const duplicateHeadings = [...new Set(headings.filter((label, index) => headings.indexOf(label) !== index))].slice(0, 12);
    const exposedTerms = ["Dagster", "DAG", "mock", "暂无", "未配置", "未保存"].filter((term) => text.includes(term));
    const actionFeedbackButtons = [
      ...document.querySelectorAll(
        [
          ".task-config-page [data-feedback-required]",
          ".listening-head [data-feedback-required]",
          ".annotation-main [data-feedback-required]",
          ".evidence-panel [data-feedback-required]",
          ".listening-operation-toast [data-feedback-required]",
          ".quality-appeal-modal [data-feedback-required]",
          ".settings-flow [data-feedback-required]",
          ".insight-command-shell [data-feedback-required]"
        ].join(", ")
      )
    ]
      .filter(visible)
      .map((button) => {
        const label = (button.textContent || "").replace(/\s+/g, " ").trim();
        const feedback = button.getAttribute("data-feedback") || "";
        const describedBy = (button.getAttribute("aria-describedby") || "")
          .split(/\s+/)
          .filter(Boolean)
          .map((id) => document.getElementById(id)?.textContent || "")
          .join(" ");
        const nearbyReason =
            button
            .closest(
              [
                ".canvas-toolbar-actions",
                ".task-tab-card",
                ".listening-action-strip",
                ".sn-act",
                ".action-row",
                ".reception-link-actions",
                ".reception-locator-write",
                ".listening-operation-toast",
                ".quality-appeal-modal",
                ".settings-smart-actions",
                ".settings-human-actions",
                ".asr-service-actions",
                ".settings-draft-card",
                ".insight-dashboard-actions",
                ".insight-report-actions",
                ".insight-agent-summary",
                ".insight-report-editor"
              ].join(",")
            )
            ?.querySelector(".disabled-reason")?.textContent || "";
        const title = button.getAttribute("title") || "";
        const disabled = button.matches(":disabled") || button.getAttribute("aria-disabled") === "true";
        return {
          label,
          disabled,
          feedback,
          hasFeedbackState: /\b(p|s|e)\b/.test(feedback),
          hasDisabledReason:
            !disabled || Boolean((title || describedBy || nearbyReason).replace(/\s+/g, " ").trim())
        };
      });
    const actionFeedbackMissing = actionFeedbackButtons
      .filter((button) => !button.hasFeedbackState || !button.hasDisabledReason)
      .map(({ label, disabled, feedback, hasFeedbackState, hasDisabledReason }) => ({
        label,
        disabled,
        feedback,
        hasFeedbackState,
        hasDisabledReason
      }));
    return {
      title: document.title,
      url: location.href,
      textLength: text.length,
      visibleButtonCount: buttons.length,
      duplicateButtons,
      duplicateHeadings,
      exposedTerms,
      actionFeedbackButtons,
      actionFeedbackMissing,
      largeEmpty,
      blockingLargeEmpty,
      documentOverflow,
      clippedControls
    };
  });
  return { file: path, label, tab, diagnostics };
}

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
await page.goto(baseUrl, { waitUntil: "networkidle" });
await sleep(500);
if (await page.getByRole("button", { name: "演示账号" }).count()) {
  await page.getByRole("button", { name: "演示账号" }).click();
  await sleep(300);
}
if (!(await page.locator('button[aria-label^="导航："]').count())) {
  throw new Error(
    `Auris Flow navigation did not render. Start the frontend and BFF, or set AURIS_AUDIT_URL to a reachable app. URL=${page.url()}`
  );
}

const results = [];
for (const mod of modules) {
  await clickNav(page, mod.label);
  for (const tab of mod.tabs ?? []) {
    await clickTab(page, tab);
  }
  results.push(await capture(page, mod.file, mod.label));
}

await page.setViewportSize({ width: 1440, height: 900 });
for (const shot of highRiskShots) {
  await clickNav(page, shot.module);
  await clickTab(page, shot.tab);
  results.push(await capture(page, shot.file, shot.module, shot.tab));
}

writeFileSync(new URL("./capture-diagnostics.json", import.meta.url), JSON.stringify(results, null, 2), "utf8");
await browser.close();

console.log(JSON.stringify({ screenshots: results.length, outDir }, null, 2));

const blocked = results
  .map((item) => ({
    label: item.label,
    tab: item.tab,
    terms: item.diagnostics.exposedTerms.filter((term) => blockedExposedTerms.has(term))
  }))
  .filter((item) => item.terms.length);
if (blocked.length) {
  throw new Error(`Blocked engineering terms are visible in audited user paths: ${JSON.stringify(blocked, null, 2)}`);
}

const missingActionFeedback = results
  .map((item) => ({
    label: item.label,
    tab: item.tab,
    missing: item.diagnostics.actionFeedbackMissing
  }))
  .filter((item) => item.missing.length);
if (missingActionFeedback.length) {
  throw new Error(`Action buttons are missing feedback metadata or disabled reasons: ${JSON.stringify(missingActionFeedback, null, 2)}`);
}

const missingFeedbackCoverage = actionFeedbackCoverage
  .map((rule) => {
    const shot = results.find((item) => item.label === rule.label && (item.tab || "") === rule.tab);
    return {
      ...rule,
      actual: shot?.diagnostics.actionFeedbackButtons?.length ?? 0
    };
  })
  .filter((item) => item.actual < item.min);
if (missingFeedbackCoverage.length) {
  throw new Error(`Action feedback coverage is below threshold: ${JSON.stringify(missingFeedbackCoverage, null, 2)}`);
}

const layoutBlockers = results
  .map((item) => ({
    label: item.label,
    tab: item.tab,
    documentOverflow: item.diagnostics.documentOverflow,
    largeEmpty: item.diagnostics.blockingLargeEmpty,
    clippedControls: item.diagnostics.clippedControls
  }))
  .filter(
    (item) => item.documentOverflow?.overflowX || item.largeEmpty?.length || item.clippedControls?.length
  );
if (layoutBlockers.length) {
  throw new Error(`Visible layout blockers detected: ${JSON.stringify(layoutBlockers, null, 2)}`);
}
