import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import Module, { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";

const require = createRequire(import.meta.url);
const { PNG } = require("playwright-core/lib/utilsBundle");

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (!argument.startsWith("--")) throw new Error(`未知参数：${argument}`);
    const key = argument.slice(2);
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`参数缺少值：${argument}`);
    values[key] = value;
    index += 1;
  }
  if (!values.expected || !values.actual) {
    throw new Error("用法：node scripts/compare-visual-baselines.mjs --expected <visual-regression-dir> --actual <visual-regression-dir> [--output <json>]");
  }
  return values;
}

function loadPngComparator() {
  const packageEntry = require.resolve("playwright-core");
  const coreBundlePath = resolve(dirname(packageEntry), "lib/coreBundle.js");
  const source = readFileSync(coreBundlePath, "utf8");
  const bundleModule = new Module(coreBundlePath);
  bundleModule.filename = coreBundlePath;
  bundleModule.paths = Module._nodeModulePaths(dirname(coreBundlePath));
  bundleModule._compile(`${source}\nmodule.exports.__aurisGetComparator = getComparator;\n`, coreBundlePath);
  return bundleModule.exports.__aurisGetComparator("image/png");
}

function sha256(buffer) {
  return createHash("sha256").update(buffer).digest("hex");
}

function pngFiles(directory) {
  return readdirSync(directory).filter((file) => file.endsWith(".png")).sort();
}

function diffBoundingBox(diffBuffer) {
  if (!diffBuffer) return null;
  const image = PNG.sync.read(diffBuffer);
  let minX = image.width;
  let minY = image.height;
  let maxX = -1;
  let maxY = -1;
  for (let y = 0; y < image.height; y += 1) {
    for (let x = 0; x < image.width; x += 1) {
      const offset = (y * image.width + x) * 4;
      if (image.data[offset] !== 255 || image.data[offset + 1] !== 0 || image.data[offset + 2] !== 0) continue;
      minX = Math.min(minX, x);
      minY = Math.min(minY, y);
      maxX = Math.max(maxX, x);
      maxY = Math.max(maxY, y);
    }
  }
  return maxX < 0 ? null : {
    x: minX,
    y: minY,
    width: maxX - minX + 1,
    height: maxY - minY + 1
  };
}

function percentile(values, ratio) {
  if (!values.length) return 0;
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * ratio) - 1)];
}

function compareGeometry(expectedPath, actualPath) {
  if (!existsSync(expectedPath) || !existsSync(actualPath)) {
    return {
      status: "missing",
      expectedExists: existsSync(expectedPath),
      actualExists: existsSync(actualPath),
      missingShots: [],
      extraShots: [],
      missingSelectors: [],
      extraSelectors: [],
      maxDelta: null,
      violations: []
    };
  }
  const expected = JSON.parse(readFileSync(expectedPath, "utf8"));
  const actual = JSON.parse(readFileSync(actualPath, "utf8"));
  const expectedShots = Object.keys(expected).sort();
  const actualShots = Object.keys(actual).sort();
  const missingShots = expectedShots.filter((shot) => !(shot in actual));
  const extraShots = actualShots.filter((shot) => !(shot in expected));
  const missingSelectors = [];
  const extraSelectors = [];
  const violations = [];
  let maxDelta = 0;
  for (const shot of expectedShots.filter((candidate) => candidate in actual)) {
    const expectedSelectors = Object.keys(expected[shot]).sort();
    const actualSelectors = Object.keys(actual[shot]).sort();
    for (const selector of expectedSelectors.filter((candidate) => !(candidate in actual[shot]))) {
      missingSelectors.push({ shot, selector });
    }
    for (const selector of actualSelectors.filter((candidate) => !(candidate in expected[shot]))) {
      extraSelectors.push({ shot, selector });
    }
    for (const selector of expectedSelectors.filter((candidate) => candidate in actual[shot])) {
      for (const field of ["x", "y", "width", "height"]) {
        const delta = Math.abs(actual[shot][selector][field] - expected[shot][selector][field]);
        maxDelta = Math.max(maxDelta, delta);
        if (delta > 0.5) violations.push({ shot, selector, field, delta });
      }
    }
  }
  return {
    status: !missingShots.length && !extraShots.length && !missingSelectors.length && !extraSelectors.length && !violations.length ? "ok" : "failed",
    expectedCount: expectedShots.length,
    actualCount: actualShots.length,
    missingShots,
    extraShots,
    missingSelectors,
    extraSelectors,
    maxDelta,
    violations
  };
}

