import type { WorkspaceContextOptions } from "../shared/contracts/workspace";
import {
  apiRequest,
  type ApiEnvelope,
  type ApiRuntimeContext
} from "./client";

export function getWorkspaceContextOptions(
  context?: ApiRuntimeContext,
  signal?: AbortSignal
): Promise<ApiEnvelope<WorkspaceContextOptions>> {
  return apiRequest<WorkspaceContextOptions>(
    "/v1/workspace-context-options",
    { signal },
    context
  );
}
