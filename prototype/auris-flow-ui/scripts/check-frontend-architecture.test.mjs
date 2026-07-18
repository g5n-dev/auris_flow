import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { analyzeProject, formatReport, hashImportSymbols } from "./frontend-architecture.mjs";

const roots = new Set();
const updateScript = fileURLToPath(new URL("./update-frontend-architecture-debt.mjs", import.meta.url));

function createProject(files, options = {}) {
  const rootDir = mkdtempSync(join(tmpdir(), "auris-architecture-"));
  roots.add(rootDir);
  write(rootDir, "tsconfig.json", JSON.stringify({
    compilerOptions: {
      target: "ES2020",
      module: "ESNext",
      moduleResolution: "Node",
      jsx: "react-jsx",
      noEmit: true,
      strict: true,
      baseUrl: ".",
      paths: options.paths ?? {}
    },
    include: ["src"]
  }));
  for (const [path, source] of Object.entries(files)) write(rootDir, path, source);
  if (options.debt) {
    write(rootDir, "scripts/frontend-architecture-debt.json", JSON.stringify(options.debt));
  }
  return rootDir;
}

function write(rootDir, path, source) {
  const target = join(rootDir, path);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, source);
}

function analyze(files, options = {}) {
  const rootDir = createProject(files, options);
  return analyzeProject({
    rootDir,
    debtPath: options.debt ? "scripts/frontend-architecture-debt.json" : null,
    policy: options.policy,
    requireZeroDebt: options.requireZeroDebt ?? false
  });
}

function codes(report) {
  return report.errors.map((error) => error.code);
}

test.after(() => {
  for (const root of roots) rmSync(root, { recursive: true, force: true });
});

test("合法的 shared、api、feature 依赖通过", () => {
  const report = analyze({
    "src/shared/contracts/run.ts": "export type Run = { id: string };\n",
    "src/api/runs.ts": "import type { Run } from '../shared/contracts/run'; export const load = (): Run => ({ id: '1' });\n",
    "src/features/home/index.ts": "export { HomeModule } from './HomeModule';\n",
    "src/features/home/HomeModule.tsx": "import { load } from '../../api/runs'; export function HomeModule() { return <div>{load().id}</div>; }\n"
  });
  assert.equal(formatReport(report), "frontend architecture ok");
});

test("运行时、type-only、re-export 与动态 import 循环都被发现", () => {
  const cases = [
    {
      "src/shared/a.ts": "import './b';\n",
      "src/shared/b.ts": "import './c';\n",
      "src/shared/c.ts": "import './a';\n"
    },
    {
      "src/shared/a.ts": "import type { B } from './b'; export type A = B;\n",
      "src/shared/b.ts": "import type { A } from './a'; export type B = A;\n"
    },
    {
      "src/shared/a.ts": "export * from './b';\n",
      "src/shared/b.ts": "export * from './a';\n"
    },
    {
      "src/shared/a.ts": "export const a = () => import('./b');\n",
      "src/shared/b.ts": "export const b = () => import('./a');\n"
    }
  ];
  for (const files of cases) assert.ok(codes(analyze(files)).includes("CYCLE"));
});

test("自环和非字面量动态 import 被拒绝", () => {
  const selfLoop = analyze({ "src/shared/self.ts": "import './self';\n" });
  assert.ok(codes(selfLoop).includes("CYCLE"));

  const escaped = analyze({
    "src/features/home/HomeModule.tsx": "const target = './part'; export const load = () => import(target);\n",
    "src/features/home/part.ts": "export const value = 1;\n"
  });
  assert.ok(codes(escaped).includes("NON_LITERAL_DYNAMIC_IMPORT"));
});

test("shared/API 越层和 feature 横向内部导入被拒绝", () => {
  const report = analyze({
    "src/shared/bad.ts": "import '../../src/features/home/HomeModule';\n",
    "src/api/bad.ts": "import '../workspace/ModuleWorkspace';\n",
    "src/workspace/ModuleWorkspace.tsx": "export const ModuleWorkspace = () => null;\n",
    "src/features/home/HomeModule.tsx": "export const HomeModule = () => null;\n",
    "src/features/data/DataModule.tsx": "import '../home/HomeModule'; export const DataModule = () => null;\n"
  });
  assert.ok(codes(report).filter((code) => code === "LAYER_VIOLATION").length >= 2);
  assert.ok(codes(report).includes("FEATURE_INTERNAL_IMPORT"));
});

