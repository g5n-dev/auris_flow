import { getBackendRun, type BackendActionReceipt } from "./client";

export function backendReleaseRequester(receipt?: BackendActionReceipt | null) {
  if (!receipt) return null;
  const runDetail = receipt.raw.run_detail;
  const source = runDetail && typeof runDetail === "object"
    ? runDetail as Record<string, unknown>
    : receipt.raw;
  const releaseGate = source.release_gate;
  if (releaseGate && typeof releaseGate === "object") {
    const requestedBy = (releaseGate as Record<string, unknown>).requested_by;
    if (typeof requestedBy === "string" && requestedBy) return requestedBy;
  }
  const requestedBy = source.requested_by;
  return typeof requestedBy === "string" && requestedBy ? requestedBy : null;
}

export async function refreshBackendRunReceipt(
  receipt: BackendActionReceipt
): Promise<BackendActionReceipt> {
  try {
    const detail = await getBackendRun(receipt.id);
    return {
      ...receipt,
      ...detail.data,
      trace_id: detail.data.trace_id ?? receipt.trace_id,
      raw: {
        ...receipt.raw,
        run_detail: detail.data.raw
      }
    };
  } catch {
    return receipt;
  }
}
