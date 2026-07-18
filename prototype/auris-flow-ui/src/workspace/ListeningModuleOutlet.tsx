import { lazy, Suspense, useEffect, useState } from "react";

import type { ListeningFeatureProps } from "../features/listening";
import { FeatureLoadBoundary } from "./FeatureLoadBoundary";

const ListeningFeature = lazy(() => import("../features/listening"));

export function ListeningModuleOutlet(props: ListeningFeatureProps) {
  const [activated, setActivated] = useState(props.active);

  useEffect(() => {
    if (props.active) setActivated(true);
  }, [props.active]);

  if (!activated && !props.active) return null;
  return (
    <FeatureLoadBoundary label="调听模块" testId="listening-module-load-error" visible={props.active}>
      <Suspense
        fallback={props.active ? (
          <section
            className="module-panel wide feature-module-loading"
            data-testid="listening-module-loading"
            role="status"
            style={{ minHeight: 520 }}
          >
            正在加载调听模块...
          </section>
        ) : null}
      >
        <ListeningFeature {...props} />
      </Suspense>
    </FeatureLoadBoundary>
  );
}
