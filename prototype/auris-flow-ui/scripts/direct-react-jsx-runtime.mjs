import { resolve } from "node:path";

const virtualRuntimeId = "\0auris-react-jsx-runtime";

export function directReactJsxRuntime(root) {
  const productionRuntime = resolve(
    root,
    "node_modules/react/cjs/react-jsx-runtime.production.min.js"
  );

  return {
    name: "auris-direct-react-jsx-runtime",
    apply: "build",
    enforce: "pre",
    resolveId(source) {
      return source === "react/jsx-runtime" ? virtualRuntimeId : null;
    },
    load(id) {
      if (id !== virtualRuntimeId) return null;
      return [
        `import runtime from ${JSON.stringify(productionRuntime)};`,
        "export const Fragment = runtime.Fragment;",
        "export const jsx = runtime.jsx;",
        "export const jsxs = runtime.jsxs;"
      ].join("\n");
    }
  };
}
