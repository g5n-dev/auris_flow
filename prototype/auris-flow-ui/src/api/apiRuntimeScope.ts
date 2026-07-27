type ApiScopeContext = {
  tenantId?: string;
  projectId?: string;
};

export function buildApiScopeKey(context: ApiScopeContext) {
  return [
    encodeURIComponent(context.tenantId || "unbound-tenant"),
    encodeURIComponent(context.projectId || "unbound-project")
  ].join(":");
}

export function readApiRuntimeScope(context: ApiScopeContext) {
  return {
    tenantId: context.tenantId ?? "",
    projectId: context.projectId ?? ""
  };
}
