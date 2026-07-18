import { useEffect, useRef, useState } from "react";
import {
  clearApiAuthContext,
  createDevAuthSession,
  establishApiSession,
  isDefinitiveAuthFailure,
  revokeAuthSession,
  validateAuthSession
} from "../api/client";
import type { AuthSession, AuthSessionUser, AuthUser } from "../shared/contracts/auth";
import {
  authSessionToUser,
  clearStoredAuthSession,
  loadStoredAuthSession,
  persistAuthSession
} from "./authSession";

export function useAuthSession() {
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null);
  const [authRestoring, setAuthRestoring] = useState(true);
  const [authRestoreAttempt, setAuthRestoreAttempt] = useState(0);
  const [authRestoreError, setAuthRestoreError] = useState<string | null>(null);
  const [logoutPending, setLogoutPending] = useState(false);
  const authRestoreRequestRef = useRef<{
    attempt: number;
    request: Promise<AuthSessionUser>;
  } | null>(null);

  useEffect(() => {
    let mounted = true;
    const stored = loadStoredAuthSession();
    if (!stored) {
      authRestoreRequestRef.current = null;
      setAuthRestoreError(null);
      setAuthRestoring(false);
      return () => {
        mounted = false;
      };
    }
    const activeRestore = authRestoreRequestRef.current;
    const restoreRequest = activeRestore?.attempt === authRestoreAttempt
      ? activeRestore.request
      : validateAuthSession(stored);
    authRestoreRequestRef.current = { attempt: authRestoreAttempt, request: restoreRequest };
    restoreRequest
      .then((validatedUser) => {
        if (!mounted) return;
        const validatedSession = { ...stored, user: validatedUser };
        establishApiSession(validatedSession);
        setAuthRestoreError(null);
        setCurrentUser(authSessionToUser(validatedSession));
      })
      .catch((caught) => {
        if (!mounted) return;
        clearApiAuthContext();
        if (isDefinitiveAuthFailure(caught)) {
          clearStoredAuthSession();
          setAuthRestoreError(null);
          return;
        }
        setAuthRestoreError("会话服务暂时不可用，已保留登录状态，可重试恢复。");
      })
      .finally(() => {
        if (mounted) setAuthRestoring(false);
      });
    return () => {
      mounted = false;
    };
  }, [authRestoreAttempt]);

  const retryRestore = () => {
    setAuthRestoreError(null);
    setAuthRestoring(true);
    setAuthRestoreAttempt((current) => current + 1);
  };

  const authenticate = async (email: string, password: string) => createDevAuthSession(email, password);

  const acceptSession = (session: AuthSession, remember: boolean) => {
    persistAuthSession(session, remember);
    establishApiSession(session);
    setAuthRestoreError(null);
    setCurrentUser(authSessionToUser(session));
  };

  const logout = async () => {
    if (logoutPending) return;
    setLogoutPending(true);
    try {
      if (currentUser?.authToken) {
        await revokeAuthSession(currentUser.authToken, currentUser.tenantId, currentUser.projectId);
      }
    } catch {
      // Local state must still be cleared when the BFF is unavailable.
    } finally {
      clearStoredAuthSession();
      clearApiAuthContext();
      setCurrentUser(null);
      setLogoutPending(false);
    }
  };

  return {
    acceptSession,
    authRestoreError,
    authRestoring,
    authenticate,
    currentUser,
    logout,
    logoutPending,
    retryRestore,
    setCurrentUser
  };
}
