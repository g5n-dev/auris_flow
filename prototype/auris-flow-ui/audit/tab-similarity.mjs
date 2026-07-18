import { chromium } from "playwright";
import { writeFileSync } from "node:fs";

const baseUrl = process.env.AURIS_AUDIT_URL || "http://127.0.0.1:5180/";
const similarityThreshold = Number(process.env.AURIS_AUDIT_TAB_SIMILARITY_THRESHOLD ?? 0.62);
const allowedSimilarPairs = new Set(
  [
    ["任务", "流程配置", "运行记录"],
    ["设置", "工具配置", "阈值配置"],
    ["设置", "工具配置", "权限配置"],
    ["设置", "阈值配置", "权限配置"],
    ["设置", "工具配置", "存储配置"],
    ["设置", "阈值配置", "存储配置"],
    ["设置", "权限配置", "存储配置"],
    ["设置", "工具配置", "通知配置"],
    ["设置", "阈值配置", "通知配置"],
    ["设置", "权限配置", "通知配置"],
    ["设置", "存储配置", "通知配置"]
  ].map(([module, a, b]) => pairKey(module, a, b))
);
const modules = [
  { label: "任务", tabs: ["流程配置", "流程模板", "编排版本", "输入输出", "触发与调度", "AB实验", "运行记录", "版本发布"] },
  { label: "数据", tabs: ["音频数据", "人物/声纹", "事件", "关联视图"] },
  { label: "知识库", tabs: ["知识总览", "知识连接器", "索引构建", "知识可视化", "质量管理", "效果展示", "运行记录"] },
  { label: "标签", tabs: ["标签体系", "智能抽取", "规则/Prompt", "评测人审", "版本发布"] },
  {
    label: "洞察",
    tabs: ["业务大盘", "门店归因", "销售绩效", "标签资产", "模型质量", "报告中心"],
    contentSelector: "[data-audit-tab-content]",
    requireContentMarker: true
  },
  { label: "设置", tabs: ["模型配置", "工具配置", "阈值配置", "权限配置", "存储配置", "通知配置"] }
];

