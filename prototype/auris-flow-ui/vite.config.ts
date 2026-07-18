import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { inlineFeatureControllers } from "./scripts/inline-feature-controller.mjs";
import { selectProductionFixtures } from "./scripts/select-production-fixtures.mjs";
import { compactJsxRuntime } from "./scripts/compact-jsx-runtime.mjs";
import { directReactJsxRuntime } from "./scripts/direct-react-jsx-runtime.mjs";
import { precompressedPreview } from "./scripts/serve-precompressed-assets.mjs";

const sharedRuntimeModules = new Set([
  "src/api/backendRuns.ts",
  "src/modules/moduleCatalog.ts",
  "src/modules/staticCatalog.ts",
  "src/shared/runtime/backendEntityIds.ts",
  "src/shared/runtime/backendRunStatus.ts",
  "src/shared/runtime/deepLinks.ts",
  "src/shared/runtime/feedbackAttributes.ts",
  "src/shared/runtime/hotwordVersionViews.ts",
  "src/shared/runtime/jsonFixture.ts",
  "src/shared/runtime/math.ts",
  "src/shared/runtime/projectionRecords.ts",
  "src/shared/runtime/records.ts",
  "src/shared/ui/DeepLinkSourceBar.tsx",
  "src/shared/ui/FactDisplays.tsx",
  "src/shared/ui/LazyBranchBoundary.tsx",
  "src/shared/ui/PanelHeader.tsx"
]);

export default defineConfig({
  plugins: [
    precompressedPreview(),
    directReactJsxRuntime(import.meta.dirname),
    selectProductionFixtures(import.meta.dirname),
    inlineFeatureControllers(import.meta.dirname),
    react(),
    compactJsxRuntime({ compressionAware: true })
  ],
  build: {
    target: "es2022",
    manifest: true,
    chunkSizeWarningLimit: 500,
    rollupOptions: {
      output: {
        manualChunks(id) {
          const sourcePath = id.startsWith(`${import.meta.dirname}/`)
            ? id.slice(import.meta.dirname.length + 1)
            : "";
          if (sharedRuntimeModules.has(sourcePath)) return "shared-runtime";
          if (id.endsWith("/src/api/client.ts")) return "api-client";
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("react-dom")) return "vendor-react-dom";
          if (id.includes("/react/")) return "vendor-react-core";
          if (id.includes("lucide-react")) return "vendor-icons";
          return "vendor";
        }
      }
    }
  },
  server: {
    port: 5173,
    watch: {
      ignored: ["**/dist*/**", "**/e2e/artifacts/**"]
    },
    proxy: {
      "/api": {
        target: process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8000",
        changeOrigin: true
      },
      "/healthz": {
        target: process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8000",
        changeOrigin: true
      }
    }
  }
});
