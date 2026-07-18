#!/usr/bin/env node
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import process from "node:process";
import postcss from "postcss";

const rootDir = process.cwd();
const stylesPath = resolve(rootDir, "src/styles.css");
const stylesDir = resolve(rootDir, "src/styles");
const transitionalDir = resolve(stylesDir, "transitional");
const finalManifestPath = resolve(stylesDir, "manifest.json");
const expected = {
  bytes: 1_029_236,
  lines: 50_904,
  sha256: "452b15214e151cdc2ccedb5b2d65782dcba1fe5de5cec67cf36e1d8aa5e07220",
  nodes: 6_697,
  nodeTextSha256: "dd04f3ad7cfadc5c8fe26ab9715325cceba929f8ced66404e8d92899a3228e77",
  astDescriptorSha256: "ca63636f54432f8fb141da7c30ecc5ad0fabd8ba7e0656c519d3406904db9c30"
};

const fragments = [
  ["01-legacy.css", "foundation/reset.css", 1, 25],
  ["01-legacy.css", "shell/app-shell.css", 26, 81],
  ["01-legacy.css", "shell/navigation.css", 82, 501],
  ["01-legacy.css", "compat/workspace-evidence.css", 502, 3000],
  ["01-legacy.css", "features/listening/modes-and-config.css", 3001, 5111],
  ["02-annotation.css", "features/listening/annotation-structure.css", 1, 3002],
  ["02-annotation.css", "features/listening/annotation-review.css", 3003, 6016],
  ["03-module-baseline.css", "compat/platform-modules.css", 1, 2173],
  ["03-module-baseline.css", "compat/shared-insight-baseline.css", 2174, 4510],
  ["03-module-baseline.css", "features/evaluation/baseline.css", 4511, 6000],
  ["03-module-baseline.css", "features/labels/baseline.css", 6001, 8122],
  ["03-module-baseline.css", "features/assets/baseline.css", 8123, 10009],
  ["03-module-baseline.css", "features/settings/baseline.css", 10010, 10727],
  ["03-module-baseline.css", "features/data/audio-services.css", 10728, 11138],
  ["03-module-baseline.css", "features/canvas/baseline.css", 11139, 14628],
  ["03-module-baseline.css", "features/voiceprint/baseline.css", 14629, 16088],
  ["03-module-baseline.css", "features/data/hierarchy.css", 16089, 18413],
  ["03-module-baseline.css", "compat/responsive-overrides.css", 18414, 18557],
  ["04-arco-skin.css", "primitives/arco-console.css", 1, 1934],
  ["05-listening-polish.css", "features/listening/polish.css", 1, 2475],
  ["06-theme-compat.css", "compat/theme-evidence.css", 1, 1929],
  ["07-command-center.css", "features/listening/command-center.css", 1, 663],
  ["08-light-lock.css", "compat/light-theme-lock.css", 1, 319],
  ["09-feature-extensions.css", "features/listening/module-controls.css", 1, 123],
  ["09-feature-extensions.css", "features/tenants/asr-access.css", 124, 350],
  ["09-feature-extensions.css", "features/listening/evidence-readability.css", 351, 656],
  ["09-feature-extensions.css", "shell/auth.css", 657, 999],
  ["09-feature-extensions.css", "shell/account-center.css", 1000, 1739],
  ["09-feature-extensions.css", "features/labels/stability.css", 1740, 1914],
  ["09-feature-extensions.css", "features/labels/production.css", 1915, 4089],
  ["09-feature-extensions.css", "features/voiceprint/extensions.css", 4090, 4832],
  ["09-feature-extensions.css", "features/listening/matrix.css", 4833, 4899],
  ["09-feature-extensions.css", "features/listening/simple-chat.css", 4900, 5346],
  ["09-feature-extensions.css", "features/knowledge/extensions.css", 5347, 7882],
  ["09-feature-extensions.css", "features/insights/extensions.css", 7883, 9879],
  ["09-feature-extensions.css", "features/labels/v2.css", 9880, 10791],
  ["10-governance.css", "governance/sticky-titles.css", 1, 141],
  ["10-governance.css", "governance/stable-tabs.css", 142, 232],
  ["10-governance.css", "primitives/segmented-controls.css", 233, 338],
  ["10-governance.css", "features/home/run-dashboard.css", 339, 1451],
  ["10-governance.css", "entry/catalog-fallback.css", 1452, 1489],
  ["10-governance.css", "features/evaluation/calibration.css", 1490, 2311],
  ["10-governance.css", "governance/asr-hotword-and-insight.css", 2312, 3109]
];

