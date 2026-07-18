import type {
  ModuleWorkspaceGateway,
  WorkspaceBackendActionReceipt
} from "../shared/contracts/moduleWorkspaceGateway";

export async function refreshBackendRunReceipt(
  receipt: WorkspaceBackendActionReceipt,
  gateway: Pick<ModuleWorkspaceGateway, "getBackendRun">
): Promise<WorkspaceBackendActionReceipt> {
  try {
    const detail = await gateway.getBackendRun(receipt.id);
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
