import { useEffect, useState } from "react";
import { clearApiAuthContext, setApiContext } from "../api/client";
import type { AuthUser } from "../shared/contracts/auth";
import type { TopbarContextState } from "../shared/contracts/workspace";
import { backendProjectIdByName } from "../shared/runtime/backendEntityIds";
import {
  defaultTopbarContext,
  topbarContextToApiContext
} from "../shared/runtime/workspaceApiContext";

export function useWorkspaceContext(currentUser: AuthUser | null) {
  const [topbarContext, setTopbarContext] = useState<TopbarContextState>(defaultTopbarContext);
  const [projectIdByName, setProjectIdByName] = useState<Record<string, string>>(backendProjectIdByName);

  useEffect(() => {
    if (!currentUser) return;
    setTopbarContext((current) => ({
      ...current,
      tenant: currentUser.tenant,
      project: currentUser.project
    }));
  }, [currentUser?.project, currentUser?.tenant]);

  useEffect(() => {
    if (currentUser) {
      setApiContext(topbarContextToApiContext(topbarContext, currentUser, projectIdByName));
    } else {
      clearApiAuthContext();
    }
  }, [currentUser?.authToken, currentUser?.projectId, currentUser?.tenantId, projectIdByName, topbarContext]);

  const activateProjectContext = (projectName: string, projectId: string) => {
    const nextProjectMap = { ...projectIdByName, [projectName]: projectId };
    const nextContext = { ...topbarContext, project: projectName };
    setProjectIdByName(nextProjectMap);
    setTopbarContext(nextContext);
    if (currentUser) {
      setApiContext(topbarContextToApiContext(nextContext, currentUser, nextProjectMap));
    }
  };

  return {
    activateProjectContext,
    projectIdByName,
    setTopbarContext,
    topbarContext
  };
}
