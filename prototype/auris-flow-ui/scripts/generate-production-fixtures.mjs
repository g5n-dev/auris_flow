import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

import { buildProductionFixturePayload, productionFixtureSpecs } from "./production-fixture-policy.mjs";

const root = resolve(new URL("..", import.meta.url).pathname);

for (const spec of productionFixtureSpecs) {
  const source = JSON.parse(readFileSync(join(root, spec.source), "utf8"));
  const output = buildProductionFixturePayload(source, spec);
  const outputPath = join(root, spec.output);
  mkdirSync(dirname(outputPath), { recursive: true });
  writeFileSync(outputPath, `${JSON.stringify(output)}\n`);
  process.stdout.write(`${spec.output}: ${Buffer.byteLength(JSON.stringify(output))} bytes\n`);
}
