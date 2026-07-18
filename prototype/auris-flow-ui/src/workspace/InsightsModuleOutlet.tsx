import { lazy, Suspense } from "react";

import type { InsightsFeatureProps } from "../features/insights";
import { FeatureLoadBoundary } from "./FeatureLoadBoundary";

const InsightsFeature = lazy(() => import("../features/insights"));

export function InsightsModuleOutlet(props: InsightsFeatureProps) {
  return (
    <FeatureLoadBoundary label="洞察模块" testId="insights-module-load-error">
      <Suspense
        fallback={(
          <section
            className="module-panel wide feature-module-loading"
            data-testid="insights-module-loading"
            role="status"
            style={{ minHeight: 520 }}
          >
            正在加载洞察模块...
          </section>
        )}
      >
        <InsightsFeature {...props} />
      </Suspense>
    </FeatureLoadBoundary>
  );
}
