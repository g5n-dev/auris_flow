import {
  createExportRun,
  createPlatformMutation,
  getBackendRun,
  getProjectSceneProfile,
  loadModuleProjection
} from "../api/client";
import type { ModuleWorkspaceGateway } from "../shared/contracts/moduleWorkspaceGateway";

export const moduleWorkspaceGateway = {
  createExportRun,
  createPlatformMutation,
  getBackendRun,
  getProjectSceneProfile,
  loadModuleProjection
} satisfies ModuleWorkspaceGateway;