test("feature 公共入口可被 workspace 引用，但内部实现不可被引用", () => {
  const good = analyze({
    "src/features/home/index.ts": "export { HomeModule } from './HomeModule';\n",
    "src/features/home/HomeModule.tsx": "export const HomeModule = () => null;\n",
    "src/workspace/ModuleWorkspace.tsx": "export const load = () => import('../features/home');\n"
  });
  assert.equal(good.ok, true, formatReport(good));

  const eager = analyze({
    "src/features/home/index.ts": "export { HomeModule } from './HomeModule';\n",
    "src/features/home/HomeModule.tsx": "export const HomeModule = () => null;\n",
    "src/workspace/ModuleWorkspace.tsx": "import { HomeModule } from '../features/home'; export const x = HomeModule;\n"
  });
  assert.ok(codes(eager).includes("FEATURE_MUST_BE_LAZY"));

  const typeOnly = analyze({
    "src/features/home/index.ts": "export type HomeProps = { title: string };\n",
    "src/workspace/ModuleWorkspace.tsx": "import type { HomeProps } from '../features/home'; export type Props = HomeProps;\n"
  });
  assert.equal(typeOnly.ok, true, formatReport(typeOnly));

  const bad = analyze({
    "src/features/home/HomeModule.tsx": "export const HomeModule = () => null;\n",
    "src/workspace/ModuleWorkspace.tsx": "import { HomeModule } from '../features/home/HomeModule'; export const x = HomeModule;\n"
  });
  assert.ok(codes(bad).includes("FEATURE_INTERNAL_IMPORT"));

  const nestedBarrel = analyze({
    "src/features/home/components/index.ts": "export { HomeCard } from './HomeCard';\n",
    "src/features/home/components/HomeCard.tsx": "export const HomeCard = () => null;\n",
    "src/workspace/ModuleWorkspace.tsx": "import { HomeCard } from '../features/home/components'; export const x = HomeCard;\n"
  });
  assert.ok(codes(nestedBarrel).includes("FEATURE_INTERNAL_IMPORT"));
});

test("任何反向依赖 App 都失败；main 只能字面量动态加载 default App", () => {
  const reverse = analyze({
    "src/App.tsx": "export default function App() { return null; }\n",
    "src/shared/bad.ts": "import type App from '../App'; export type Bad = typeof App;\n"
  });
  assert.ok(codes(reverse).includes("APP_REVERSE_DEPENDENCY"));

  const staticMain = analyze({
    "src/App.tsx": "export default function App() { return null; }\n",
    "src/main.tsx": "import App from './App'; void App;\n"
  });
  assert.ok(codes(staticMain).includes("APP_BOOTSTRAP_IMPORT"));

  const dynamicMain = analyze({
    "src/App.tsx": "export default function App() { return null; }\n",
    "src/main.tsx": "void import('./App').then(({ default: App }) => App);\n"
  });
  assert.equal(dynamicMain.ok, true, formatReport(dynamicMain));
});

test("path alias 无法绕过分层门禁", () => {
  const report = analyze({
    "src/shared/bad.ts": "import '@home/HomeModule';\n",
    "src/features/home/HomeModule.tsx": "export const HomeModule = () => null;\n"
  }, { paths: { "@home/*": ["src/features/home/*"] } });
  assert.ok(codes(report).includes("LAYER_VIOLATION"));
});

test("文件、入口、hook/service 和函数规模边界严格执行", () => {
  const lines = (count, text = "export const value = 1;") => Array.from({ length: count }, () => text).join("\n");
  const report = analyze({
    "src/features/home/index.ts": lines(401, "export type X = string;"),
    "src/features/home/hooks/useLarge.ts": lines(301, "export type X = string;"),
    "src/features/home/use-data.ts": lines(301, "export type X = string;"),
    "src/features/home/HomeService.ts": lines(301, "export type X = string;"),
    "src/features/home/buildHomeActions.ts": lines(301, "export type X = string;"),
    "src/features/home/buildHomeOperations.ts": lines(301, "export type X = string;"),
    "src/features/home/HomeGateway.ts": lines(301, "export type X = string;"),
    "src/features/home/buildHomePollingActions.ts": lines(301, "export type X = string;"),
    "src/features/home/services/large.service.ts": lines(301, "export type X = string;"),
    "src/features/home/HomeModule.tsx": lines(401, "export type X = string;"),
    "src/features/home/fixtures.ts": lines(801, "export type X = string;"),
    "src/api/huge.ts": lines(801, "export type X = string;"),
    "src/features/home/Large.ts": `export function large() {\n${Array.from({ length: 400 }, () => "void 0;").join("\n")}\n}\n`
  });
  assert.ok(codes(report).filter((code) => code === "FILE_SIZE").length >= 12);
  assert.ok(codes(report).includes("FUNCTION_SIZE"));
});

