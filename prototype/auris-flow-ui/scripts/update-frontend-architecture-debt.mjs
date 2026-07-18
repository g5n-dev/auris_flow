#!/usr/bin/env node
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import process from "node:process";

import { analyzeProject, formatReport, hashImportSymbols } from "./frontend-architecture.mjs";

const rootDir = process.cwd();
const debtPath = resolve(rootDir, "scripts/frontend-architecture-debt.json");
const report = analyzeProject({ rootDir });
if (!report.ok) {
  console.error(formatReport(report));
  process.exit(1);
}

const debt = JSON.parse(readFileSync(debtPath, "utf8"));
const transition = debt.transitions?.["src/App.tsx"];
if (!transition) {
  console.log("frontend architecture debt already zero");
  process.exit(0);
}

const finalReport = analyzeProject({
  rootDir,
  debtPath: null,
  requireZeroDebt: true
});
if (finalReport.ok) {
  delete debt.transitions["src/App.tsx"];
  writeFileSync(debtPath, `${JSON.stringify(debt, null, 2)}\n`);
  console.log("frontend architecture debt cleared: src/App.tsx satisfies final policy");
  process.exit(0);
}

const currentSymbols = report.metrics.app.directApiImports;
const activeTargets = new Set(
  report.edges
    .filter((edge) => edge.importer === "src/App.tsx")
    .map((edge) => edge.target)
);
transition.maxLines = report.metrics.app.lines;
transition.maxDirectUseStateCalls = report.metrics.app.useStateCalls;
transition.directApiImports = {
  count: currentSymbols.length,
  setSha256: hashImportSymbols(currentSymbols),
  allowedSymbols: currentSymbols
};
transition.allowedBoundaryTargets = transition.allowedBoundaryTargets
  .filter((target) => activeTargets.has(target))
  .sort();

writeFileSync(debtPath, `${JSON.stringify(debt, null, 2)}\n`);
console.log(`frontend architecture debt updated: ${transition.maxLines} lines, ${transition.maxDirectUseStateCalls} useState, ${currentSymbols.length} API symbols`);
