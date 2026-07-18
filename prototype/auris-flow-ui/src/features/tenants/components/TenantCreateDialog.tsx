import { GitBranch, ShieldCheck, X } from "lucide-react";

import { EntitySelect } from "../../../shared/ui/EntitySelect";
import { tenantQuotaOptions, tenantSceneOptions } from "../fixtures";
import type { TenantWorkspace } from "../useTenantWorkspace";

export function TenantCreateDialog({ workspace }: { workspace: TenantWorkspace }) {
  const {
    canCreateTenant,
    canManageTenants,
    createTenant,
    createTenantSceneDraft,
    draftTenant,
    selectTenantScene,
    setDraftTenant,
    setTenantCreateOpen,
    setTenantSceneCreateOpen,
    setTenantSceneDraft,
    tenantAction,
    tenantAdminUnavailableReason,
    tenantCreateOpen,
    tenantSceneCreateOpen,
    tenantSceneCustom,
    tenantSceneDraft,
    tenantSceneNeedsSetup,
    tenantSceneUnbound
  } = workspace;

  return (
    <>
      {undefined}
    </>
  );
}