test("App 最终规模、useState 与直接 API import 门禁生效", () => {
  const source = [
    "import { useState } from 'react';",
    "import { apiRequest } from './api/client';",
    "export default function App() {",
    ...Array.from({ length: 6 }, (_, index) => `  const [v${index}] = useState(${index});`),
    "  void apiRequest; return null;",
    "}"
  ].join("\n");
  const report = analyze({
    "src/App.tsx": source,
    "src/api/client.ts": "export const apiRequest = 1;\n"
  });
  assert.ok(codes(report).includes("APP_USE_STATE"));
  assert.ok(codes(report).includes("APP_API_IMPORT"));

  const aliased = analyze({
    "src/App.tsx": [
      "import { useState as state } from 'react';",
      "export default function App() {",
      ...Array.from({ length: 6 }, (_, index) => `state(${index});`),
      "return null; }"
    ].join("\n")
  });
  assert.ok(codes(aliased).includes("APP_USE_STATE"));

  const unrelated = analyze({
    "src/App.tsx": "const store = { useState: (_v: number) => 1 }; export default function App() { store.useState(1); store.useState(2); store.useState(3); store.useState(4); store.useState(5); store.useState(6); return null; }\n"
  });
  assert.ok(!codes(unrelated).includes("APP_USE_STATE"));
});

test("大杂烩文件名失败，具备领域语义的文件名通过", () => {
  const bad = analyze({ "src/features/home/utils.ts": "export const x = 1;\n" });
  assert.ok(codes(bad).includes("GENERIC_FILENAME"));
  const good = analyze({ "src/features/home/deepLinks.ts": "export const x = 1;\n" });
  assert.equal(good.ok, true, formatReport(good));
});

test("冻结 legacy 可维持但不可增长", () => {
  const policy = {
    frozenLegacy: {
      "src/features/legacy/LegacyModule.tsx": { maxLines: 2, skipFunctionLimit: true }
    }
  };
  const good = analyze({
    "src/features/legacy/LegacyModule.tsx": "export function LegacyModule() {\n return null; }\n"
  }, { policy });
  assert.equal(good.ok, true, formatReport(good));
  const bad = analyze({
    "src/features/legacy/LegacyModule.tsx": "export function LegacyModule() {\n void 0;\n return null; }\n"
  }, { policy });
  assert.ok(codes(bad).includes("FROZEN_LEGACY_GROWTH"));
});

test("迁移债务只允许收缩，且最终模式要求清零", () => {
  const debt = {
    schemaVersion: 1,
    transitions: {
      "src/App.tsx": {
        maxLines: 20,
        maxDirectUseStateCalls: 2,
        directApiImports: {
          count: 1,
          setSha256: hashImportSymbols(["apiRequest"]),
          allowedSymbols: ["apiRequest"]
        },
        allowedBoundaryTargets: ["src/api/client.ts"]
      }
    }
  };
  const files = {
    "src/App.tsx": "import { useState } from 'react'; import { apiRequest } from './api/client'; export default function App() { useState(1); void apiRequest; return null; }\n",
    "src/api/client.ts": "export const apiRequest = 1; export const newCall = 2;\n"
  };
  const transitional = analyze(files, { debt });
  assert.equal(transitional.ok, true, formatReport(transitional));
  const strict = analyze(files, { debt, requireZeroDebt: true });
  assert.ok(codes(strict).includes("TRANSITION_DEBT"));

  const growth = analyze({
    ...files,
    "src/App.tsx": "import { apiRequest, newCall } from './api/client'; export default function App() { void apiRequest; void newCall; return null; }\n"
  }, { debt });
  assert.ok(codes(growth).includes("APP_API_IMPORT_GROWTH"));

  const dynamicGrowth = analyze({
    ...files,
    "src/App.tsx": "export default function App() { void import('./api/client'); return null; }\n"
  }, { debt });
  assert.ok(codes(dynamicGrowth).includes("APP_API_IMPORT_GROWTH"));

  const reExportGrowth = analyze({
    ...files,
    "src/App.tsx": "export { newCall } from './api/client'; export default function App() { return null; }\n"
  }, { debt });
  assert.ok(codes(reExportGrowth).includes("APP_API_IMPORT_GROWTH"));

  const importTypeGrowth = analyze({
    ...files,
    "src/App.tsx": "type Client = import('./api/client'); export default function App() { return null as Client; }\n"
  }, { debt });
  assert.ok(codes(importTypeGrowth).includes("APP_API_IMPORT_GROWTH"));

  const requireGrowth = analyze({
    ...files,
    "src/App.tsx": "declare const require: (path: string) => unknown; require('./api/client'); export default function App() { return null; }\n"
  }, { debt });
  assert.ok(codes(requireGrowth).includes("APP_API_IMPORT_GROWTH"));

  const sideEffectGrowth = analyze({
    ...files,
    "src/App.tsx": "import './api/client'; export default function App() { return null; }\n"
  }, { debt });
  assert.ok(codes(sideEffectGrowth).includes("APP_API_IMPORT_GROWTH"));

  const malformedDebt = structuredClone(debt);
  malformedDebt.transitions["src/App.tsx"].directApiImports.count = 2;
  assert.ok(codes(analyze(files, { debt: malformedDebt })).includes("DEBT_SCHEMA"));
});

