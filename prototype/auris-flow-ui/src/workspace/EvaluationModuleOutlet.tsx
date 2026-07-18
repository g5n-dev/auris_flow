import { lazy, Suspense } from "react";

import type { EvaluationModuleProps } from "../features/evaluation";
import { FeatureLoadBoundary } from "./FeatureLoadBoundary";

const EvaluationModule = lazy(() => import("../features/evaluation"));

export function EvaluationModuleOutlet(props: EvaluationModuleProps) {
  return (
    <FeatureLoadBoundary label="评测模块" testId="evaluation-module-load-error">
      <Suspense
        fallback={(
          <section
            className="module-panel wide feature-module-loading"
            data-testid="evaluation-module-loading"
            role="status"
            style={{ minHeight: 520 }}
          >
            正在加载评测模块...
          </section>
        )}
      >
        <EvaluationModule {...props} />
      </Suspense>
    </FeatureLoadBoundary>
  );
}
