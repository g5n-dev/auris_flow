import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { basename, join, resolve } from "node:path";

const root = resolve(new URL("..", import.meta.url).pathname);
const catalogRoot = join(root, "src/catalogs");
const productionRoot = join(catalogRoot, "production");
mkdirSync(productionRoot, { recursive: true });

const results = [];
for (const name of ["module-catalog.json", "static-catalog.json"]) {
  const sourcePath = join(catalogRoot, name);
  const source = readFileSync(sourcePath, "utf8");
  const parsed = JSON.parse(source);
  const compact = `${JSON.stringify(parsed)}\n`;
  if (JSON.stringify(JSON.parse(compact)) !== JSON.stringify(parsed)) {
    throw new Error(`${name} 生产派生资产语义不等价`);
  }
  const outputPath = join(productionRoot, basename(name));
  writeFileSync(outputPath, compact);
  results.push({
    name,
    sourceBytes: Buffer.byteLength(source),
    productionBytes: Buffer.byteLength(compact),
    sourceSha256: createHash("sha256").update(source).digest("hex"),
    semanticSha256: createHash("sha256").update(JSON.stringify(parsed)).digest("hex")
  });
}

process.stdout.write(`${JSON.stringify({ status: "ok", catalogs: results }, null, 2)}\n`);
