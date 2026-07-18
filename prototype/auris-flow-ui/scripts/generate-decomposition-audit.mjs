import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";

function parseOutput(argv) {
  const index = argv.indexOf("--output");
  if (index < 0 || !argv[index + 1]) throw new Error("用法：node scripts/generate-decomposition-audit.mjs --output <directory>");
  return resolve(argv[index + 1]);
}

function readJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

function writeJson(path, value) {
  writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function sha256(buffer) {
  return createHash("sha256").update(buffer).digest("hex");
}

function lineCount(buffer) {
  const text = buffer.toString("utf8");
  if (!text) return 0;
  const newlines = text.match(/\n/g)?.length ?? 0;
  return newlines + (text.endsWith("\n") ? 0 : 1);
}

function walk(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? walk(path) : [path];
  });
}

function runJson(script, args = []) {
  return JSON.parse(execFileSync(process.execPath, [script, ...args], {
    cwd: process.cwd(),
    encoding: "utf8",
    maxBuffer: 128 * 1024 * 1024
  }));
}

function dependencyGroup(path) {
  const parts = path.split("/");
  if (parts[0] !== "src") return "other";
  if (parts[1] === "features" || parts[1] === "modules") return `${parts[1]}/${parts[2] ?? "root"}`;
  return parts[1] ?? "src";
}

function dependencySummary(edges) {
  const grouped = new Map();
  for (const edge of edges) {
    const importer = dependencyGroup(edge.importer);
    const target = dependencyGroup(edge.target);
    if (importer === target) continue;
    const key = `${importer}\0${target}`;
    const prior = grouped.get(key) ?? { importer, target, runtime: 0, type: 0, total: 0 };
    prior[edge.kind === "type" ? "type" : "runtime"] += 1;
    prior.total += 1;
    grouped.set(key, prior);
  }
  return [...grouped.values()].sort((left, right) =>
    left.importer.localeCompare(right.importer) || left.target.localeCompare(right.target)
  );
}

function mermaidGraph(edges) {
  const groups = [...new Set(edges.flatMap((edge) => [edge.importer, edge.target]))].sort();
  const ids = new Map(groups.map((group, index) => [group, `n${index}`]));
  const lines = ["flowchart LR"];
  for (const group of groups) lines.push(`  ${ids.get(group)}["${group}"]`);
  for (const edge of edges) {
    const label = edge.type ? `runtime ${edge.runtime} / type ${edge.type}` : `runtime ${edge.runtime}`;
    lines.push(`  ${ids.get(edge.importer)} -->|"${label}"| ${ids.get(edge.target)}`);
  }
  return `${lines.join("\n")}\n`;
}

const root = process.cwd();
const output = parseOutput(process.argv.slice(2));
mkdirSync(output, { recursive: true });

const architecture = runJson("scripts/check-frontend-architecture.mjs", ["--require-zero-debt", "--json"]);
const bundle = runJson("scripts/check-bundle-budget.mjs");
const phase0Directory = join(root, "audit-iteration/frontend-decomposition/baseline-2026-07-17T07-19-53-349Z");
const phase0Bundle = readJson(join(phase0Directory, "phase0-green/bundle-metrics.json"));
const baselinePointer = readFileSync(join(root, "audit-iteration/frontend-decomposition/current-baseline.txt"), "utf8").trim();
const visualDirectory = join(baselinePointer, "visual-regression");
const visualProvenance = readJson(join(visualDirectory, "provenance.json"));
const visualVerification = readJson(join(visualDirectory, "current-verification.json"));

const sourceFiles = walk(join(root, "src")).sort();
const sourceHashes = sourceFiles.map((path) => {
  const buffer = readFileSync(path);
  return {
    path: relative(root, path),
    bytes: buffer.length,
    lines: /\.(?:css|ts|tsx)$/.test(path) ? lineCount(buffer) : undefined,
    sha256: sha256(buffer)
  };
});
const app = sourceHashes.find((item) => item.path === "src/App.tsx");
const rootCss = sourceHashes.find((item) => item.path === "src/styles.css");
const cssFiles = sourceHashes.filter((item) => item.path.endsWith(".css") && item.path !== "src/styles.css");
const largestCss = [...cssFiles].sort((left, right) => right.lines - left.lines)[0];
const groupedDependencies = dependencySummary(architecture.edges);
const routeEntries = Object.entries(bundle.routes);
const largestRoute = routeEntries.sort(([, left], [, right]) => right.rawBytes - left.rawBytes)[0];
const viteManifestPath = join(root, "dist/.vite/manifest.json");
const cssManifestPath = join(root, "src/styles/manifest.json");

