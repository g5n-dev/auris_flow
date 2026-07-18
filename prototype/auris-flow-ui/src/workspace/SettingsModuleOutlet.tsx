import { lazy, Suspense } from "react";

import type { SettingsModuleProps } from "../features/settings";
import { FeatureLoadBoundary } from "./FeatureLoadBoundary";

const SettingsModule = lazy(() => import("../features/settings"));

export function SettingsModuleOutlet(props: SettingsModuleProps) {
  return (
    <FeatureLoadBoundary label="设置模块" testId="settings-module-load-error">
      <Suspense
        fallback={(
          <section
            className="module-panel wide feature-module-loading"
            data-testid="settings-module-loading"
            role="status"
            style={{ minHeight: 420 }}
          >
            正在加载设置模块...
          </section>
        )}
      >
        <SettingsModule {...props} />
      </Suspense>
    </FeatureLoadBoundary>
  );
}
