import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  controllerGroups,
  controllerPropertyNames,
  generateController,
  inlineFeatureControllers,
  transformControllerConsumers
} from "./inline-feature-controller.mjs";

test("controller 分组按真实调用顺序内联，而不是按 import 顺序", () => {
  const directory = mkdtempSync(join(tmpdir(), "auris-controller-order-"));
  const controllerPath = join(directory, "useExampleController.ts");
  writeFileSync(join(directory, "groups.ts"), [
    "export function buildFirst(props) { return { first: props.first }; }",
    "export function buildSecond(props) { return { second: props.second }; }"
  ].join("\n"));
  writeFileSync(controllerPath, [
    'import { buildSecond, buildFirst } from "./groups";',
    "export function useExampleController(props) {",
    "  const first = buildFirst(props);",
    "  const second = buildSecond({ ...props, ...first });",
    "  return { ...first, ...second };",
    "}"
  ].join("\n"));

  assert.deepEqual(
    controllerGroups(controllerPath, "useExampleController").map((group) => group.functionName),
    ["buildFirst", "buildSecond"]
  );
});

test("只收集由组件 controller 参数绑定的属性", () => {
  const directory = mkdtempSync(join(tmpdir(), "auris-controller-scan-"));
  writeFileSync(join(directory, "View.tsx"), [
    "export function View({ controller }) { return controller.activeTab; }",
    "export function Unrelated() {",
    '  const controller = { activeTab: "local", secret: true };',
    "  return controller.secret;",
    "}"
  ].join("\n"));

  assert.deepEqual(controllerPropertyNames(directory), ["activeTab"]);
});

test("别名转换尊重 controller 的词法作用域", () => {
  const source = [
    "export function View({ controller }) {",
    "  const { activeTab } = controller;",
    "  const values = [1].map((controller) => controller.activeTab);",
    "  return activeTab + controller[\"notice\"] + values[0];",
    "}",
    "export function Unrelated() {",
    '  const controller = { activeTab: "local" };',
    "  return controller.activeTab;",
    "}"
  ].join("\n");
  const transformed = transformControllerConsumers(
    source,
    "View.tsx",
    new Map([["activeTab", "a"], ["notice", "b"]])
  );

  assert.match(transformed, /const \{ a: activeTab \} = controller;/);
  assert.match(transformed, /controller\.b/);
  assert.match(transformed, /\(controller\) => controller\.activeTab/);
  assert.match(transformed, /return controller\.activeTab;/);
  assert.doesNotMatch(transformed, /\(controller\) => controller\.a\b/);
});

test("Canvas 内联产物保留节点图标映射声明", () => {
  const root = process.cwd();
  const plugin = inlineFeatureControllers(root);
  const virtualId = plugin.resolveId(
    "./controller/useCanvasController",
    join(root, "src/features/canvas/CanvasModule.tsx")
  );
  assert.equal(typeof virtualId, "string");
  const loaded = plugin.load.call({ addWatchFile() {} }, virtualId);
  assert.match(loaded.code, /const canvasNodeIcons\b/);
  assert.match(loaded.code, /icon: canvasNodeIcons\[iconKey\]/);
  assert.doesNotMatch(loaded.code, /(?<!\.)\bscope\b/);
});

test("Insights 内联产物不得遗留自由 scope", () => {
  const root = process.cwd();
  const plugin = inlineFeatureControllers(root);
  const virtualId = plugin.resolveId(
    "./controller/useInsightsController",
    join(root, "src/features/insights/InsightsModule.tsx")
  );
  assert.equal(typeof virtualId, "string");
  const loaded = plugin.load.call({ addWatchFile() {} }, virtualId);
  assert.match(loaded.code, /const buildInsightBusinessCharts\b/);
  assert.match(loaded.code, /const buildInsightStoreSalesCharts\b/);
  assert.match(loaded.code, /const buildInsightGovernanceCharts\b/);
  assert.doesNotMatch(
    loaded.code,
    /buildInsight(?:Business|StoreSales|Governance)Charts\(scope\)/
  );
});

test("内联分组不得在形参解构消除后继续直接引用形参", () => {
  const directory = mkdtempSync(join(tmpdir(), "auris-controller-parameter-"));
  const controllerPath = join(directory, "useExampleController.ts");
  writeFileSync(join(directory, "helper.ts"), "export const helper = (value) => value.x;\n");
  writeFileSync(join(directory, "groups.ts"), [
    'import { helper } from "./helper";',
    "export function buildFirst(scope) {",
    "  const { x } = scope;",
    "  const first = helper(scope);",
    "  return { first };",
    "}",
    "export function buildSecond(scope) {",
    "  const { first } = scope;",
    "  return { second: first };",
    "}"
  ].join("\n"));
  writeFileSync(controllerPath, [
    'import { buildFirst, buildSecond } from "./groups";',
    "export function useExampleController(props) {",
    "  const first = buildFirst(props);",
    "  const second = buildSecond({ ...props, ...first });",
    "  return { ...first, ...second };",
    "}"
  ].join("\n"));

  assert.throws(() => generateController({
    controllerPath,
    exportName: "useExampleController",
    propertyMap: new Map([["first", "a"], ["second", "b"]])
  }), /buildFirst.*直接引用形参 scope/);
});

test("内联分组不得依赖会被虚拟模块丢弃的同文件模块级运行时绑定", () => {
  const directory = mkdtempSync(join(tmpdir(), "auris-controller-module-binding-"));
  const controllerPath = join(directory, "useExampleController.ts");
  writeFileSync(join(directory, "groups.ts"), [
    "const helper = (value) => value * 2;",
    "export function buildFirst(scope) {",
    "  const { x } = scope;",
    "  const first = helper(x);",
    "  return { first };",
    "}",
    "export function buildSecond(scope) {",
    "  const { first } = scope;",
    "  return { second: first };",
    "}"
  ].join("\n"));
  writeFileSync(controllerPath, [
    'import { buildFirst, buildSecond } from "./groups";',
    "export function useExampleController(props) {",
    "  const first = buildFirst(props);",
    "  const second = buildSecond({ ...props, ...first });",
    "  return { ...first, ...second };",
    "}"
  ].join("\n"));

  assert.throws(() => generateController({
    controllerPath,
    exportName: "useExampleController",
    propertyMap: new Map([["first", "a"], ["second", "b"]])
  }), /buildFirst.*同文件模块级运行时绑定.*helper/);
});

test("内联分组的额外形参继续由 props 注入", () => {
  const directory = mkdtempSync(join(tmpdir(), "auris-controller-extra-parameter-"));
  const controllerPath = join(directory, "useExampleController.ts");
  writeFileSync(join(directory, "groups.ts"), [
    "export function buildFirst(scope, multiplier) {",
    "  const { x } = scope;",
    "  const first = x * multiplier;",
    "  return { first };",
    "}",
    "export function buildSecond(scope) {",
    "  const { first } = scope;",
    "  return { second: first };",
    "}"
  ].join("\n"));
  writeFileSync(controllerPath, [
    'import { buildFirst, buildSecond } from "./groups";',
    "export function useExampleController(props) {",
    "  const first = buildFirst(props, props.multiplier);",
    "  const second = buildSecond({ ...props, ...first });",
    "  return { ...first, ...second };",
    "}"
  ].join("\n"));

  const generated = generateController({
    controllerPath,
    exportName: "useExampleController",
    propertyMap: new Map([["first", "a"], ["second", "b"]])
  });
  assert.match(generated.code, /const \{ multiplier, x \} = props;/);
  assert.match(generated.code, /const first = x \* multiplier;/);
});
