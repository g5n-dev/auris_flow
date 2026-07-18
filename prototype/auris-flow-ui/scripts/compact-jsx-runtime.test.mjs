import assert from "node:assert/strict";
import test from "node:test";
import ts from "typescript";

import {
  compactJsxRuntimeChunk,
  compactJsxRuntimeChunkForBrotli
} from "./compact-jsx-runtime.mjs";

const runtimeImport =
  'import { jsx as j, jsxs as s, Fragment as f } from "./_auris-react-jsx-runtime-test.js";';

function executable(source) {
  return source.replace(
    runtimeImport,
    "const { jsx: j, jsxs: s, Fragment: f } = runtime;"
  );
}

function execute(source, runtimeOverride) {
  const calls = [];
  const runtime = runtimeOverride ?? {
    Fragment: "fragment",
    jsx(type, props, key) {
      calls.push({ method: "jsx", length: arguments.length, type, props, key });
      return { type, props, key };
    },
    jsxs(type, props, key) {
      calls.push({ method: "jsxs", length: arguments.length, type, props, key });
      return { type, props, key };
    }
  };
  const run = Function("runtime", "calls", `${executable(source)}; return output;`);
  return { output: run(runtime, calls), calls };
}

function compact(source) {
  return compactJsxRuntimeChunk(source, "test.js");
}

