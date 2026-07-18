#!/usr/bin/env node
import process from "node:process";

import { analyzeProject, formatReport } from "./frontend-architecture.mjs";

const args = new Set(process.argv.slice(2));
const known = new Set(["--json", "--require-zero-debt"]);
const unknown = [...args].filter((arg) => !known.has(arg));
if (unknown.length > 0) {
  console.error(`unknown architecture check option: ${unknown.join(", ")}`);
  process.exit(2);
}

const report = analyzeProject({
  rootDir: process.cwd(),
  requireZeroDebt: args.has("--require-zero-debt")
});

if (args.has("--json")) console.log(JSON.stringify(report, null, 2));
else console.log(formatReport(report));

if (!report.ok) process.exit(1);
