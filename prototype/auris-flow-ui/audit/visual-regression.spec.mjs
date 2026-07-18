import { expect, test } from "playwright/test";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

const baseUrl = process.env.AURIS_AUDIT_URL || "http://127.0.0.1:5180/";
const updateBaseline = process.env.AURIS_UPDATE_VISUAL_BASELINE === "1";
const geometryPath = process.env.AURIS_VISUAL_GEOMETRY_PATH || new URL(
  "../test-baselines/visual-regression/geometry.json",
  import.meta.url
).pathname;
const fixedTime = "2025-05-26T12:27:18+08:00";
const moduleFilter = new Set(
  (process.env.AURIS_VISUAL_MODULES ?? "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean)
);

const modules = [
  { label: "首页", file: "01-home", tabs: ["总览", "待处理", "异常提醒", "最近资产"] },
  { label: "租户", file: "02-tenants", tabs: ["概览", "项目", "成员", "ASR 接入", "资源配额", "审计日志"] },
  { label: "项目", file: "03-projects", tabs: ["项目概览", "数据源", "成员", "标签体系", "质量目标"] },
  { label: "任务", file: "04-canvas", tabs: ["流程配置", "流程模板", "编排版本", "输入输出", "触发与调度", "AB实验", "运行记录", "版本发布"] },
  { label: "数据", file: "05-data", tabs: ["音频数据", "人物/声纹", "事件", "关联视图"] },
  { label: "知识库", file: "06-knowledge", tabs: ["知识总览", "知识连接器", "索引构建", "知识可视化", "质量管理", "效果展示", "运行记录"] },
  { label: "调听", file: "07-listening", tabs: [] },
  { label: "标签", file: "08-labels", tabs: ["标签体系", "智能抽取", "规则/Prompt", "评测人审", "版本发布"] },
  { label: "洞察", file: "09-insights", tabs: ["业务大盘", "门店归因", "销售绩效", "标签资产", "模型质量", "报告中心"] },
  { label: "评测", file: "10-evaluation", tabs: ["自动化评测", "打标评测", "Prompt优化", "人工评测", "评测集", "模型对比", "badcase"] },
  { label: "资产", file: "11-assets", tabs: ["资产目录", "资产详情", "资产血缘", "数据回填", "资产质量", "资产任务"] },
  { label: "设置", file: "12-settings", tabs: ["模型配置", "工具配置", "阈值配置", "权限配置", "存储配置", "通知配置"] }
];
const selectedModules = moduleFilter.size
  ? modules.filter((module) => moduleFilter.has(module.label) || moduleFilter.has(module.file))
  : modules;

if (moduleFilter.size && selectedModules.length !== moduleFilter.size) {
  throw new Error(`未知视觉模块过滤项：${[...moduleFilter].filter((item) => !selectedModules.some((module) => module.label === item || module.file === item)).join(", ")}`);
}

const geometrySelectors = {
  shell: ".app-shell",
  sidebar: ".sidebar",
  topbar: ".topbar",
  workbench: ".workbench",
  page: ".module-page",
  tabs: ".module-tabs",
  selectedTab: '.module-tabs button[aria-selected="true"]',
  firstPanel: ".module-page .module-panel"
};

const exactText = (text) => new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`);

function normalizeDynamicMetadata(value) {
  if (Array.isArray(value)) return value.map(normalizeDynamicMetadata);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value).map(([key, child]) => {
    if (key === "request_id") return [key, "visual-request-id"];
    if (key === "trace_id") return [key, "trace_visual_regression"];
    return [key, normalizeDynamicMetadata(child)];
  }));
}

async function waitForStablePage(page) {
  const projection = page.locator('[data-testid="module-projection-state"]').first();
  if (await projection.count()) {
    await projection.waitFor({ state: "visible" });
    await expect.poll(async () => projection.getAttribute("data-state")).not.toBe("pending");
  }
  await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => {});
  await page.evaluate(async () => {
    if (document.fonts?.ready) await document.fonts.ready;
  });

  let previous = "";
  let stableSamples = 0;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await page.waitForTimeout(100);
    const snapshot = await page.evaluate(() => {
      const pageNode = document.querySelector(".module-page") ?? document.body;
      const rect = pageNode.getBoundingClientRect();
      return JSON.stringify({
        text: pageNode.textContent?.replace(/\s+/g, " ").trim(),
        elements: pageNode.querySelectorAll("*").length,
        width: Math.round(rect.width * 2) / 2,
        height: Math.round(rect.height * 2) / 2,
        scrollWidth: pageNode.scrollWidth,
        scrollHeight: pageNode.scrollHeight
      });
    });
    stableSamples = snapshot === previous ? stableSamples + 1 : 0;
    previous = snapshot;
    if (stableSamples >= 2) return;
  }
  throw new Error("页面文本与几何在 3 秒内未稳定");
}

async function expectLazyBranchesHealthy(page, context) {
  const errors = page.locator('.feature-module-error:visible, [data-testid$="-module-load-error"]:visible');
  const fallbacks = page.locator('.feature-module-loading:visible, [data-testid$="-loading"]:visible');
  await expect(errors, `${context} 出现 lazy 错误边界`).toHaveCount(0);
  await expect(fallbacks, `${context} 的 lazy fallback 未收敛`).toHaveCount(0, { timeout: 10_000 });
  await expect(errors, `${context} 在 lazy 加载后出现错误边界`).toHaveCount(0);
}

async function clickModule(page, label) {
  const button = page.locator(`button[aria-label="导航：${label}"]`).first();
  await button.click();
  await expect(button).toHaveClass(/active/);
  await expectLazyBranchesHealthy(page, `模块 ${label}`);
  await waitForStablePage(page);
}

async function clickTab(page, label) {
  const tab = page.locator(".module-tabs button").filter({ hasText: exactText(label) }).first();
  await tab.click();
  await expect(tab).toHaveAttribute("aria-selected", "true");
  await expectLazyBranchesHealthy(page, `Tab ${label}`);
  await waitForStablePage(page);
}

async function captureGeometry(page) {
  return page.evaluate((selectors) => Object.fromEntries(
    Object.entries(selectors).flatMap(([key, selector]) => {
      const node = document.querySelector(selector);
      if (!node) return [];
      const rect = node.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return [];
      const precise = (value) => Number(value.toFixed(3));
      return [[key, {
        x: precise(rect.x),
        y: precise(rect.y),
        width: precise(rect.width),
        height: precise(rect.height)
      }]];
    })
  ), geometrySelectors);
}

function assertGeometryMatches(actual, expected) {
  expect(Object.keys(actual).sort()).toEqual(Object.keys(expected).sort());
  for (const [shot, boxes] of Object.entries(expected)) {
    expect(Object.keys(actual[shot]).sort(), `${shot} 的关键元素集合变化`).toEqual(Object.keys(boxes).sort());
    for (const [key, rect] of Object.entries(boxes)) {
      for (const field of ["x", "y", "width", "height"]) {
        expect(
          Math.abs(actual[shot][key][field] - rect[field]),
          `${shot} / ${key}.${field} 超过 0.5px`
        ).toBeLessThanOrEqual(0.5);
      }
    }
  }
}

test("所有业务模块与 tab 保持像素和关键几何等价", async ({ page }) => {
  const consoleErrors = [];
  const pageErrors = [];
  const requestFailures = [];
  const failedResponses = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
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
        method: response.request().method(),
        status: response.status(),
        url: response.url()
      });
    }
  });

  await page.clock.setFixedTime(new Date(fixedTime));
  const visualSessionResponse = await page.request.post(
    new URL("/api/v1/auth/dev-login", baseUrl).toString(),
    {
      data: {
        email: "demo.operator@auris.local",
        password: "auris-demo"
      }
    }
  );
  expect(visualSessionResponse.ok(), "视觉回归无法建立服务端开发会话").toBeTruthy();
  const visualSessionCookie = (await page.context().cookies(baseUrl)).find(
    (cookie) => cookie.name === "auris_session"
  );
  expect(visualSessionCookie?.httpOnly, "视觉回归必须使用 HttpOnly Cookie 会话").toBe(true);
  await page.route("**/api/v1/**", async (route) => {
    const response = await route.fetch();
    const contentType = response.headers()["content-type"] ?? "";
    if (!contentType.includes("application/json")) {
      await route.fulfill({ response });
      return;
    }
    const json = normalizeDynamicMetadata(await response.json());
    await route.fulfill({ response, json });
  });

  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.addStyleTag({ content: `
    *, *::before, *::after {
      animation-delay: 0s !important;
      animation-duration: 0s !important;
      animation-iteration-count: 1 !important;
      caret-color: transparent !important;
      scroll-behavior: auto !important;
      transition-delay: 0s !important;
      transition-duration: 0s !important;
    }
  ` });
  await page.locator('button[aria-label^="导航："]').first().waitFor({ state: "visible" });

  const geometry = {};
  for (const module of selectedModules) {
    await clickModule(page, module.label);
    const rootShot = `${module.file}__root.png`;
    geometry[rootShot] = await captureGeometry(page);
    await expect(page).toHaveScreenshot(rootShot, { animations: "disabled" });

    for (const [index, tab] of module.tabs.entries()) {
      await clickTab(page, tab);
      const tabShot = `${module.file}__${String(index + 1).padStart(2, "0")}-${tab.replaceAll("/", "-")}.png`;
      geometry[tabShot] = await captureGeometry(page);
      await expect(page).toHaveScreenshot(tabShot, { animations: "disabled" });
    }
  }

  expect(pageErrors, "视觉遍历存在 pageerror").toEqual([]);
  expect(consoleErrors, "视觉遍历存在 console.error").toEqual([]);
  expect(requestFailures, "视觉遍历存在网络请求失败").toEqual([]);
  expect(failedResponses, "视觉遍历存在 HTTP 4xx/5xx").toEqual([]);

  if (updateBaseline) {
    mkdirSync(dirname(geometryPath), { recursive: true });
    const priorGeometry = moduleFilter.size && existsSync(geometryPath)
      ? JSON.parse(readFileSync(geometryPath, "utf8"))
      : {};
    writeFileSync(geometryPath, `${JSON.stringify({ ...priorGeometry, ...geometry }, null, 2)}\n`, "utf8");
  } else {
    const expectedGeometry = JSON.parse(readFileSync(geometryPath, "utf8"));
    const expected = moduleFilter.size
      ? Object.fromEntries(Object.keys(geometry).map((shot) => [shot, expectedGeometry[shot]]))
      : expectedGeometry;
    assertGeometryMatches(geometry, expected);
  }
});
