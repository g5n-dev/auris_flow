export type TopbarContextKey = "tenant" | "project" | "store" | "date" | "model" | "label";

export type TopbarContextState = Record<TopbarContextKey, string>;

export type WorkspaceContextOption = {
  id: string;
  label: string;
  status?: string;
  meta?: string;
};

export type WorkspaceContextOptions = {
  scope: {
    tenant_id: string;
    tenant_name: string;
    project_id: string;
    project_name: string;
  };
  stores: Array<{ store_id: string; name: string; status: string }>;
  business_dates: string[];
  model_versions: Array<{
    id: string;
    label: string;
    status: string;
    source_task_version_ids?: string[];
  }>;
  label_versions: Array<{ id: string; label: string; status: string }>;
  defaults: {
    store_id: string | null;
    business_date: string | null;
    model_version: string | null;
    label_version: string | null;
  };
  active_scene_binding: {
    binding_id: string;
    environment: string;
    scene_profile_id: string;
    scene_profile_version_id: string;
    manifest_sha256: string;
    status: string;
    resource_version: number;
    trace_id?: string;
  } | null;
  as_of: string;
  trace_id: string;
};
