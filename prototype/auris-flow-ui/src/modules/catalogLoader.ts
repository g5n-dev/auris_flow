const catalogLoads = new Map<string, Promise<unknown>>();

export function loadJsonCatalog(url: URL, label: string): Promise<unknown> {
  const key = url.href;
  const existing = catalogLoads.get(key);
  if (existing) return existing;

  const request = fetch(url, {
    cache: "force-cache",
    credentials: "same-origin"
  }).then(async (response) => {
    if (!response.ok) {
      throw new Error(`${label} 加载失败：${response.status}`);
    }
    return response.json() as Promise<unknown>;
  });
  catalogLoads.set(key, request);
  return request;
}

export function preloadJsonCatalog(url: URL, label: string): void {
  // Retain the same settled promise (including a rejection) so App observes
  // the exact preloaded result instead of issuing a racing duplicate request.
  void loadJsonCatalog(url, label).catch(() => undefined);
}
