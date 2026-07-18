import React from "react";
import ReactDOM from "react-dom/client";
import { preloadJsonCatalog } from "./modules/catalogLoader";
import "./styles.css";

const rootElement = document.getElementById("root");

if (!rootElement) {
  throw new Error("应用根节点不存在");
}

const root = ReactDOM.createRoot(rootElement);

// App currently consumes both catalogs during module evaluation. Start the immutable
// asset requests in parallel with the lazy App chunk so the browser can coalesce the
// later fetches instead of creating a serial App -> catalog waterfall.
for (const [catalogUrl, label] of [
  [new URL("./catalogs/production/module-catalog.json", import.meta.url), "模块 catalog"],
  [new URL("./catalogs/production/static-catalog.json", import.meta.url), "静态 catalog"]
] as const) {
  preloadJsonCatalog(catalogUrl, label);
}

void import("./App")
  .then(({ default: App }) => {
    root.render(
      <React.StrictMode>
        <App />
      </React.StrictMode>
    );
  })
  .catch((error: unknown) => {
    const message = error instanceof Error ? error.message : "模块资源加载失败";
    root.render(
      <main className="boot-error" role="alert">
        <div className="boot-error__panel">
          <strong>工作区加载失败</strong>
          <p>{message}</p>
          <button type="button" onClick={() => window.location.reload()}>
            重新加载
          </button>
        </div>
      </main>
    );
  });
