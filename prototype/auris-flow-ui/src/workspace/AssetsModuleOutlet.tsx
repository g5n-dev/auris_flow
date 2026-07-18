import { lazy, Suspense } from "react";

import type { AssetsModuleProps } from "../features/assets";
import { FeatureLoadBoundary } from "./FeatureLoadBoundary";

const AssetsModule = lazy(() => import("../features/assets"));

export function AssetsModuleOutlet(props: AssetsModuleProps) {
  return (
    <FeatureLoadBoundary label="数据资产模块" testId="assets-module-load-error">
      <Suspense
        fallback={(
          <section
            className="module-panel wide feature-module-loading"
            data-testid="assets-module-loading"
            role="status"
            style={{ minHeight: 420 }}
          >
            正在加载数据资产模块...
          </section>
        )}
      >
        <AssetsModule {...props} />
      </Suspense>
    </FeatureLoadBoundary>
  );
}
