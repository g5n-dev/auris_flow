import type { TenantModuleProps } from "./types";
import { TenantCreateDialog } from "./components/TenantCreateDialog";
import { TenantTabViews } from "./components/TenantTabViews";
import { useTenantWorkspace } from "./useTenantWorkspace";

export function TenantModule(props: TenantModuleProps) {
  const workspace = useTenantWorkspace(props);
  const { tenantNotice } = workspace;
  return (
    <>
      <div className={`operation-toast tenant-operation-toast is-${tenantNotice.status}`} role="status" aria-live="polite">
        <strong>{tenantNotice.title}</strong>
        <span>{tenantNotice.detail}</span>
      </div>
      <div className="module-grid tenant-grid">
        <TenantTabViews workspace={workspace} />
      </div>
      <TenantCreateDialog workspace={workspace} />
    </>
  );
}
