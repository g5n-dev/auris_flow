export type BackendAffectedObjectRef = {
  type: string;
  id: string;
  readback_url?: string;
  resource_version?: number;
};

export function normalizeAffectedObjectRefs(
  value: unknown
): BackendAffectedObjectRef[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value
    .filter(
      (item): item is Record<string, unknown> =>
        Boolean(item) &&
        typeof item === "object" &&
        typeof (item as Record<string, unknown>).type === "string" &&
        typeof (item as Record<string, unknown>).id === "string"
    )
    .map((item) => ({
      type: String(item.type),
      id: String(item.id),
      ...(typeof item.readback_url === "string" && item.readback_url
        ? { readback_url: item.readback_url }
        : {}),
      ...(typeof item.resource_version === "number" &&
      Number.isInteger(item.resource_version) &&
      item.resource_version > 0
        ? { resource_version: item.resource_version }
        : {})
    }));
}
