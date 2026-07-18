import { lazy, Suspense } from "react";

import type { ProjectModuleProps } from "../features/projects";
import { FeatureLoadBoundary } from "./FeatureLoadBoundary";

const ProjectModule = lazy(() => import("../features/projects"));

export function ProjectsModuleOutlet(props: ProjectModuleProps) {
  return (
    <FeatureLoadBoundary label="项目模块" testId="projects-module-load-error">
      <Suspense
        fallback={(
          <section
            className="module-panel wide feature-module-loading"
            data-testid="projects-module-loading"
            role="status"
            style={{ minHeight: 420 }}
          >
            正在加载项目模块...
          </section>
        )}
      >
        <ProjectModule {...props} />
      </Suspense>
    </FeatureLoadBoundary>
  );
}
