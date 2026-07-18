export const productionFixtureSpecs = [
  {
    source: "src/features/canvas/fixtures/data/canvas-fixtures.json",
    output: "src/features/canvas/fixtures/production/canvas-fixtures.json",
    includeTopLevel: ["intentsMapping"],
    omit: []
  },
  {
    source: "src/features/listening/fixtures/data/listening-fixtures.json",
    output: "src/features/listening/fixtures/production/listening-fixtures.json",
    includeTopLevel: null,
    omit: [["reviewSamples", "reviewSamples"]]
  }
];

export function buildProductionFixturePayload(source, spec) {
  const selected = spec.includeTopLevel
    ? Object.fromEntries(spec.includeTopLevel.map((key) => {
        if (!(key in source)) throw new Error(`${spec.source} 缺少生产键 ${key}`);
        return [key, source[key]];
      }))
    : structuredClone(source);
  for (const path of spec.omit) {
    const parent = path.slice(0, -1).reduce((value, key) => value?.[key], selected);
    const key = path.at(-1);
    if (!parent || !(key in parent)) throw new Error(`${spec.source} 缺少待裁剪键 ${path.join(".")}`);
    delete parent[key];
  }
  return selected;
}
