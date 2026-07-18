export function isRecordValue(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

export function recordList(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter(isRecordValue) : [];
}

export function backendId(record: Record<string, unknown>, ...keys: string[]) {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return "";
}
