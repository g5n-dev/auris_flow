import { normalize, resolve } from "node:path";

const selections = [
  {
    root: "src/features/canvas/fixtures",
    canonical: "./data/canvas-fixtures.json",
    production: "./production/canvas-fixtures.json"
  },
  {
    root: "src/features/listening/fixtures",
    canonical: "./data/listening-fixtures.json",
    production: "./production/listening-fixtures.json"
  }
];

const normalizePath = (path) => normalize(path).replaceAll("\\", "/");

export function replaceProductionFixtureUrl(code, canonical, production) {
  const escaped = canonical.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pattern = new RegExp(`new\\s+URL\\(\\s*(["'])${escaped}\\1`, "g");
  return code.replace(pattern, (match, quote) => match.replace(`${quote}${canonical}${quote}`, `${quote}${production}${quote}`));
}

export function selectProductionFixtures(root) {
  const resolved = selections.map((selection) => ({
    ...selection,
    root: normalizePath(resolve(root, selection.root))
  }));
  return {
    name: "auris-select-production-fixtures",
    apply: "build",
    enforce: "pre",
    transform(code, id) {
      const path = normalizePath(id.split("?")[0]);
      const selection = resolved.find((candidate) => path.startsWith(`${candidate.root}/`));
      if (!selection || !code.includes(selection.canonical)) return null;
      const transformed = replaceProductionFixtureUrl(code, selection.canonical, selection.production);
      if (transformed === code) return null;
      return { code: transformed, map: null };
    }
  };
}
