export type AuthUser = {
  userId: string;
  name: string;
  email: string;
  role: string;
  tenant: string;
  project: string;
  initials: string;
  roles: string[];
  tenantId: string;
  projectId: string;
  /** Legacy shape retained for fixture compatibility; browser sessions always keep this empty. */
  authToken: string;
  expiresAt: string;
  provider?: string;
};

export type AuthSessionUser = {
  user_id: string;
  name: string;
  email: string;
  role: string;
  roles: string[];
  initials: string;
  tenant_id: string;
  tenant_name: string;
  project_id: string;
  project_name: string;
  provider?: string;
  csrf_token?: string;
};

export type AuthSession = {
  access_token?: string;
  token_type?: "Bearer";
  expires_at?: string;
  provider?: string;
  csrf_token?: string;
  user: AuthSessionUser;
};

export type AuthLogoutReceipt = {
  status: "revoked";
  session_id: string;
  revoked_at: string;
};
