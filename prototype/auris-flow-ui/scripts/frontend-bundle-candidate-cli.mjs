import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import { basename, dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { frontendBundleBudgetPolicy } from "./frontend-bundle-budget-policy.mjs";
import {
  canonicalJson,
  createCandidateDocument,
  requireGitObject,
  requireRealDirectory,
  sha256,
  validateCandidateBundleReport,
  verifyCandidateDirectory,
  writeCandidateDirectory
} from "./frontend-bundle-candidate.mjs";
import {
  buildDistInventory,
  distInventorySha256,
  loadFrontendBundleLock,
  sha256File
} from "./frontend-bundle-lock.mjs";

const uiRoot = resolve(fileURLToPath(new URL("..", import.meta.url)));
const repositoryRoot = resolve(uiRoot, "../..");
const defaultDistDir = join(uiRoot, "dist");
const checkerPath = join(uiRoot, "scripts/check-bundle-budget.mjs");

function gitText(...args) {
  return execFileSync("git", args, {
    cwd: repositoryRoot,
    encoding: "utf8"
  }).trim();
}

function checkoutState() {
  return {
    commit: gitText("rev-parse", "--verify", "HEAD^{commit}"),
    repositoryTree: gitText("rev-parse", "--verify", "HEAD^{tree}"),
    frontendTree: gitText(
      "rev-parse",
      "--verify",
      "HEAD:prototype/auris-flow-ui"
    ),
    status: gitText("status", "--porcelain=v1", "--untracked-files=all")
  };
}

function requireCleanCheckout(expectedCommit = null) {
  const state = checkoutState();
  if (state.status) {
    throw new Error(
      `frontend bundle candidate requires a clean tree:\n${state.status}`
    );
  }
  if (expectedCommit !== null && state.commit !== expectedCommit) {
    throw new Error(
      `source commit mismatch: expected ${expectedCommit}, got ${state.commit}`
    );
  }
  return state;
}

function runBundleChecker() {
  const result = spawnSync(process.execPath, [checkerPath], {
    cwd: uiRoot,
    encoding: "utf8",
    env: {
      ...process.env,
      AURIS_REQUIRE_APPROVED_BUNDLE: "1"
    },
    maxBuffer: 64 * 1024 * 1024
  });
  if (result.error) throw result.error;
  if (result.signal) {
    throw new Error(`bundle checker terminated by ${result.signal}`);
  }
  let report;
  try {
    report = JSON.parse(result.stdout);
  } catch (error) {
    throw new Error(`bundle checker did not emit JSON: ${error.message}`);
  }
  if (result.status === 0 || report.status !== "failed") {
    throw new Error(
      "bundle candidate is unnecessary because the approved gate already passes"
    );
  }
  if (result.status !== 1) {
    throw new Error(
      `bundle checker exited unexpectedly with ${result.status}: ${result.stderr}`
    );
  }
  const normalized = structuredClone(report);
  delete normalized.generatedAt;
  const failures = validateCandidateBundleReport(normalized);
  if (failures.length) {
    throw new Error(
      `bundle report is not candidate-eligible: ${failures.join("; ")}`
    );
  }
  return normalized;
}

function readCanonicalInput(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

function currentBuildContract() {
  const vitePackage = readCanonicalInput(
    join(uiRoot, "node_modules/vite/package.json")
  );
  return {
    node_version: process.version,
    npm_version: execFileSync("npm", ["--version"], {
      encoding: "utf8"
    }).trim(),
    vite_version: vitePackage.version,
    package_lock_sha256: sha256File(join(uiRoot, "package-lock.json")),
    vite_config_sha256: sha256File(join(uiRoot, "vite.config.ts")),
    checker_sha256: sha256File(checkerPath),
    route_scenarios_sha256: sha256File(
      join(uiRoot, "scripts/bundle-route-scenarios.mjs")
    ),
    precompression_sha256: sha256File(
      join(uiRoot, "scripts/precompressed-assets.mjs")
    ),
    policy_sha256: sha256File(
      join(uiRoot, "scripts/frontend-bundle-budget-policy.mjs")
    )
  };
}

function currentCandidateInputs(distDir = defaultDistDir) {
  const report = runBundleChecker();
  const viteManifest = readCanonicalInput(join(distDir, ".vite/manifest.json"));
  const brotliManifest = readCanonicalInput(
    join(distDir, ".vite/brotli-manifest.json")
  );
  const distInventory = buildDistInventory(distDir);
  const rendered = {
    report: canonicalJson(report),
    viteManifest: canonicalJson(viteManifest),
    brotliManifest: canonicalJson(brotliManifest),
    distInventory: canonicalJson(distInventory)
  };
  return {
    report,
    viteManifest,
    brotliManifest,
    distInventory,
    artifacts: {
      bundle_report_sha256: sha256(rendered.report),
      vite_manifest_sha256: sha256(rendered.viteManifest),
      brotli_manifest_sha256: sha256(rendered.brotliManifest),
      dist_inventory_sha256: sha256(rendered.distInventory)
    }
  };
}

function requireDiagnosticOutput(outputDir) {
  const buildRoot = join(repositoryRoot, "build");
  mkdirSync(buildRoot, { recursive: true, mode: 0o755 });
  requireRealDirectory(buildRoot, "repository build directory");
  const output = resolve(outputDir);
  if (
    dirname(output) !== buildRoot ||
    !basename(output).startsWith("frontend-bundle-candidate")
  ) {
    throw new Error(
      "candidate output must be a direct build/frontend-bundle-candidate* directory"
    );
  }
  return output;
}

async function generateCandidate({ sourceCommit, output }) {
  requireGitObject(sourceCommit, "source commit");
  const before = requireCleanCheckout(sourceCommit);
  const inputs = currentCandidateInputs();
  if (
    distInventorySha256(inputs.distInventory) !==
    inputs.artifacts.dist_inventory_sha256
  ) {
    throw new Error("dist inventory canonical digest contract drifted");
  }
  const buildContract = currentBuildContract();
  const bundleLock = loadFrontendBundleLock();
  const candidate = createCandidateDocument({
    source: before,
    report: inputs.report,
    bundleLock,
    comparisonSnapshot: frontendBundleBudgetPolicy.comparisonSnapshot,
    buildContract,
    artifacts: inputs.artifacts
  });
  const outputDir = requireDiagnosticOutput(output);
  const verified = await writeCandidateDirectory(outputDir, {
    candidate,
    report: inputs.report,
    viteManifest: inputs.viteManifest,
    brotliManifest: inputs.brotliManifest,
    distInventory: inputs.distInventory
  });
  const after = requireCleanCheckout(sourceCommit);
  if (
    after.repositoryTree !== before.repositoryTree ||
    after.frontendTree !== before.frontendTree
  ) {
    throw new Error("candidate generation changed the bound Git trees");
  }
  return { output: outputDir, ...verified };
}

async function verifyCandidateAgainstCheckout(candidateDir) {
  const verified = await verifyCandidateDirectory(candidateDir);
  const state = requireCleanCheckout(verified.candidate.source.commit);
  if (state.repositoryTree !== verified.candidate.source.repository_tree) {
    throw new Error("candidate repository tree does not match checkout");
  }
  if (state.frontendTree !== verified.candidate.source.frontend_tree) {
    throw new Error("candidate frontend tree does not match checkout");
  }
  const inputs = currentCandidateInputs();
  if (
    canonicalJson(inputs.artifacts) !==
    canonicalJson(verified.candidate.artifacts)
  ) {
    throw new Error("candidate artifacts do not match the rebuilt checkout");
  }
  if (
    canonicalJson(currentBuildContract()) !==
    canonicalJson(verified.candidate.build_contract)
  ) {
    throw new Error(
      "candidate build contract does not match the checkout toolchain"
    );
  }
  if (
    sha256(canonicalJson(inputs.report)) !==
    verified.candidate.artifacts.bundle_report_sha256
  ) {
    throw new Error(
      "candidate bundle report does not match the rebuilt checkout"
    );
  }
  return verified;
}

function parseOptions(argv, allowed) {
  const options = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (
      !name?.startsWith("--") ||
      value === undefined ||
      value.startsWith("--")
    ) {
      throw new Error("CLI options must use --name value pairs");
    }
    const key = name.slice(2);
    if (!allowed.includes(key) || key in options) {
      throw new Error(`unknown or duplicate option: ${name}`);
    }
    options[key] = value;
  }
  for (const key of allowed) {
    if (!options[key]) throw new Error(`missing required option: --${key}`);
  }
  return options;
}

async function main(argv) {
  const [command, ...rest] = argv;
  if (command === "generate") {
    const options = parseOptions(rest, ["source-commit", "output"]);
    const result = await generateCandidate({
      sourceCommit: options["source-commit"],
      output: options.output
    });
    process.stdout.write(
      canonicalJson({
        candidate_sha256: result.candidate_sha256,
        output: relative(repositoryRoot, result.output),
        source_commit: result.candidate.source.commit,
        status: result.candidate.status
      })
    );
    return;
  }
  if (command === "verify") {
    const options = parseOptions(rest, ["candidate-dir"]);
    const result = await verifyCandidateAgainstCheckout(
      options["candidate-dir"]
    );
    process.stdout.write(
      canonicalJson({
        candidate_sha256: result.candidate_sha256,
        source_commit: result.candidate.source.commit,
        status: "verified"
      })
    );
    return;
  }
  throw new Error(
    "usage: frontend-bundle-candidate-cli.mjs <generate|verify> ..."
  );
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`frontend bundle candidate: ${error.message}\n`);
    process.exitCode = 1;
  });
}
