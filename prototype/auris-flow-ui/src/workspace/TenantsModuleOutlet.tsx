import { lazy, Suspense } from "react";

import type { TenantModuleProps } from "../features/tenants";
import { FeatureLoadBoundary } from "./FeatureLoadBoundary";

const TenantModule = lazy(() => import("../features/tenants"));

export function TenantsModuleOutlet(props: TenantModuleProps) {
  return (
    <FeatureLoadBoundary label="租户模块" testId="tenants-module-load-error">
      <Suspense
        fallback={(
          <section
            className="module-panel wide feature-module-loading"
            data-testid="tenants-module-loading"
            role="status"
            style={{ minHeight: 420 }}
          >
            正在加载租户模块...
          </section>
        )}
      >
        <TenantModule {...props} />
      </Suspense>
    </FeatureLoadBoundary>
  );
}