function fingerprint(visualDirectory, files) {
  const hash = createHash("sha256");
  for (const file of files) {
    const buffer = readFileSync(join(visualDirectory, "screenshots", file));
    hash.update(`${file}\0${buffer.length}\0${sha256(buffer)}\n`);
  }
  const geometryPath = join(visualDirectory, "geometry.json");
  if (existsSync(geometryPath)) {
    const buffer = readFileSync(geometryPath);
    hash.update(`geometry.json\0${buffer.length}\0${sha256(buffer)}\n`);
  }
  return hash.digest("hex");
}

const args = parseArgs(process.argv.slice(2));
const expectedDirectory = resolve(args.expected);
const actualDirectory = resolve(args.actual);
const expectedScreenshots = join(expectedDirectory, "screenshots");
const actualScreenshots = join(actualDirectory, "screenshots");
const expectedFiles = pngFiles(expectedScreenshots);
const actualFiles = pngFiles(actualScreenshots);
const missing = expectedFiles.filter((file) => !actualFiles.includes(file));
const extra = actualFiles.filter((file) => !expectedFiles.includes(file));
const comparator = loadPngComparator();
const comparisons = [];

for (const file of expectedFiles.filter((candidate) => actualFiles.includes(candidate))) {
  const expectedBuffer = readFileSync(join(expectedScreenshots, file));
  const actualBuffer = readFileSync(join(actualScreenshots, file));
  const expectedImage = PNG.sync.read(expectedBuffer);
  const actualImage = PNG.sync.read(actualBuffer);
  const result = comparator(actualBuffer, expectedBuffer, { threshold: 0.2, maxDiffPixels: 0 });
  const match = result?.errorMessage?.match(/(\d+) pixels/);
  const diffPixels = match ? Number(match[1]) : 0;
  const totalPixels = expectedImage.width * expectedImage.height;
  const ratio = totalPixels ? diffPixels / totalPixels : 1;
  comparisons.push({
    file,
    module: file.match(/^(\d{2}-[a-z]+)/)?.[1] ?? "unknown",
    expectedSha256: sha256(expectedBuffer),
    actualSha256: sha256(actualBuffer),
    expectedBytes: expectedBuffer.length,
    actualBytes: actualBuffer.length,
    expectedSize: { width: expectedImage.width, height: expectedImage.height },
    actualSize: { width: actualImage.width, height: actualImage.height },
    diffPixels,
    ratio,
    diffBoundingBox: diffBoundingBox(result?.diff),
    status: expectedImage.width === actualImage.width && expectedImage.height === actualImage.height && ratio <= 0.001 ? "ok" : "failed"
  });
}

const modules = Object.fromEntries([...new Set(comparisons.map((item) => item.module))].sort().map((module) => {
  const items = comparisons.filter((item) => item.module === module);
  const ratios = items.map((item) => item.ratio);
  return [module, {
    screenshots: items.length,
    failed: items.filter((item) => item.status !== "ok").length,
    maxRatio: Math.max(...ratios),
    p50Ratio: percentile(ratios, 0.5),
    p95Ratio: percentile(ratios, 0.95)
  }];
}));
const geometry = compareGeometry(
  join(expectedDirectory, "geometry.json"),
  join(actualDirectory, "geometry.json")
);
const report = {
  status: !missing.length && !extra.length && comparisons.every((item) => item.status === "ok") && geometry.status === "ok" ? "ok" : "failed",
  threshold: { maxDiffPixelRatio: 0.001, maxGeometryDeltaPx: 0.5, pixelmatchThreshold: 0.2 },
  expectedDirectory,
  actualDirectory,
  expectedFingerprint: fingerprint(expectedDirectory, expectedFiles),
  actualFingerprint: fingerprint(actualDirectory, actualFiles),
  expectedCount: expectedFiles.length,
  actualCount: actualFiles.length,
  missing,
  extra,
  failed: comparisons.filter((item) => item.status !== "ok"),
  modules,
  geometry,
  comparisons
};
const output = `${JSON.stringify(report, null, 2)}\n`;
if (args.output) {
  const outputPath = resolve(args.output);
  mkdirSync(dirname(outputPath), { recursive: true });
  writeFileSync(outputPath, output, "utf8");
}
process.stdout.write(output);