const exact = (text) => new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`);
const tokens = (text) => [...new Set((text || "").replace(/[A-Za-z0-9_:/.-]+/g, " ").match(/[\u4e00-\u9fa5]{2,}|[A-Za-z]{3,}/g) || [])];
function pairKey(module, a, b) {
  return [module, ...[a, b].sort()].join("::");
}
const jaccard = (a, b) => {
  const setA = new Set(a);
  const setB = new Set(b);
  const union = new Set([...setA, ...setB]);
  let hit = 0;
  for (const item of setA) if (setB.has(item)) hit += 1;
  return union.size ? Math.round((hit / union.size) * 1000) / 1000 : 0;
};

async function clickNav(page, label) {
  const locator = page.locator(`button[aria-label="导航：${label}"]`).first();
  if (!(await locator.count())) {
    throw new Error(
      `Navigation not found: ${label}. Audit target may not be loaded or BFF login failed. URL=${page.url()}`
    );
  }
  await locator.click();
  await page.waitForFunction((expectedLabel) => {
    const active = [...document.querySelectorAll('button[aria-label^="导航："]')]
      .find((button) => button.classList.contains("active"));
    return active?.getAttribute("aria-label") === `导航：${expectedLabel}` && Boolean(document.querySelector(".module-page"));
  }, label);
  const projection = page.locator('[data-testid="module-projection-state"]').first();
  await projection.waitFor({ state: "visible" });
  await page.waitForFunction(() => {
    const state = document.querySelector('[data-testid="module-projection-state"]')?.getAttribute("data-state");
    return Boolean(state && state !== "pending");
  });
}

async function clickTab(page, label) {
  const locator = page.locator(".module-tabs button").filter({ hasText: exact(label) }).first();
  if (!(await locator.count())) throw new Error(`Tab not found: ${label}`);
  await locator.click();
  await page.waitForFunction((expectedLabel) => {
    const selected = document.querySelector('.module-tabs button[aria-selected="true"]');
    return selected?.textContent?.trim() === expectedLabel;
  }, label);
  const selected = await locator.getAttribute("aria-selected");
  if (selected !== "true") {
    throw new Error(`Clicked tab ${label}, but aria-selected=${selected}`);
  }
}

async function waitForStableContent(page, locator, label) {
  await locator.waitFor({ state: "visible" });
  await page.evaluate(async () => {
    if (document.fonts?.ready) await document.fonts.ready;
  });
  await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});

  let previous = "";
  let stableSamples = 0;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await page.waitForTimeout(100);
    const snapshot = await locator.evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return JSON.stringify({
        marker: node.getAttribute("data-audit-tab-content") ?? "",
        text: node.innerText,
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        scrollWidth: node.scrollWidth,
        scrollHeight: node.scrollHeight,
        elements: node.querySelectorAll("*").length
      });
    });
    stableSamples = snapshot === previous ? stableSamples + 1 : 0;
    previous = snapshot;
    if (stableSamples >= 2) return;
  }
  throw new Error(`Tab content did not become stable: ${label}`);
}

async function readTabContent(page, mod, tab) {
  const locator = mod.contentSelector
    ? page.locator(`${mod.contentSelector}[data-audit-tab-content="${tab}"]`).first()
    : page.locator(".module-page").first();
  await waitForStableContent(page, locator, `${mod.label} / ${tab}`);
  const marker = await locator.getAttribute("data-audit-tab-content");
  if (mod.requireContentMarker && marker !== tab) {
    throw new Error(`Tab ${tab} selected, but module content marker=${marker ?? "missing"}`);
  }
  const text = (await locator.innerText()).trim();
  const contentTokens = tokens(text);
  if (text.length < 80 || contentTokens.length < 8) {
    throw new Error(`Tab main content is blank or too sparse: ${mod.label} / ${tab} (${text.length} chars, ${contentTokens.length} tokens)`);
  }
  return { text, tokens: contentTokens, marker, contentChars: text.length };
}

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
await page.goto(baseUrl, { waitUntil: "networkidle" });
if (await page.getByRole("button", { name: "演示账号" }).count()) {
  await page.getByRole("button", { name: "演示账号" }).click();
  await page.locator('button[aria-label^="导航："]').first().waitFor({ state: "visible" });
}
if (!(await page.locator('button[aria-label^="导航："]').count())) {
  throw new Error(
    `Auris Flow navigation did not render. Start the frontend and BFF, or set AURIS_AUDIT_URL to a reachable app. URL=${page.url()}`
  );
}

const results = [];
const unexpectedPairs = [];
for (const mod of modules) {
  await clickNav(page, mod.label);
  const tabTexts = [];
  for (const tab of mod.tabs) {
    await clickTab(page, tab);
    const content = await readTabContent(page, mod, tab);
    tabTexts.push({ tab, ...content });
  }
  const pairs = [];
  for (let i = 0; i < tabTexts.length; i += 1) {
    for (let j = i + 1; j < tabTexts.length; j += 1) {
      const similarity = jaccard(tabTexts[i].tokens, tabTexts[j].tokens);
      if (similarity >= similarityThreshold) {
        const key = pairKey(mod.label, tabTexts[i].tab, tabTexts[j].tab);
        const allowed = allowedSimilarPairs.has(key);
        const pair = { a: tabTexts[i].tab, b: tabTexts[j].tab, similarity, allowed };
        pairs.push(pair);
        if (!allowed) unexpectedPairs.push({ module: mod.label, ...pair });
      }
    }
  }
  results.push({
    module: mod.label,
    capturedTabs: tabTexts.map((item) => ({
      tab: item.tab,
      contentChars: item.contentChars,
      tokenCount: item.tokens.length,
      marker: item.marker
    })),
    similarPairs: pairs.sort((a, b) => b.similarity - a.similarity).slice(0, 12)
  });
}

writeFileSync(new URL("./tab-similarity.json", import.meta.url), JSON.stringify(results, null, 2), "utf8");
console.log(JSON.stringify(results, null, 2));
await browser.close();

if (unexpectedPairs.length) {
  throw new Error(`Unexpected high-similarity tabs found: ${JSON.stringify(unexpectedPairs, null, 2)}`);
}