test("更新器在最终策略通过时删除 App 迁移债务", () => {
  const debt = {
    schemaVersion: 1,
    transitions: {
      "src/App.tsx": {
        maxLines: 20,
        maxDirectUseStateCalls: 0,
        directApiImports: {
          count: 0,
          setSha256: hashImportSymbols([]),
          allowedSymbols: []
        },
        allowedBoundaryTargets: []
      }
    }
  };
  const rootDir = createProject({
    "src/App.tsx": "export default function App() { return null; }\n"
  }, { debt });
  const result = spawnSync(process.execPath, [updateScript], { cwd: rootDir, encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const updated = JSON.parse(readFileSync(join(rootDir, "scripts/frontend-architecture-debt.json"), "utf8"));
  assert.deepEqual(updated.transitions, {});
  const report = analyzeProject({ rootDir, debtPath: null, requireZeroDebt: true });
  assert.equal(report.ok, true, formatReport(report));
});

test("更新器不会清除仍依赖边界豁免的 App 迁移债务", () => {
  const debt = {
    schemaVersion: 1,
    transitions: {
      "src/App.tsx": {
        maxLines: 20,
        maxDirectUseStateCalls: 0,
        directApiImports: {
          count: 0,
          setSha256: hashImportSymbols([]),
          allowedSymbols: []
        },
        allowedBoundaryTargets: ["src/features/home/index.ts"]
      }
    }
  };
  const rootDir = createProject({
    "src/App.tsx": "import { HomeModule } from './features/home'; export default function App() { return HomeModule(); }\n",
    "src/features/home/index.ts": "export { HomeModule } from './HomeModule';\n",
    "src/features/home/HomeModule.tsx": "export function HomeModule() { return null; }\n"
  }, { debt });
  const result = spawnSync(process.execPath, [updateScript], { cwd: rootDir, encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const updated = JSON.parse(readFileSync(join(rootDir, "scripts/frontend-architecture-debt.json"), "utf8"));
  assert.ok(updated.transitions["src/App.tsx"]);
  assert.deepEqual(updated.transitions["src/App.tsx"].allowedBoundaryTargets, ["src/features/home/index.ts"]);
});

test("tsconfig exclude 不能让 src 文件逃逸门禁", () => {
  const rootDir = createProject({
    "src/shared/visible.ts": "export const visible = true;\n",
    "src/features/hidden/HiddenModule.tsx": Array.from({ length: 401 }, () => "export type X = string;").join("\n")
  });
  write(rootDir, "tsconfig.json", JSON.stringify({
    compilerOptions: { target: "ES2020", module: "ESNext", moduleResolution: "Node", noEmit: true },
    include: ["src"],
    exclude: ["src/features/hidden/**"]
  }));
  const report = analyzeProject({ rootDir, debtPath: null });
  assert.ok(codes(report).includes("FILE_SIZE"));
});

test("报告排序是确定性的", () => {
  const files = {
    "src/shared/utils.ts": "import '../features/z/Z';\n",
    "src/features/z/Z.ts": "export const z = 1;\n",
    "src/features/a/helpers.ts": "export const a = 1;\n"
  };
  const first = formatReport(analyze(files));
  const second = formatReport(analyze(files));
  assert.equal(first, second);
});
