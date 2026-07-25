import type { AuthUser } from "../contracts/auth";
import type { TopbarContextState } from "../contracts/workspace";
import { LABEL_DEMO_MODE } from "./demoMode";
import { backendProjectIdByName, backendTenantIdByName } from "./backendEntityIds";
import { defaultCurrentUser } from "../fixtures/currentUser";

export const defaultTopbarContext: TopbarContextState = LABEL_DEMO_MODE
  ? {
      tenant: defaultCurrentUser.tenant,
      project: defaultCurrentUser.project,
      store: "极光中心店",
      date: "2025-05-26",
      model: "v2.3.1",
      label: "v1.8.4"
    }
  : {
      tenant: "",
      project: "",
      store: "",
      date: "",
      model: "",
      label: ""
    };

export const topbarContextToApiContext = (
  context: TopbarContextState,
  user: AuthUser | null,
  projectIdByName: Record<string, string> = backendProjectIdByName
) => ({
  tenantId: backendTenantIdByName[context.tenant] ?? user?.tenantId ?? (LABEL_DEMO_MODE ? "aurora_auto" : ""),
  projectId:
    projectIdByName[context.project] ??
    (context.project === user?.project ? user.projectId : undefined) ??
    user?.projectId ??
    (LABEL_DEMO_MODE ? "sales_qa" : ""),
  store: context.store,
  date: context.date,
  model: context.model,
  label: context.label
});
