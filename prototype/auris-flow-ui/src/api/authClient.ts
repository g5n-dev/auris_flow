import type {
  AuthLogoutReceipt,
  AuthSession,
  AuthSessionUser
} from "../shared/contracts/auth";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api";
const DEVELOPMENT_MODE = import.meta.env.DEV;
const DEMO_SCOPE_HEADERS = {
  "X-Tenant-Id": "aurora_auto",
  "X-Project-Id": "sales_qa"
};

let browserSessionCsrfToken = "";

const requestId = () =>
  `ui-auth-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

type ApiEnvelope<T> = {
  data: T;
  meta?: {
    trace_id?: string;
    request_id?: string;
    [key: string]: unknown;
  };
};

export class AuthRequestError extends Error {
  readonly status: number;
  readonly code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "AuthRequestError";
    this.status = status;
    this.code = code;
  }
}

export const isDefinitiveAuthFailure = (error: unknown) =>
  error instanceof AuthRequestError && (error.status === 401 || error.status === 403);

async function authRequest<T>(path: string, options: RequestInit = {}): Promise<ApiEnvelope<T>> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers ?? {}),
      "X-Request-Id": requestId()
    },
    credentials: "include"
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new AuthRequestError(
      body?.error?.message ?? `认证服务返回 ${response.status}`,
      response.status,
      body?.error?.code
    );
  }
  return body as ApiEnvelope<T>;
}

export function setBrowserSessionCsrfToken(token?: string | null) {
  browserSessionCsrfToken = token?.trim() ?? "";
}

export function getBrowserSessionCsrfToken() {
  return browserSessionCsrfToken;
}

export function clearBrowserSessionSecurityContext() {
  browserSessionCsrfToken = "";
}

export async function createDevAuthSession(email: string, password: string): Promise<AuthSession> {
  const response = await authRequest<AuthSession>("/v1/auth/dev-login", {
    method: "POST",
    body: JSON.stringify({ email, password })
  });
  // The backend keeps a compatibility bearer for non-browser development clients.
  // The browser deliberately discards it and uses only the HttpOnly cookie + CSRF token.
  const {
    access_token: _discardedCompatibilityToken,
    token_type: _discardedTokenType,
    ...cookieSession
  } = response.data;
  void _discardedCompatibilityToken;
  void _discardedTokenType;
  return cookieSession;
}

export async function restoreBrowserAuthSession(): Promise<AuthSession> {
  const response = await authRequest<AuthSessionUser>("/v1/auth/session", {
    headers: DEVELOPMENT_MODE ? DEMO_SCOPE_HEADERS : undefined
  });
  const { csrf_token: csrfToken, provider, ...user } = response.data;
  return {
    provider,
    csrf_token: csrfToken,
    user
  };
}

export async function revokeBrowserAuthSession(
  tenantId: string,
  projectId: string
): Promise<AuthLogoutReceipt> {
  const csrfToken = getBrowserSessionCsrfToken();
  const response = await authRequest<AuthLogoutReceipt>("/v1/auth/logout", {
    method: "POST",
    headers: {
      "X-Tenant-Id": tenantId,
      "X-Project-Id": projectId,
      ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {})
    }
  });
  return response.data;
}

export function buildOidcLoginUrl(returnPath: string) {
  const safeReturnPath = returnPath.startsWith("/") && !returnPath.startsWith("//")
    ? returnPath
    : "/";
  const query = new URLSearchParams({ return_path: safeReturnPath });
  return `${API_BASE_URL}/v1/auth/oidc/login?${query.toString()}`;
}

export function beginOidcLogin() {
  const returnPath = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  window.location.assign(buildOidcLoginUrl(returnPath));
}
