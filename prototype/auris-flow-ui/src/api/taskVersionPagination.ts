type CollectionEnvelope = {
  data: unknown;
  meta?: {
    next_cursor?: unknown;
    [key: string]: unknown;
  };
};

type CollectionRequest<T extends CollectionEnvelope = CollectionEnvelope> = (
  path: string
) => Promise<T>;

export async function listAllCollectionItems<T, R extends CollectionEnvelope>(
  request: CollectionRequest<R>,
  path: string,
  label: string,
  readItems: (data: unknown) => T[]
) {
  const items: T[] = [];
  const observedCursors = new Set<string>();
  let cursor = "";
  for (let page = 0; page < 50; page += 1) {
    const query = new URLSearchParams({ limit: "200" });
    if (cursor) query.set("cursor", cursor);
    const response = await request(`${path}?${query.toString()}`);
    items.push(...readItems(response.data));
    const nextCursor = String(response.meta?.next_cursor ?? "");
    if (!nextCursor) return { response, items };
    if (observedCursors.has(nextCursor)) {
      throw new Error(`${label} 分页游标重复，无法恢复最新配置`);
    }
    observedCursors.add(nextCursor);
    cursor = nextCursor;
  }
  throw new Error(`${label} 数量超过前端安全分页上限，无法恢复最新配置`);
}

type TaskVersionCollectionEnvelope = CollectionEnvelope & {
  data: { items: Array<Record<string, unknown>> };
};

export async function listAllTaskVersions(
  request: CollectionRequest<TaskVersionCollectionEnvelope>
) {
  const { response, items } = await listAllCollectionItems(
    request,
    "/v1/task-versions",
    "TaskVersion",
    (data) => (data as TaskVersionCollectionEnvelope["data"]).items
  );
  return { ...response, data: { items } };
}
