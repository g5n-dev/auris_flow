type PreviewValueInput = {
  audioUrlFieldPath: string;
  field: string;
  record: Record<string, unknown>;
};

const EMPTY_PREVIEW_VALUE = "—";
const REDACTED_PREVIEW_VALUE = "已返回（敏感值已隐藏）";
const SENSITIVE_FIELD = /(url|token|authorization|credential|secret|password|api[_-]?key)/i;
const SENSITIVE_TEXT = /(?:https?|wss):\/\/|secret:\/\/|(?:bearer|basic)\s+\S+|(?:access[_-]?token|token|authorization|credential|secret|password|api[_-]?key)\s*[:=]/i;
const JWT_LIKE_VALUE = /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/;

const normalizedPath = (path: string) =>
  path.split(".").map((segment) => segment.trim()).filter(Boolean).join(".");

const hasPreviewValue = (value: unknown) =>
  value !== null && value !== undefined && value !== "";

function readPreviewValue(record: Record<string, unknown>, field: string) {
  if (Object.prototype.hasOwnProperty.call(record, field)) return record[field];
  return normalizedPath(field).split(".").reduce<unknown>((current, segment) => {
    if (!current || typeof current !== "object" || Array.isArray(current)) return undefined;
    return (current as Record<string, unknown>)[segment];
  }, record);
}

function containsSensitiveValue(value: unknown, depth = 0): boolean {
  if (depth > 8) return true;
  if (typeof value === "string") {
    return SENSITIVE_TEXT.test(value) || JWT_LIKE_VALUE.test(value);
  }
  if (Array.isArray(value)) {
    return value.some((item) => containsSensitiveValue(item, depth + 1));
  }
  if (!value || typeof value !== "object") return false;
  return Object.entries(value as Record<string, unknown>).some(
    ([key, item]) => SENSITIVE_FIELD.test(key) || containsSensitiveValue(item, depth + 1)
  );
}

function isMappedAudioPath(field: string, audioUrlFieldPath: string) {
  const fieldPath = normalizedPath(field).toLowerCase();
  const mappedPath = normalizedPath(audioUrlFieldPath).toLowerCase();
  return Boolean(
    fieldPath
    && mappedPath
    && (fieldPath === mappedPath || mappedPath.startsWith(`${fieldPath}.`))
  );
}

export function formatAudioImportPreviewValue({
  audioUrlFieldPath,
  field,
  record
}: PreviewValueInput) {
  const value = readPreviewValue(record, field);
  if (!hasPreviewValue(value)) return EMPTY_PREVIEW_VALUE;
  if (
    SENSITIVE_FIELD.test(field)
    || isMappedAudioPath(field, audioUrlFieldPath)
    || containsSensitiveValue(value)
  ) {
    return REDACTED_PREVIEW_VALUE;
  }
  if (typeof value !== "object") return String(value);
  try {
    const serialized = JSON.stringify(value);
    return serialized.length > 240 ? `${serialized.slice(0, 237)}...` : serialized;
  } catch {
    return "对象值不可预览";
  }
}
