import { type AuthSession } from "../api/client";
import type { AuthUser } from "../shared/contracts/auth";

export const AUTH_SESSION_STORAGE_KEY = "auris-flow.auth-session.v1";

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
  authToken: session.access_token,
  expiresAt: session.expires_at
});

export const loadStoredAuthSession = (): AuthSession | null => {
  if (typeof window === "undefined") return null;
  for (const storage of [window.localStorage, window.sessionStorage]) {
    const raw = storage.getItem(AUTH_SESSION_STORAGE_KEY);
    if (!raw) continue;
    try {
      const parsed = JSON.parse(raw) as AuthSession;
      if (
        parsed.access_token &&
        parsed.expires_at &&
        parsed.user?.user_id &&
        new Date(parsed.expires_at).getTime() > Date.now()
      ) {
        return parsed;
      }
    } catch {
      storage.removeItem(AUTH_SESSION_STORAGE_KEY);
    }
  }
  return null;
};

export const persistAuthSession = (session: AuthSession, remember: boolean) => {
  const target = remember ? window.localStorage : window.sessionStorage;
  const other = remember ? window.sessionStorage : window.localStorage;
  other.removeItem(AUTH_SESSION_STORAGE_KEY);
  target.setItem(AUTH_SESSION_STORAGE_KEY, JSON.stringify(session));
};

export const clearStoredAuthSession = () => {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(AUTH_SESSION_STORAGE_KEY);
  window.sessionStorage.removeItem(AUTH_SESSION_STORAGE_KEY);
};
