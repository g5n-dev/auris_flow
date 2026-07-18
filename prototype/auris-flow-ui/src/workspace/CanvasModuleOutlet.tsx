import { lazy, Suspense } from "react";

import type { CanvasModuleProps } from "../features/canvas";
import { FeatureLoadBoundary } from "./FeatureLoadBoundary";

const CanvasModule = lazy(() => import("../features/canvas"));

export function CanvasModuleOutlet(props: CanvasModuleProps) {
  return (
    <FeatureLoadBoundary label="任务编排模块" testId="canvas-module-load-error">
      <Suspense
        fallback={(
          <section
            className="module-panel wide feature-module-loading"
            data-testid="canvas-module-loading"
            role="status"
            style={{ minHeight: 520 }}
          >
            正在加载任务编排模块...
          </section>
        )}
      >
        <CanvasModule {...props} />
      </Suspense>
    </FeatureLoadBoundary>
  );
}
