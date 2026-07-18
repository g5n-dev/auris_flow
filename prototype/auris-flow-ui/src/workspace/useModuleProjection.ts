import { useEffect, useRef, useState } from "react";
import type { AuthUser } from "../shared/contracts/auth";
import type {
  ModuleWorkspaceGateway,
  WorkspaceApiContext,
  WorkspaceModuleProjectionReceipt,
  WorkspaceProjectSceneBinding
} from "../shared/contracts/moduleWorkspaceGateway";
import type { ModuleKey } from "../shared/contracts/navigation";
import type { ProjectionDisplayState } from "../shared/contracts/modules";
import type { TopbarContextState } from "../shared/contracts/workspace";
import { topbarContextToApiContext } from "../shared/runtime/workspaceApiContext";

type ModuleProjectionInput = {
  gateway: Pick<ModuleWorkspaceGateway, "getProjectSceneProfile" | "loadModuleProjection">;
  moduleKey: Exclude<ModuleKey, "listening">;
  currentUser: AuthUser;
  topbarContext: TopbarContextState;
  projectIdByName: Record<string, string>;
};

export function useModuleProjection({
  gateway,
  moduleKey,
  currentUser,
  topbarContext,
  projectIdByName
}: ModuleProjectionInput) {
  const [projectionRequestStatus, setProjectionStatus] = useState<ProjectionDisplayState>("pending");
  const [projectionStateKey, setProjectionStateKey] = useState("");
  const [projectionReceipt, setProjectionReceipt] = useState<WorkspaceModuleProjectionReceipt | null>(null);
  const [projectionError, setProjectionError] = useState("");
  const [projectionRefreshNonce, setProjectionRefreshNonce] = useState(0);
  const projectionRequestSequence = useRef(0);
  const projectionContext: WorkspaceApiContext = topbarContextToApiContext(topbarContext, currentUser, projectIdByName);
  const [workspaceSceneBinding, setWorkspaceSceneBinding] = useState<WorkspaceProjectSceneBinding | null>(null);
  const [workspaceSceneState, setWorkspaceSceneState] = useState<"pending" | "bound" | "unbound" | "error">("pending");

  useEffect(() => {
    const projectId = String(projectionContext.projectId ?? "").trim();
    const tenantId = String(projectionContext.tenantId ?? "").trim();
    setWorkspaceSceneBinding(null);
    if (!projectId) {
      setWorkspaceSceneState("unbound");
      return;
    }
    let disposed = false;
    setWorkspaceSceneState("pending");
    void gateway.getProjectSceneProfile(projectId, "production", projectionContext)
      .then((response) => {
        if (disposed) return;
        const binding = response.data;
        if (!binding) {
          setWorkspaceSceneBinding(null);
          setWorkspaceSceneState("unbound");
          return;
        }
        const bindingTenantId = String((binding as WorkspaceProjectSceneBinding & { tenant_id?: string }).tenant_id ?? "").trim();
        if (
          binding.project_id !== projectId ||
          (bindingTenantId && bindingTenantId !== tenantId) ||
          binding.environment !== "production" ||
          binding.status !== "active" ||
          binding.version.status !== "published" ||
          binding.version.scene_profile_id !== binding.scene_profile_id ||
          binding.version.scene_profile_version_id !== binding.scene_profile_version_id ||
          binding.version.manifest_sha256 !== binding.manifest_sha256
        ) {
          throw new Error("SCENE_PROFILE_BINDING_DRIFT：返回绑定与当前租户/项目或已发布快照不一致");
        }
        setWorkspaceSceneBinding(binding);
        setWorkspaceSceneState("bound");
      })
      .catch((error) => {
        if (disposed) return;
        setWorkspaceSceneBinding(null);
        const message = error instanceof Error ? error.message : "";
        setWorkspaceSceneState(message.includes("没有已激活") || message.includes("SCENE_PROFILE_BINDING") ? "unbound" : "error");
      });
    return () => {
      disposed = true;
    };
  }, [projectionContext.projectId, projectionContext.tenantId]);

  const projectionScopeKey = JSON.stringify([
    moduleKey,
    projectionContext.tenantId,
    projectionContext.projectId,
    projectionContext.store,
    projectionContext.date,
    projectionContext.model,
    projectionContext.label
  ]);
  const projectionStatus: ProjectionDisplayState = projectionStateKey === projectionScopeKey
    ? projectionRequestStatus
    : "pending";

  useEffect(() => {
    const requestSequence = projectionRequestSequence.current + 1;
    projectionRequestSequence.current = requestSequence;
    let disposed = false;
    setProjectionStateKey(projectionScopeKey);
    setProjectionStatus("pending");
    setProjectionReceipt(null);
    setProjectionError("");
    gateway.loadModuleProjection(moduleKey, {
      context: projectionContext,
      force: projectionRefreshNonce > 0
    })
      .then((receipt) => {
        if (disposed || projectionRequestSequence.current !== requestSequence) return;
        setProjectionReceipt(receipt);
        setProjectionStatus(receipt.state);
      })
      .catch((error) => {
        if (disposed || projectionRequestSequence.current !== requestSequence) return;
        setProjectionReceipt(null);
        setProjectionStatus("degraded");
        setProjectionError(error instanceof Error ? error.message : "BFF projection unavailable");
      });
    return () => {
      disposed = true;
    };
  }, [projectionRefreshNonce, projectionScopeKey]);

  return {
    projectionContext,
    projectionError,
    projectionReceipt,
    projectionStatus,
    retryProjection: () => setProjectionRefreshNonce((current) => current + 1),
    workspaceSceneBinding,
    workspaceSceneState
  };
}

export type ModuleProjectionController = ReturnType<typeof useModuleProjection>;
