import type { EntitySelectOption } from "../../shared/ui/EntitySelect";
import type { TenantAsrBinding, TenantMember, TenantProject, TenantRow } from "./types";
import type tenantsFixtureSchema from "./fixtures/data/tenants-fixtures.json";
import { loadJsonFixture } from "../../shared/runtime/jsonFixture";

const tenantsFixture = await loadJsonFixture<typeof tenantsFixtureSchema>(
  new URL("./fixtures/data/tenants-fixtures.json", import.meta.url),
  "租户 fixture"
);


export const tenantRows = (tenantsFixture.fixtures.tenantRows as unknown as { name: string; status: string; projects: number; members: number; storage: string; risk: string; }[]);

export const createTenantSceneValue = "__create_tenant_scene__";

export const tenantSceneOptions: EntitySelectOption[] = (tenantsFixture.fixtures.tenantSceneOptions as unknown as EntitySelectOption[]);

export const tenantQuotaOptions: EntitySelectOption[] = (tenantsFixture.fixtures.tenantQuotaOptions as unknown as EntitySelectOption[]);

export const tenantProjects: Record<string, TenantProject[]> = (tenantsFixture.fixtures.tenantProjects as unknown as Record<string, TenantProject[]>);

export const tenantMembers: Record<string, TenantMember[]> = (tenantsFixture.fixtures.tenantMembers as unknown as Record<string, TenantMember[]>);

export const tenantAuditItems: Record<string, Array<[string, string, string]>> = (tenantsFixture.fixtures.tenantAuditItems as unknown as Record<string, [string, string, string][]>);

export const tenantAsrBindings: Record<string, TenantAsrBinding> = (tenantsFixture.fixtures.tenantAsrBindings as unknown as Record<string, TenantAsrBinding>);

export const defaultTenantAsrBinding: TenantAsrBinding = (tenantsFixture.fixtures.defaultTenantAsrBinding as unknown as TenantAsrBinding);
