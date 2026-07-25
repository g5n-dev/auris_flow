import { useCallback, useEffect, useMemo, useState } from "react";
import {
  clearApiAuthContext,
  setApiContext
} from "../api/client";
import { transitionBrowserAuthSession } from "../api/authClient";
import { getWorkspaceContextOptions } from "../api/workspaceClient";
import type { AuthSession, AuthUser } from "../shared/contracts/auth";
import type {
  TopbarContextOption,
  TopbarCoreContextKey
} from "../shared/contracts/application";
import type {
  TopbarContextState,
  WorkspaceContextOptions
} from "../shared/contracts/workspace";
import {
  defaultTopbarContext,
  topbarContextToApiContext
} from "../shared/runtime/workspaceApiContext";

type ContextLoadState = "idle" | "loading" | "ready" | "error" | "switching";
type ContextOptionsByKey = Record<TopbarCoreContextKey, TopbarContextOption[]>;

const emptyOptions = (): ContextOptionsByKey => ({
  tenant: [],
  project: [],
  store: [],
  date: [],
  model: [],
  label: []
});

function authoritativeOptions(
  user: AuthUser,
  workspace: WorkspaceContextOptions | null
): ContextOptionsByKey {
  return {
    tenant: [
      {
        id: user.tenantId,
        value: user.tenant,
        meta: "身份会话锁定的租户边界"
      }
    ],
    project: user.projectMemberships.map((membership) => ({
      id: membership.project_id,
      value: membership.project_name,
      meta: membership.roles.join(" / ")
    })),
    store: (workspace?.stores ?? []).map((store) => ({
      id: store.store_id,
      value: store.name,
      meta: `${store.store_id} · ${store.status}`
    })),
    date: (workspace?.business_dates ?? []).map((date) => ({
      id: date,
      value: date,
      meta: "当前项目业务日期"
    })),
    model: (workspace?.model_versions ?? []).map((version) => ({
      id: version.id,
      value: version.label,
      meta: `${version.status} · BFF`
    })),
    label: (workspace?.label_versions ?? []).map((version) => ({
      id: version.id,
      value: version.label,
      meta: `${version.status} · BFF`
    }))
  };
}

function contextFromWorkspace(
  current: TopbarContextState,
  user: AuthUser,
  workspace: WorkspaceContextOptions
): TopbarContextState {
  return {
    ...current,
    tenant: workspace.scope.tenant_name,
    project: workspace.scope.project_name,
    store: workspace.defaults.store_id ?? "",
    date: workspace.defaults.business_date ?? "",
    model: workspace.defaults.model_version ?? "",
    label: workspace.defaults.label_version ?? ""
  };
}

export function useWorkspaceContext(
  currentUser: AuthUser | null,
  acceptSession: (session: AuthSession) => void
) {
  const [topbarContext, setTopbarContext] =
    useState<TopbarContextState>(defaultTopbarContext);
  const [workspaceOptions, setWorkspaceOptions] =
    useState<WorkspaceContextOptions | null>(null);
  const [contextState, setContextState] = useState<ContextLoadState>("idle");
  const [contextError, setContextError] = useState("");

  const projectIdByName = useMemo(
    () =>
      Object.fromEntries(
        (currentUser?.projectMemberships ?? []).map((membership) => [
          membership.project_name,
          membership.project_id
        ])
      ),
    [currentUser?.projectMemberships]
  );
  const contextOptions = useMemo(
    () => currentUser ? authoritativeOptions(currentUser, workspaceOptions) : emptyOptions(),
    [currentUser, workspaceOptions]
  );

  useEffect(() => {
    if (!currentUser) {
      setWorkspaceOptions(null);
      setContextState("idle");
      clearApiAuthContext();
      return;
    }
    const sessionContext: TopbarContextState = {
      ...topbarContext,
      tenant: currentUser.tenant,
      project: currentUser.project,
      store: "",
      date: "",
      model: "",
      label: ""
    };
    setTopbarContext(sessionContext);
    setApiContext(
      topbarContextToApiContext(sessionContext, currentUser, projectIdByName)
    );

    const controller = new AbortController();
    setWorkspaceOptions(null);
    setContextState("loading");
    setContextError("");
    void getWorkspaceContextOptions(
      {
        tenantId: currentUser.tenantId,
        projectId: currentUser.projectId
      },
      controller.signal
    )
      .then((response) => {
        if (
          response.data.scope.tenant_id !== currentUser.tenantId
          || response.data.scope.project_id !== currentUser.projectId
        ) {
          throw new Error("WORKSPACE_CONTEXT_SCOPE_DRIFT");
        }
        const nextContext = contextFromWorkspace(
          sessionContext,
          currentUser,
          response.data
        );
        setWorkspaceOptions(response.data);
        setTopbarContext(nextContext);
        setApiContext(
          topbarContextToApiContext(nextContext, currentUser, projectIdByName)
        );
        setContextState("ready");
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setWorkspaceOptions(null);
        setContextState("error");
        setContextError(
          error instanceof Error
            ? error.message
            : "工作区权威上下文读取失败"
        );
      });
    return () => controller.abort();
    // Scope identity, not local filters, owns this request lifecycle.
  }, [
    acceptSession,
    currentUser?.projectId,
    currentUser?.tenantId,
    currentUser?.userId,
    projectIdByName
  ]);

  const switchProject = useCallback(async (projectId: string) => {
    if (!currentUser || contextState === "switching") return;
    const membership = currentUser.projectMemberships.find(
      (item) => item.project_id === projectId
    );
    if (!membership) {
      setContextError("目标项目不在当前有效成员关系中。");
      throw new Error("目标项目不在当前有效成员关系中。");
    }
    if (projectId === currentUser.projectId) return;
    setContextState("switching");
    setContextError("");
    try {
      const session = await transitionBrowserAuthSession(projectId);
      acceptSession(session);
      if (typeof BroadcastChannel !== "undefined") {
        const channel = new BroadcastChannel("auris-flow.session-scope.v1");
        channel.postMessage({ type: "scope-changed", changedAt: Date.now() });
        channel.close();
      }
    } catch (error) {
      setContextState("error");
      setContextError(
        error instanceof Error ? error.message : "项目作用域切换失败"
      );
      throw error;
    }
  }, [acceptSession, contextState, currentUser]);

  const selectContextValue = useCallback(
    async (key: TopbarCoreContextKey, option: TopbarContextOption) => {
      if (key === "tenant") return;
      if (key === "project") {
        await switchProject(option.id);
        return;
      }
      setTopbarContext((current) => ({ ...current, [key]: option.id }));
      setApiContext({ [key]: option.id });
    },
    [switchProject]
  );

  const activateProjectContext = useCallback(
    (_projectName: string, projectId: string) => switchProject(projectId),
    [switchProject]
  );

  return {
    activateProjectContext,
    contextError,
    contextOptions,
    contextState,
    projectIdByName,
    selectContextValue,
    topbarContext,
    workspaceOptions
  };
}
