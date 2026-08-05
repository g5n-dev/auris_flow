import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

const baseUrl = process.env.AURIS_README_SCREENSHOT_URL ?? "http://127.0.0.1:5173/";
const outputRoot = resolve(import.meta.dirname, "../../../doc/assets/screenshots");
// Keep runtime behavior aligned with the visual-regression fixture clock. Visible
// dates are rewritten below so the exported images remain unmistakably synthetic.
const fixedTime = new Date("2025-05-26T12:27:18+08:00");

const captures = [
  {
    module: "数据",
    tab: "音频数据",
    file: "audio-data-assets.png",
    readyText: "项目数据资产"
  },
  {
    module: "任务",
    tab: "流程配置",
    file: "workflow-configuration.png",
    readyText: "任务配置"
  },
  {
    module: "调听",
    file: "listening-evidence-review.png",
    readyText: "调听工作台"
  }
];

const replacements = [
  ["星3系 325Li", "演示对象 X1"],
  ["星3系", "演示对象"],
  ["BJ-041", "DEMO-Q1"],
  ["金额冲突 15", "金额冲突 2"],
  ["串音候选 24", "串音候选 3"],
  ["低置信 18", "低置信 3"],
  ["全部 128", "全部 20"],
  ["待审 24", "待审 4"],
  ["已审 71", "已审 12"],
  ["跳过 6", "跳过 1"],
  ["rec_SH_A012_20250526_091500", "DEMO-REC-001"],
  ["SH-JINGAN-001", "DEMO-LOCATION-02"],
  ["u_sales_a", "demo_person_01"],
  ["北京区域 / 极光中心店", "地点 A"],
  ["门店、员工、报价事件、试驾事件、报价单的数据契约与关联", "空间、时间、事件、人物与单据的数据契约和关联"],
  ["分区、门店、人群、模型版本", "空间、时间、事件、人物与模型版本"],
  ["区域 / 门店 / 设备", "地点 / 设备"],
  ["销售 / 客户组 / 声纹", "角色 / 人物 / 声纹"],
  ["租户接口、员工接口、音频 URL 接口、认证事件接口", "地点、时间、事件、人物接口"],
  ["也被 租户接口、员工接口、音频 URL 接口、认证事件接口使用", "地点、时间、事件、人物与音频 URL 接口共用此认证"],
  ["接待单 #JD-20250526-000128", "示例接待单 #DEMO-O1"],
  ["A-1001_20250526_122300.wav", "DEMO-A01.wav"],
  ["S20250526-000128", "DEMO-S01"],
  ["S20250526-000131", "DEMO-S02"],
  ["汽车门店销售质检", "演示音频质检场景"],
  ["销售话术质检", "演示音频质检"],
  ["北京区域", "地点组 A"],
  ["极光中心店", "地点 01"],
  ["极光汽车", "演示租户"],
  ["task_sales_quality", "demo_audio_quality"],
  ["auto-sales-quality", "demo-audio-quality"],
  ["9625c78ecae3", "demo-snapshot"],
  ["BJ-AURORA-001", "DEMO-LOCATION-01"],
  ["aurora_auto", "demo_tenant"],
  ["报价单 #BJ-041", "示例报价单 #DEMO-Q1"],
  ["试驾单 #SJ-028", "示例试驾单 #DEMO-T1"],
  ["陈先生", "示例人物 B"],
  ["销售A", "示例人物 A"],
  ["销售B", "示例人物 C"],
  ["A-1001", "DEMO-A1"],
  ["B-2001", "DEMO-B1"],
  ["Hall-Mic", "DEMO-MIC"],
  ["Drive-01", "DEMO-D1"],
  ["AF-128", "DEMO-E01"],
  ["AF-129", "DEMO-E02"],
  ["AF-131", "DEMO-E03"],
  ["2025-05-26", "2099-01-01"],
  ["20250526", "20990101"],
  ["31.69 万", "12.34 万"],
  ["29.10 万", "10.50 万"],
  ["28.19 万", "10.00 万"],
  ["28 万以内", "10 万以内"],
  ["3.50 万", "2.34 万"],
  ["3.5 万", "2.34 万"],
  ["3.8 万", "2.56 万"],
  ["门店", "地点"],
  ["员工", "人物"],
  ["客户", "人物"],
  ["区域", "地点组"]
];

const blockedScreenshotTerms = [
  "极光汽车",
  "极光中心店",
  "陈先生",
  "A-1001",
  "B-2001",
  "BJ-041",
  "星3系",
  "SH-JINGAN-001",
  "u_sales_a",
  "rec_SH_A012",
  "门店",
  "task_sales_quality",
  "2025-05-26"
];

