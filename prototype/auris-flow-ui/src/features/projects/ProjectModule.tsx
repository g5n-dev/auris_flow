import { ProjectDialogs } from "./components/ProjectDialogs";
import { ProjectTabViews } from "./components/ProjectTabViews";
import type { ProjectModuleProps } from "./types";
import { useProjectWorkspace } from "./useProjectWorkspace";

export function ProjectModule(props: ProjectModuleProps) {
  const workspace = useProjectWorkspace(props);
  const { projectNotice } = workspace;
  return (
    <>
      <div className={`operation-toast project-operation-toast is-${projectNotice.status}`} role="status" aria-live="polite">
        <strong>{projectNotice.title}</strong>
        <span>{projectNotice.detail}</span>
      </div>
      <div className="module-grid project-grid">
        <ProjectTabViews workspace={workspace} />
      </div>
      <ProjectDialogs workspace={workspace} />
    </>
  );
}
