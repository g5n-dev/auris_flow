import type { CanvasModuleProps } from "../types";
import type { CanvasState } from "./useCanvasState";
import type { CanvasRunLog } from "../types";

export function buildCanvasPrimitiveActions(scope: CanvasModuleProps & CanvasState) {
  const { rememberTaskVersionId, setDraftState, setRecoveredTaskVersion, setRunHistory, setTaskReleaseGate } = scope;
  const markTaskDraftDirty = () => {
      setDraftState("未保存");
      rememberTaskVersionId(null);
      setRecoveredTaskVersion(null);
      setTaskReleaseGate(null);
    };

  const pushRunHistory = (name: string, state: string) => {
      const time = new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
      const nextRow: CanvasRunLog = {
        id: `run-log-${Date.now()}-${globalThis.crypto.randomUUID()}`,
        time,
        name,
        state
      };
      setRunHistory((current) => [nextRow, ...current].slice(0, 4));
    };

  const shortTrace = (trace?: string) => (trace ? trace.slice(0, 12) : "no-trace");

  return {
    markTaskDraftDirty,
    pushRunHistory,
    shortTrace
  };
}

export type CanvasPrimitiveActions = ReturnType<typeof buildCanvasPrimitiveActions>;