const exactText = (text) => new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`);

async function waitForStablePage(page) {
  await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => {});
  await page.evaluate(async () => {
    if (document.fonts?.ready) await document.fonts.ready;
  });
  let previous = "";
  let stableSamples = 0;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await page.waitForTimeout(100);
    const current = await page.locator("body").innerText();
    stableSamples = current === previous ? stableSamples + 1 : 0;
    previous = current;
    if (stableSamples >= 2) return;
  }
  throw new Error("README 截图页面未在 3 秒内稳定");
}

async function clickModule(page, label) {
  const button = page.locator(`button[aria-label="导航：${label}"]`).first();
  await button.click();
  await button.waitFor({ state: "visible" });
  await waitForStablePage(page);
}

async function clickTab(page, label) {
  const tab = page.locator(".module-tabs button").filter({ hasText: exactText(label) }).first();
  await tab.click();
  await tab.waitFor({ state: "visible" });
  await waitForStablePage(page);
}

async function applySyntheticScreenshotData(page) {
  await page.evaluate(({ replacementPairs }) => {
    document.getElementById("readme-synthetic-data-badge")?.remove();
    const replaceText = (input) => replacementPairs.reduce(
      (value, [source, target]) => value.replaceAll(source, target),
      input
    );
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode);
    for (const node of textNodes) {
      const nextValue = replaceText(node.nodeValue ?? "");
      if (nextValue !== node.nodeValue) node.nodeValue = nextValue;
    }
    for (const element of document.querySelectorAll("[aria-label], [title], input[value], textarea")) {
      for (const attribute of ["aria-label", "title", "value", "placeholder"]) {
        const value = element.getAttribute(attribute);
        if (value) element.setAttribute(attribute, replaceText(value));
      }
    }
    const badge = document.createElement("div");
    badge.id = "readme-synthetic-data-badge";
    badge.textContent = "DEMO · 全量合成数据 · 非客户数据";
    badge.setAttribute("aria-label", "全部为合成演示数据，不对应真实客户");
    document.body.append(badge);
  }, { replacementPairs: replacements });
  await page.addStyleTag({ content: `
    *, *::before, *::after {
      animation: none !important;
      caret-color: transparent !important;
      scroll-behavior: auto !important;
      transition: none !important;
    }
    #readme-synthetic-data-badge {
      position: fixed;
      left: 104px;
      bottom: 12px;
      z-index: 2147483647;
      padding: 6px 10px;
      border: 1px solid #91caff;
      border-radius: 6px;
      background: rgba(230, 244, 255, 0.96);
      color: #0958d9;
      font: 600 12px/1.2 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0.02em;
      pointer-events: none;
      white-space: nowrap;
      width: max-content !important;
      max-width: none !important;
      overflow: visible !important;
      text-overflow: clip !important;
    }
  ` });
}

mkdirSync(outputRoot, { recursive: true });
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    colorScheme: "light"
  });
  const page = await context.newPage();
  await page.clock.setFixedTime(fixedTime);
  const sessionResponse = await page.request.post(new URL("/api/v1/auth/dev-login", baseUrl).toString(), {
    data: {
      email: "demo.operator@auris.local",
      password: "auris-demo"
    }
  });
  if (!sessionResponse.ok()) {
    throw new Error(`无法建立 README 截图演示会话：HTTP ${sessionResponse.status()}`);
  }

  // Fail before writing any image when the running UI is not the explicit
  // fixture-only DEMO build required for public documentation captures.
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.locator('button[aria-label^="导航："]').first().waitFor({ state: "visible" });
  await clickModule(page, "调听");
  if (await page.locator('[data-listening-source="demo"]').count() === 0) {
    throw new Error("README 截图必须连接以 VITE_DEMO_MODE=true 启动的显式 DEMO 前端");
  }

  for (const capture of captures) {
    // Reload for every capture so direct DOM aliases never leak into React state
    // or affect the next module's navigation behavior.
    await page.goto(baseUrl, { waitUntil: "networkidle" });
    await page.locator('button[aria-label^="导航："]').first().waitFor({ state: "visible" });
    await clickModule(page, capture.module);
    if (capture.tab) await clickTab(page, capture.tab);
    await page.getByText(capture.readyText, { exact: true }).first().waitFor({ state: "visible" });
    await applySyntheticScreenshotData(page);
    const screenshotText = await page.locator("body").innerText();
    const exposed = blockedScreenshotTerms.filter((term) => screenshotText.includes(term));
    if (exposed.length) {
      throw new Error(`${capture.file} 仍暴露未标记的演示标识：${exposed.join(", ")}`);
    }
    if (!screenshotText.includes("全量合成数据 · 非客户数据")) {
      throw new Error(`${capture.file} 缺少合成演示数据标识`);
    }
    await page.screenshot({
      path: resolve(outputRoot, capture.file),
      fullPage: false,
      animations: "disabled"
    });
  }
  await context.close();
} finally {
  await browser.close();
}

process.stdout.write(`README 合成演示截图已更新：${captures.length} 张\n`);
