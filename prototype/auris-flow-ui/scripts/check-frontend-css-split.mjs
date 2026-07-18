#!/usr/bin/env node
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import process from "node:process";
import postcss from "postcss";

const rootDir = process.cwd();
const stylesPath = resolve(rootDir, "src/styles.css");
const stylesDir = resolve(rootDir, "src/styles");
const manifest = JSON.parse(readFileSync(resolve(stylesDir, "manifest.json"), "utf8"));
const styles = readFileSync(stylesPath, "utf8");
const imports = [...styles.matchAll(/@import\s+["']\.\/styles\/([^"']+)["'];/g)].map((match) => match[1]);
const expectedOrder = manifest.fragments.map((fragment) => fragment.file);

assert(manifest.schemaVersion === 2, "CSS manifest 必须是最终 schemaVersion 2");
assert(manifest.cascade === "strict-eager-source-order", "CSS 必须保持严格 eager cascade");
assert(!existsSync(resolve(stylesDir, "transitional")), "CSS 过渡分片必须清零");
assert(!JSON.stringify(manifest).includes("transitional"), "最终 manifest 不得保留过渡字段或路径");
assert(styles.split(/\r?\n/).length - 1 <= 100, "styles.css 必须保持在 100 行以内");
assert(JSON.stringify(imports) === JSON.stringify(expectedOrder), "styles.css import 顺序与 manifest 不一致");
assert(!/\{[^}]*\}/s.test(styles), "styles.css 只能保留全局有序 import 清单");

const actualCssFiles = walkFiles(stylesDir)
  .filter((file) => file.endsWith(".css"))
  .map((file) => relative(stylesDir, file).replaceAll("\\", "/"))
  .sort();
assert(JSON.stringify(actualCssFiles) === JSON.stringify([...expectedOrder].sort()), "最终 CSS 文件集合与 manifest 不一致");

const parts = manifest.fragments.map((fragment) => {
  const bytes = readFileSync(resolve(stylesDir, fragment.file));
  assert(fragment.eager === true, `${fragment.file} 必须登记为 eager`);
  assert(fragment.lines <= 4_000, `${fragment.file} 超过 4,000 行`);
  assert(bytes.length === fragment.bytes, `${fragment.file} 字节数不一致`);
  assert(sha256(bytes) === fragment.sha256, `${fragment.file} hash 不一致`);
  assert(fragment.endLine - fragment.startLine + 1 === fragment.lines, `${fragment.file} 全局行范围不一致`);
  return bytes;
});
const reconstructed = Buffer.concat(parts);
assert(reconstructed.length === manifest.sourceBytes, "CSS 重建字节数不一致");
assert(sha256(reconstructed) === manifest.sourceSha256, "CSS 重建 hash 与冻结基线不一致");
const root = postcss.parse(reconstructed.toString("utf8"));
assert(root.nodes.length === manifest.topLevelNodes, "CSS 顶层 AST 节点数不一致");
assert(orderedNodeTextHash(root) === manifest.orderedNodeTextSha256, "CSS 顶层节点顺序已变化");
assert(orderedAstDescriptorHash(root) === manifest.orderedAstDescriptorSha256, "CSS AST 描述顺序已变化");
const duplicates = duplicateSelectorRegistry(root);
assert(duplicates.groups === manifest.duplicateSelectorGroups, "出现未登记的重复选择器组");
assert(duplicates.signature === manifest.duplicateSelectorSignature, "重复选择器登记已变化");

if (process.argv.includes("--dist")) {
  const assetsDir = resolve(rootDir, "dist/assets");
  const cssAssets = readdirSync(assetsDir).filter((file) => file.endsWith(".css"));
  assert(cssAssets.length === 1, `生产构建应只有一个 eager CSS 产物，实际 ${cssAssets.length}`);
  const cssBytes = statSync(resolve(assetsDir, cssAssets[0])).size;
  console.log(`frontend CSS dist ok: ${cssAssets[0]} (${cssBytes} bytes)`);
} else {
  console.log(`frontend CSS final order ok: ${parts.length} fragments, ${root.nodes.length} AST nodes`);
}

function walkFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? walkFiles(path) : [path];
  });
}
function duplicateSelectorRegistry(ast) {
  const counts = new Map();
  ast.walkRules((rule) => counts.set(rule.selector, (counts.get(rule.selector) ?? 0) + 1));
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
