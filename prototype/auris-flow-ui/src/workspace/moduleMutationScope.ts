export type ScopedMutationRecord = {
  scopeKey: string;
  idempotencyKey: string;
};

export function createModuleMutationIntentIdempotencyKey(scope: string): string {
  const intentId = globalThis.crypto?.randomUUID?.()
    ?? `intent-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  return `${scope}:intent:${intentId}`;
}

export function recordsForMutationScope<T extends ScopedMutationRecord>(
  records: T[],
  currentScopeKey: string
): T[] {
  return records.every((record) => record.scopeKey === currentScopeKey)
    ? records
    : records.filter((record) => record.scopeKey === currentScopeKey);
}

export function mutationWriteOptions(record: ScopedMutationRecord): { idempotencyKey: string } {
  if (!record.idempotencyKey.trim()) throw new Error("mutation record 缺少 user intent idempotency key");
  return { idempotencyKey: record.idempotencyKey };
}
