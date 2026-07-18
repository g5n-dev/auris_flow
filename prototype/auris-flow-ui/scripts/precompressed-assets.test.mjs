import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { brotliCompressSync, constants as zlibConstants } from "node:zlib";

import {
  acceptsBrotli,
  appendVaryHeader,
  auditPrecompressedAssets,
  generatePrecompressedAssets
} from "./precompressed-assets.mjs";

function createDist() {
  const directory = mkdtempSync(join(tmpdir(), "auris-precompress-"));
  mkdirSync(join(directory, "assets"), { recursive: true });
  mkdirSync(join(directory, ".vite"), { recursive: true });
  writeFileSync(join(directory, "index.html"), "<!doctype html><script src='/assets/app.js'></script>\n");
  writeFileSync(join(directory, "assets/app.js"), "export const greeting = '你好，Auris Flow';\n".repeat(32));
  writeFileSync(join(directory, "assets/app.css"), ".app-shell { color: #165dff; }\n".repeat(16));
  writeFileSync(join(directory, "assets/data.json"), `${JSON.stringify({ rows: ["一", "二", "三"] })}\n`);
  return directory;
}

function withDist(run) {
  const directory = createDist();
  try {
    return run(directory);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

test("q11 预压缩连续生成保持 sidecar 与 manifest 字节确定", () => withDist((distDir) => {
  const first = generatePrecompressedAssets(distDir);
  const firstManifest = readFileSync(join(distDir, ".vite/brotli-manifest.json"));
  const firstSidecars = Object.fromEntries(
    Object.keys(first.manifest.entries).map((path) => [path, readFileSync(join(distDir, `${path}.br`))])
  );

  const second = generatePrecompressedAssets(distDir);
  assert.deepEqual(readFileSync(join(distDir, ".vite/brotli-manifest.json")), firstManifest);
  for (const [path, bytes] of Object.entries(firstSidecars)) {
    assert.deepEqual(readFileSync(join(distDir, `${path}.br`)), bytes);
  }
  assert.equal(second.audit.ok, true);
  assert.deepEqual(second.audit.errors, []);
}));

test("审计识别缺失、陈旧、孤儿、损坏与非规范 q11 sidecar", () => withDist((distDir) => {
  generatePrecompressedAssets(distDir);
  rmSync(join(distDir, "assets/app.css.br"));
  let codes = new Set(auditPrecompressedAssets(distDir).errors.map((error) => error.code));
  assert.ok(codes.has("BROTLI_SIDECAR_MISSING"));

  generatePrecompressedAssets(distDir);
  writeFileSync(join(distDir, "assets/app.js"), "export const changed = true;\n");
  codes = new Set(auditPrecompressedAssets(distDir).errors.map((error) => error.code));
  assert.ok(codes.has("SOURCE_SIZE_MISMATCH") || codes.has("SOURCE_HASH_MISMATCH"));

  generatePrecompressedAssets(distDir);
  writeFileSync(join(distDir, "assets/orphan.js.br"), "orphan");
  codes = new Set(auditPrecompressedAssets(distDir).errors.map((error) => error.code));
  assert.ok(codes.has("BROTLI_SIDECAR_ORPHAN"));

  rmSync(join(distDir, "assets/orphan.js.br"));
  writeFileSync(join(distDir, "assets/app.js.br"), "not-brotli");
  codes = new Set(auditPrecompressedAssets(distDir).errors.map((error) => error.code));
  assert.ok(codes.has("BROTLI_DECOMPRESS_FAILED"));

  generatePrecompressedAssets(distDir);
  const source = readFileSync(join(distDir, "assets/app.js"));
  const q5 = brotliCompressSync(source, {
    params: { [zlibConstants.BROTLI_PARAM_QUALITY]: 5 }
  });
  writeFileSync(join(distDir, "assets/app.js.br"), q5);
  codes = new Set(auditPrecompressedAssets(distDir).errors.map((error) => error.code));
  assert.ok(codes.has("BROTLI_NOT_CANONICAL_Q11"));
}));

test("审计独立识别 manifest 条目与 hash 被篡改", () => withDist((distDir) => {
  generatePrecompressedAssets(distDir);
  const manifestPath = join(distDir, ".vite/brotli-manifest.json");
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  delete manifest.entries["assets/data.json"];
  manifest.entries["assets/ghost.js"] = {
    sourceSha256: "0".repeat(64),
    brotliSha256: "0".repeat(64),
    rawBytes: 1,
    brotliBytes: 1
  };
  manifest.entries["assets/app.js"].brotliSha256 = "f".repeat(64);
  writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
  const codes = new Set(auditPrecompressedAssets(distDir).errors.map((error) => error.code));
  assert.ok(codes.has("PRECOMPRESS_ENTRY_MISSING"));
  assert.ok(codes.has("PRECOMPRESS_ENTRY_ORPHAN"));
  assert.ok(codes.has("BROTLI_HASH_MISMATCH"));
}));

test("Accept-Encoding 解析尊重显式 br 禁用与 wildcard，Vary 合并去重", () => {
  assert.equal(acceptsBrotli("gzip, br"), true);
  assert.equal(acceptsBrotli("BR; q=0.5"), true);
  assert.equal(acceptsBrotli("br;q=0, *;q=1"), false);
  assert.equal(acceptsBrotli("gzip, *;q=0.5"), true);
  assert.equal(acceptsBrotli("gzip, *;q=0"), false);
  assert.equal(acceptsBrotli("br;q=wat"), false);
  assert.equal(acceptsBrotli(undefined), false);
  assert.equal(appendVaryHeader(undefined, "Accept-Encoding"), "Accept-Encoding");
  assert.equal(appendVaryHeader("Origin", "Accept-Encoding"), "Origin, Accept-Encoding");
  assert.equal(appendVaryHeader("accept-encoding, Origin", "Accept-Encoding"), "accept-encoding, Origin");
});
