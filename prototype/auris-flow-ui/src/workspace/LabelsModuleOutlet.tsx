import { lazy, Suspense } from "react";

import type { LabelsModuleProps } from "../features/labels";
import { FeatureLoadBoundary } from "./FeatureLoadBoundary";

const LabelsModule = lazy(() => import("../features/labels"));

export function LabelsModuleOutlet(props: LabelsModuleProps) {
  return (
    <FeatureLoadBoundary label="标签治理模块" testId="labels-module-load-error">
      <Suspense
        fallback={(
          <section
            className="module-panel wide feature-module-loading"
            data-testid="labels-module-loading"
            role="status"
            style={{ minHeight: 520 }}
          >
            正在加载标签治理模块...
          </section>
        )}
      >
        <LabelsModule {...props} />
      </Suspense>
    </FeatureLoadBoundary>
  );
}
