#!/usr/bin/env node

import { scanProductionActionTruth } from "./production-action-truth-policy.mjs";

const findings = await scanProductionActionTruth();
if (findings.length) {
  console.error(JSON.stringify({ ok: false, findings }, null, 2));
  process.exitCode = 1;
} else {
  console.log(JSON.stringify({ ok: true, findings: [] }));
}
