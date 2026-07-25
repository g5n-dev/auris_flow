import { useCallback, useEffect, useRef, useState } from "react";
import {
  clearApiAuthContext,
  establishApiSession,
  subscribeApiAuthEvents
} from "../api/client";
import {
  beginOidcLogin,
  createDevAuthSession,
  isDefinitiveAuthFailure,
  restoreBrowserAuthSession,
  revokeBrowserAuthSession
} from "../api/authClient";
import type { AuthSession, AuthUser } from "../shared/contracts/auth";
import {
  authSessionToUser,
  clearLegacyStoredAuthSession
} from "./authSession";

export function useAuthSession() {
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null);
  const [authRestoring, setAuthRestoring] = useState(true);
  const [authRestoreAttempt, setAuthRestoreAttempt] = useState(0);
  const [authRestoreError, setAuthRestoreError] = useState<string | null>(null);
  const [logoutPending, setLogoutPending] = useState(false);
  const authRestoreRequestRef = useRef<{
    attempt: number;
    request: Promise<AuthSession>;
  } | null>(null);

  const applySession = useCallback((session: AuthSession) => {
    establishApiSession(session);
    setAuthRestoreError(null);
    setCurrentUser(authSessionToUser(session));
  }, []);

  useEffect(() => {
    let mounted = true;
    clearLegacyStoredAuthSession();
    const activeRestore = authRestoreRequestRef.current;
    const restoreRequest = activeRestore?.attempt === authRestoreAttempt
      ? activeRestore.request
      : restoreBrowserAuthSession();
    authRestoreRequestRef.current = { attempt: authRestoreAttempt, request: restoreRequest };
    restoreRequest
      .then((session) => {
        if (!mounted) return;
        applySession(session);
      })
      .catch((caught) => {
        if (!mounted) return;
        clearApiAuthContext();
        if (isDefinitiveAuthFailure(caught)) {
          setAuthRestoreError(null);
          return;
        }
        setAuthRestoreError("会话服务暂时不可用，可重试通过安全 Cookie 恢复。");
      })
      .finally(() => {
        if (mounted) setAuthRestoring(false);
      });
    return () => {
      mounted = false;
    };
  }, [applySession, authRestoreAttempt]);

  useEffect(() => subscribeApiAuthEvents((event) => {
    if (event.kind === "reauth-required") {
      clearApiAuthContext();
      setCurrentUser(null);
      setAuthRestoreError(null);
      return;
    }
    void restoreBrowserAuthSession()
      .then(applySession)
      .catch((caught) => {
        if (isDefinitiveAuthFailure(caught)) {
          clearApiAuthContext();
          setCurrentUser(null);
        }
      });
  }), [applySession]);

  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return undefined;
    const channel = new BroadcastChannel("auris-flow.session-scope.v1");
    channel.onmessage = (event) => {
      if (event.data?.type !== "scope-changed") return;
      void restoreBrowserAuthSession()
        .then(applySession)
        .catch((caught) => {
          if (isDefinitiveAuthFailure(caught)) {
            clearApiAuthContext();
            setCurrentUser(null);
          }
        });
    };
    return () => channel.close();
  }, [applySession]);

  const retryRestore = () => {
    setAuthRestoreError(null);
    setAuthRestoring(true);
    setAuthRestoreAttempt((current) => current + 1);
  };

  const authenticate = async (email: string, password: string) => createDevAuthSession(email, password);

  const acceptSession = applySession;

  const logout = async () => {
    if (logoutPending) return;
    setLogoutPending(true);
    setAuthRestoreError(null);
    try {
      if (currentUser) {
        await revokeBrowserAuthSession(currentUser.tenantId, currentUser.projectId);
      }
    } catch {
      setAuthRestoreError("服务端未确认会话注销；本地认证上下文已清除，请检查网络后重试登录。");
    } finally {
      clearLegacyStoredAuthSession();
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
    beginOidcLogin,
    currentUser,
    logout,
    logoutPending,
    retryRestore,
    setCurrentUser
  };
}