if (!existsSync(transitionalDir)) {
  const manifest = JSON.parse(readFileSync(finalManifestPath, "utf8"));
  assert(manifest.schemaVersion === 2, "缺少可复用的最终 CSS manifest");
  if (process.argv.includes("--refresh-trimmed-eof")) {
    refreshManifestAfterTerminalBlankLineCleanup(manifest);
    process.exit(0);
  }
  console.log(`frontend CSS already final: ${manifest.fragments.length} fragments`);
  process.exit(0);
}

const transitionalManifest = JSON.parse(readFileSync(resolve(transitionalDir, "manifest.json"), "utf8"));
const sources = new Map(transitionalManifest.fragments.map(({ file, bytes, sha256: hash }) => {
  const source = readFileSync(resolve(transitionalDir, file));
  assert(source.length === bytes && sha256(source) === hash, `${file} 与过渡清单不一致`);
  return [file, source];
}));
const reconstructed = Buffer.concat(transitionalManifest.fragments.map(({ file }) => sources.get(file)));
validateReconstructed(reconstructed);

let globalLine = 1;
const finalFragments = fragments.map(([sourceFile, file, startLine, endLine]) => {
  const source = sources.get(sourceFile);
  assert(source, `缺少过渡源 ${sourceFile}`);
  const lineStarts = byteLineStarts(source);
  const bytes = source.subarray(lineStarts[startLine - 1], lineStarts[endLine] ?? source.length);
  const ast = postcss.parse(bytes.toString("utf8"), { from: sourceFile });
  assert(ast.nodes.length > 0, `${file} 不是完整 CSS AST 分片`);
  const lines = endLine - startLine + 1;
  assert(lines <= 4_000, `${file} 超过 4,000 行`);
  const output = resolve(stylesDir, file);
  mkdirSync(dirname(output), { recursive: true });
  writeFileSync(output, bytes);
  const entry = {
    file,
    owner: file.split("/")[0],
    eager: true,
    sourceFile,
    sourceStartLine: startLine,
    sourceEndLine: endLine,
    startLine: globalLine,
    endLine: globalLine + lines - 1,
    lines,
    bytes: bytes.length,
    sha256: sha256(bytes)
  };
  globalLine += lines;
  return entry;
});

