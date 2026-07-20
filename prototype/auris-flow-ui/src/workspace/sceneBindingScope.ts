import type { WorkspaceProjectSceneBinding } from "../shared/contracts/moduleWorkspaceGateway";

export type WorkspaceSceneState = "pending" | "bound" | "unbound" | "error";

export type WorkspaceSceneResolution = {
  scopeKey: string;
  binding: WorkspaceProjectSceneBinding | null;
  state: WorkspaceSceneState;
};

export function resolveWorkspaceSceneResolution({
  currentScopeKey,
  hasProject,
  resolution
}: {
  currentScopeKey: string;
  hasProject: boolean;
  resolution: WorkspaceSceneResolution;
}): Pick<WorkspaceSceneResolution, "binding" | "state"> {
  if (resolution.scopeKey === currentScopeKey) {
    return { binding: resolution.binding, state: resolution.state };
  }
  return {
    binding: null,
    state: hasProject ? "pending" : "unbound"
  };
}
