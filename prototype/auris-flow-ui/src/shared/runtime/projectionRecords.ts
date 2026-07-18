import { isRecordValue } from "./records";

export function projectionRecord(value: unknown, index: number, kind: string) {
  return isRecordValue(value) ? value : { id: `${kind}_${index + 1}` };
}

export function projectionText(record: Record<string, unknown>, keys: string[], fallback: string) {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return fallback;
}

export function projectionNumber(record: Record<string, unknown>, keys: string[], fallback = 0) {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  }
  return fallback;
}
