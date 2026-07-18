type ApiRequestErrorOptions = {
  code?: string;
  details?: unknown;
  retryable?: boolean;
  traceId?: string;
};

const asRecord = (value: unknown): Record<string, unknown> | undefined =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;

const asString = (value: unknown): string | undefined =>
  typeof value === "string" ? value : undefined;

export class ApiRequestError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly details?: unknown;
  readonly retryable: boolean;
  readonly traceId?: string;

  constructor(message: string, status: number, options: ApiRequestErrorOptions = {}) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = options.code;
    this.details = options.details;
    this.retryable = options.retryable ?? false;
    this.traceId = options.traceId;
  }
}

export const isApiRequestError = (error: unknown): error is ApiRequestError =>
  error instanceof ApiRequestError;

export function parseApiRequestError(body: unknown, status: number): ApiRequestError {
  const responseBody = asRecord(body);
  const error = asRecord(responseBody?.error);
  const details = error?.details;
  const traceId =
    asString(asRecord(responseBody?.meta)?.trace_id) ??
    asString(error?.trace_id) ??
    asString(asRecord(details)?.trace_id);
  return new ApiRequestError(asString(error?.message) ?? `API ${status}`, status, {
    code: asString(error?.code),
    details,
    retryable: error?.retryable === true,
    traceId
  });
}
