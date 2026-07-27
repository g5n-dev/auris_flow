import type { AuthUser } from "../contracts/auth";

export const defaultCurrentUser: AuthUser = {
  userId: "u_admin_001",
  name: "Demo Operator",
  email: "demo.operator@auris.local",
  role: "平台管理员",
  tenant: "极光汽车",
  project: "销售话术质检",
  initials: "D",
  roles: ["project_admin", "asset_manager"],
  tenantId: "aurora_auto",
  projectId: "sales_qa",
  authToken: "",
  expiresAt: "",
  projectMemberships: [
    {
      project_id: "sales_qa",
      project_name: "销售话术质检",
      roles: ["project_admin", "asset_manager"]
    }
  ]
};