const summary = {
  generatedAt: new Date().toISOString(),
  status: architecture.ok && bundle.status === "ok" && visualVerification.status === "ok" ? "ok" : "failed",
  source: {
    app: {
      baselineLines: phase0Bundle.sources.find((item) => item.path === "src/App.tsx").lines,
      finalLines: app.lines,
      baselineBytes: phase0Bundle.sources.find((item) => item.path === "src/App.tsx").bytes,
      finalBytes: app.bytes,
      useStateCalls: architecture.metrics.app.useStateCalls,
      directApiImports: architecture.metrics.app.directApiImports.length
    },
    rootCss: {
      baselineLines: phase0Bundle.sources.find((item) => item.path === "src/styles.css").lines,
      finalLines: rootCss.lines,
      baselineBytes: phase0Bundle.sources.find((item) => item.path === "src/styles.css").bytes,
      finalBytes: rootCss.bytes
    },
    cssFragments: cssFiles.length,
    largestCss,
    architectureFiles: architecture.metrics.files,
    dependencyEdges: architecture.edges.length,
    cycles: architecture.cycles.length,
    architectureErrors: architecture.errors.length
  },
  bundle: {
    brotliQuality: 11,
    budgetPolicy: bundle.budgetPolicy,
    js: bundle.totals.js,
    all: bundle.totals.all,
    initialClosure: {
      rawBytes: bundle.initialClosure.rawBytes,
      brotliBytes: bundle.initialClosure.brotliBytes
    },
    limits: bundle.limits,
    margins: {
      jsRawBytes: bundle.limits.totalJsRawBytes - bundle.totals.js.rawBytes,
      jsBrotliBytes: bundle.limits.totalJsBrotliBytes - bundle.totals.js.brotliBytes,
      allRawBytes: bundle.limits.totalAssetsRawBytes - bundle.totals.all.rawBytes,
      allBrotliBytes: bundle.limits.totalAssetsBrotliBytes - bundle.totals.all.brotliBytes,
      initialBrotliBytes: bundle.limits.initialClosureBrotliBytes - bundle.initialClosure.brotliBytes
    },
    largestRoute: {
      name: largestRoute[0],
      rawBytes: largestRoute[1].rawBytes,
      brotliBytes: largestRoute[1].brotliBytes
    },
    routes: Object.fromEntries(routeEntries.map(([name, route]) => [name, {
      rawBytes: route.rawBytes,
      brotliBytes: route.brotliBytes
    }]))
  },
  visual: {
    baselinePointer,
    acceptedConcurrentChanges: visualProvenance.acceptedFiles,
    screenshots: visualVerification.expectedCount,
    failed: visualVerification.failed.length,
    missing: visualVerification.missing.length,
    extra: visualVerification.extra.length,
    maxGeometryDeltaPx: visualVerification.geometry.maxDelta,
    geometryViolations: visualVerification.geometry.violations.length
  },
  hashes: {
    app: app.sha256,
    rootCss: rootCss.sha256,
    viteManifest: sha256(readFileSync(viteManifestPath)),
    cssManifest: sha256(readFileSync(cssManifestPath))
  }
};

writeJson(join(output, "architecture.json"), architecture);
writeJson(join(output, "bundle.json"), bundle);
writeJson(join(output, "phase0-bundle.json"), phase0Bundle);
writeJson(join(output, "source-hashes.json"), sourceHashes);
writeJson(join(output, "dependency-summary.json"), groupedDependencies);
writeFileSync(join(output, "dependency-graph.mmd"), mermaidGraph(groupedDependencies), "utf8");
writeJson(join(output, "summary.json"), summary);
copyFileSync(viteManifestPath, join(output, "vite-manifest.json"));
copyFileSync(cssManifestPath, join(output, "css-manifest.json"));
copyFileSync(join(visualDirectory, "provenance.json"), join(output, "visual-provenance.json"));
copyFileSync(join(visualDirectory, "current-verification.json"), join(output, "visual-current-verification.json"));
if (existsSync(join(visualDirectory, "comparison-vs-phase0.json"))) {
  copyFileSync(join(visualDirectory, "comparison-vs-phase0.json"), join(output, "visual-comparison-vs-phase0.json"));
}

process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
