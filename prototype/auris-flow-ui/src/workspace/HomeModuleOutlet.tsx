import { lazy, Suspense } from "react";

import type { HomeModuleProps } from "../features/home";
import { FeatureLoadBoundary } from "./FeatureLoadBoundary";

const HomeModule = lazy(() => import("../features/home"));

export function HomeModuleOutlet(props: HomeModuleProps) {
  return (
    <FeatureLoadBoundary label="首页模块" testId="home-module-load-error">
      <Suspense
        fallback={(
          <section
            className="module-panel wide feature-module-loading"
            data-testid="home-module-loading"
            role="status"
            style={{ minHeight: 420 }}
          >
            正在加载首页模块...
          </section>
        )}
      >
        <HomeModule {...props} />
      </Suspense>
    </FeatureLoadBoundary>
  );
}