const finalBytes = Buffer.concat(finalFragments.map(({ file }) => readFileSync(resolve(stylesDir, file))));
validateReconstructed(finalBytes);
const root = postcss.parse(finalBytes.toString("utf8"));
const duplicates = duplicateSelectorRegistry(root);
const manifest = {
  schemaVersion: 2,
  source: "src/styles.css",
  cascade: "strict-eager-source-order",
  sourceSha256: expected.sha256,
  sourceBytes: expected.bytes,
  sourceLines: expected.lines,
  topLevelNodes: expected.nodes,
  orderedNodeTextSha256: expected.nodeTextSha256,
  orderedAstDescriptorSha256: expected.astDescriptorSha256,
  duplicateSelectorGroups: duplicates.groups,
  duplicateSelectorSignature: duplicates.signature,
  fragments: finalFragments
};
writeFileSync(finalManifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
writeFileSync(stylesPath, [
  "/* Strict eager cascade manifest. Ownership is recorded in ./styles/manifest.json. */",
  ...finalFragments.map(({ file }) => `@import "./styles/${file}";`),
  ""
].join("\n"));
rmSync(transitionalDir, { recursive: true, force: true });
console.log(`frontend CSS finalized: ${finalFragments.length} fragments, ${expected.nodes} AST nodes`);

function validateReconstructed(source) {
  assert(source.length === expected.bytes, `CSS 重建字节数异常：${source.length}`);
  assert(sha256(source) === expected.sha256, "CSS 重建 hash 已变化");
  const root = postcss.parse(source.toString("utf8"));
  assert(root.nodes.length === expected.nodes, `CSS 顶层节点应为 ${expected.nodes}`);
  assert(orderedNodeTextHash(root) === expected.nodeTextSha256, "CSS 顶层节点文本顺序已变化");
  assert(orderedAstDescriptorHash(root) === expected.astDescriptorSha256, "CSS AST 描述顺序已变化");
}
function refreshManifestAfterTerminalBlankLineCleanup(manifest) {
  let changed = 0;
  let globalLine = 1;
  const refreshedFragments = manifest.fragments.map((fragment) => {
    const bytes = readFileSync(resolve(stylesDir, fragment.file));
    const unchanged = bytes.length === fragment.bytes && sha256(bytes) === fragment.sha256;
    if (!unchanged) {
      const previousBytes = Buffer.concat([bytes, Buffer.from("\n")]);
      assert(
        previousBytes.length === fragment.bytes && sha256(previousBytes) === fragment.sha256,
        `${fragment.file} 的变化不只是移除一个末尾空行，拒绝刷新 manifest`,
      );
      changed += 1;
    }
    const lines = newlineCount(bytes);
    const refreshed = {
      ...fragment,
      startLine: globalLine,
      endLine: globalLine + lines - 1,
      lines,
      bytes: bytes.length,
      sha256: sha256(bytes),
    };
    globalLine += lines;
    return refreshed;
  });
  assert(changed > 0, "没有发现只移除末尾空行的 CSS 分片");

  const reconstructed = Buffer.concat(
    refreshedFragments.map(({ file }) => readFileSync(resolve(stylesDir, file))),
  );
  const root = postcss.parse(reconstructed.toString("utf8"));
  const duplicates = duplicateSelectorRegistry(root);
  assert(root.nodes.length === manifest.topLevelNodes, "CSS 顶层节点数变化，拒绝刷新 manifest");
  assert(
    duplicates.groups === manifest.duplicateSelectorGroups &&
      duplicates.signature === manifest.duplicateSelectorSignature,
    "CSS 重复选择器语义变化，拒绝刷新 manifest",
  );

  const refreshedManifest = {
    ...manifest,
    sourceSha256: sha256(reconstructed),
    sourceBytes: reconstructed.length,
    sourceLines: newlineCount(reconstructed),
    orderedNodeTextSha256: orderedNodeTextHash(root),
    orderedAstDescriptorSha256: orderedAstDescriptorHash(root),
    fragments: refreshedFragments,
  };
  writeFileSync(finalManifestPath, `${JSON.stringify(refreshedManifest, null, 2)}\n`);
  console.log(`frontend CSS manifest refreshed: ${changed} terminal blank lines removed`);
}
function newlineCount(source) {
  let lines = 0;
  for (const byte of source) if (byte === 0x0a) lines += 1;
  return lines;
}
function byteLineStarts(source) {
  const starts = [0];
  for (let index = 0; index < source.length; index += 1) if (source[index] === 0x0a) starts.push(index + 1);
  return starts;
}
function duplicateSelectorRegistry(root) {
  const counts = new Map();
  root.walkRules((rule) => counts.set(rule.selector, (counts.get(rule.selector) ?? 0) + 1));
  const repeated = [...counts].filter(([, count]) => count > 1).sort(([left], [right]) => left.localeCompare(right));
  return { groups: repeated.length, signature: sha256(Buffer.from(JSON.stringify(repeated))) };
}
function orderedNodeTextHash(ast) { return sha256(Buffer.from(ast.nodes.map((node) => node.toString()).join("\0"))); }
function orderedAstDescriptorHash(ast) {
  const descriptor = ast.nodes.map((node) => [node.type, "name" in node ? node.name : "", "selector" in node ? node.selector : "", "params" in node ? node.params : "", node.source?.start?.line ?? 0, node.source?.end?.line ?? 0].join("|")).join("\n");
  return sha256(Buffer.from(descriptor));
}
function sha256(value) { return createHash("sha256").update(value).digest("hex"); }
function assert(condition, message) { if (!condition) throw new Error(message); }