test("压缩直接 JSX ESM 绑定并保持 props、key、arity 与嵌套结果", () => {
  const children = Array.from({ length: 20 }, (_, index) =>
    `j('span', { title: 'item-${index}', children: (sideEffects.push('child-${index}'), '${index}') }, 'k${index}')`
  );
  const source = [
    runtimeImport,
    "const sideEffects = [];",
    "const output = s(f, { id: (sideEffects.push('props'), 'root'), children: [",
    children.join(",\n"),
    "] }, 'root-key');",
    "output.sideEffects = sideEffects;"
  ].join("\n");
  const result = compact(source);
  assert.equal(result.changed, true);
  assert.deepEqual(execute(result.code), execute(source));
  assert.ok(result.code.length < source.length);
  assert.doesNotMatch(result.code, /children:\s*\(sideEffects/);
  assert.equal(result.code.match(/'span'/g)?.length, 1);
  assert.equal(result.code.match(/title:/g)?.length ?? 0, 0);
});

test("保持二参数与显式 undefined key 的 arguments.length", () => {
  const source = [
    runtimeImport,
    "const output = [",
    "  j('div', { children: 'two' }),",
    "  s('div', { children: ['three'] }, undefined),",
    "  ...Array.from({ length: 18 }, (_, index) => j('i', { children: index }))",
    "];"
  ].join("\n");
  const before = execute(source);
  const after = execute(compact(source).code);
  assert.deepEqual(after, before);
  assert.equal(after.calls[0].length, 2);
  assert.equal(after.calls[1].length, 3);
});

test("重复 JSX 字符串值只提升为模块常量且保持值、key 与嵌套对象语义", () => {
  const repeated = Array.from({ length: 20 }, (_, index) =>
    `j('button', { title: 'shared-long-value', meta: { label: 'shared-long-value' }, children: 'shared-long-value' }, 'shared-long-value')`
  ).join(",");
  const source = [runtimeImport, `const output = [${repeated}];`].join("\n");
  const result = compact(source);
  assert.equal(result.changed, true);
  assert.deepEqual(execute(result.code), execute(source));
  assert.equal(result.code.match(/shared-long-value/g)?.length, 1);
});

test("children 始终是可写、可枚举、可配置的 own data property", () => {
  const source = [
    runtimeImport,
    "const setterCalls = [];",
    "const proto = { set children(value) { setterCalls.push(value); } };",
    "const output = j('div', { __proto__: proto, get children() { return 'old'; }, children: undefined });",
    "output.setterCalls = setterCalls;",
    "output.descriptor = Object.getOwnPropertyDescriptor(output.props, 'children');",
    "output.keys = Object.keys(output.props);"
  ].join("\n");
  const after = execute(compact(source).code).output;
  assert.deepEqual(after.setterCalls, []);
  assert.equal(Object.hasOwn(after.props, "children"), true);
  assert.deepEqual(after.descriptor, {
    value: undefined,
    writable: true,
    enumerable: true,
    configurable: true
  });
  assert.deepEqual(after.keys, ["children"]);
});

test("同名局部绑定不被改写，顶层 JSX import 仍可压缩", () => {
  const padding = Array.from({ length: 20 }, (_, index) =>
    `j('i', { children: '${index}' })`
  ).join(",");
  const source = [
    runtimeImport,
    "function local(j) { return j('local', { children: 'local-child' }); }",
    `const output = [${padding}, local((type, props) => ({ type, props }))];`
  ].join("\n");
  const result = compact(source);
  assert.equal(result.changed, true);
  assert.deepEqual(execute(result.code), execute(source));
  assert.match(result.code, /return j\('local',\s*\{ children: 'local-child' \}\)/);
});

test("非末尾 children、非对象 props、spread 参数与 Fragment 均保持原文", () => {
  const padding = Array.from({ length: 20 }, (_, index) =>
    `j('i', { children: '${index}' })`
  ).join(",");
  const source = [
    runtimeImport,
    "const props = { children: 'external' };",
    "const args = ['x', props];",
    `const output = [${padding}, j('div', { children: 'first', title: 'last' }), j('p', props), j(...args), f];`
  ].join("\n");
  const result = compact(source);
  assert.deepEqual(execute(result.code), execute(source));
  assert.match(result.code, /children:\s*'first',\s*title:\s*'last'/);
  assert.match(result.code, /j\('p',\s*props\)/);
  assert.match(result.code, /j\(\.\.\.args\)/);
});

test("注入保留 directive prologue 且无分号输入仍可解析", () => {
  const padding = Array.from({ length: 20 }, (_, index) =>
    `j('i', { children: '${index}' })`
  ).join(",");
  const source = [
    runtimeImport,
    '"use strict"',
    `const output = [${padding}]`
  ].join("\n");
  const result = compact(source);
  const parsed = ts.createSourceFile("output.js", result.code, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  assert.equal(parsed.parseDiagnostics.length, 0);
  assert.ok(result.code.indexOf('"use strict"') < result.code.indexOf("Object.defineProperty"));
  assert.deepEqual(execute(result.code), execute(source));
});

test("顶层 Object 绑定时 fail-closed", () => {
  const source = [
    runtimeImport,
    "const Object = globalThis.Object;",
    "const output = j('div', { children: 'safe' });"
  ].join("\n");
  const result = compact(source);
  assert.equal(result.changed, false);
  assert.equal(result.code, source);
});

test("Brotli 自适应选择保持 JSX 运行语义并记录真实成本", () => {
  const rows = Array.from({ length: 32 }, (_, index) =>
    `j('article', { title: 'row-${index}', children: j('span', { children: '${index}' }) }, 'key-${index}')`
  ).join(",");
  const source = [runtimeImport, `const output = [${rows}];`].join("\n");
  const result = compactJsxRuntimeChunkForBrotli(source, "adaptive.js", {
    maxBrotliCostPerRawByte: Number.POSITIVE_INFINITY
  });
  assert.equal(result.changed, true);
  assert.deepEqual(execute(result.code), execute(source));
  assert.ok(result.code.length < source.length);
  assert.ok(result.summaries.some((summary) => summary.savedRawBytes > 0));
  assert.ok(result.summaries.every((summary) => Number.isFinite(summary.brotliDeltaBytes)));
});

test("Brotli 自适应在候选超过成本阈值时保持原产物", () => {
  const rows = Array.from({ length: 24 }, (_, index) =>
    `j('section', { children: j('strong', { children: '${index}' }) })`
  ).join(",");
  const source = [runtimeImport, `const output = [${rows}];`].join("\n");
  const result = compactJsxRuntimeChunkForBrotli(source, "strict.js", {
    maxBrotliCostPerRawByte: Number.NEGATIVE_INFINITY
  });
  assert.equal(result.changed, false);
  assert.equal(result.code, source);
});
