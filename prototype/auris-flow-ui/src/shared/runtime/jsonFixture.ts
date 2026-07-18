const fixtureCache = new Map<string, Promise<unknown>>();

export function loadJsonFixture<T>(url: URL, label: string): Promise<T> {
  const key = url.href;
  const cached = fixtureCache.get(key);
  if (cached) return cached as Promise<T>;

  const request = fetch(url, {
    cache: "force-cache",
    credentials: "same-origin"
  }).then(async (response) => {
    if (!response.ok) {
      throw new Error(`${label}请求失败（HTTP ${response.status}）`);
    }
    return response.json() as Promise<T>;
  }).catch((error) => {
    fixtureCache.delete(key);
    throw error;
  });

  fixtureCache.set(key, request);
  return request;
}
