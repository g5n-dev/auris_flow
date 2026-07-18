import type { AuthSession, AuthUser } from "../shared/contracts/auth";

export const LEGACY_AUTH_SESSION_STORAGE_KEY = "auris-flow.auth-session.v1";

export const authSessionToUser = (session: AuthSession): AuthUser => ({
  userId: session.user.user_id,
  name: session.user.name,
  email: session.user.email,
  role: session.user.role,
  tenant: session.user.tenant_name,
  project: session.user.project_name,
  initials: session.user.initials,
  roles: session.user.roles,
  tenantId: session.user.tenant_id,
  projectId: session.user.project_id,
  authToken: "",
  expiresAt: session.expires_at ?? "",
  provider: session.provider ?? session.user.provider
});

/** Purge tokens persisted by pre-OIDC builds; new sessions are cookie-only. */
export const clearLegacyStoredAuthSession = () => {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(LEGACY_AUTH_SESSION_STORAGE_KEY);
  window.sessionStorage.removeItem(LEGACY_AUTH_SESSION_STORAGE_KEY);
};
