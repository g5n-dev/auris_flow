import { useEffect } from "react";

import { getTaskVersion } from "../../api/client";
import {
  isImportBatchTerminal,
  type AudioImportBatch
} from "./audioImportFlowModel";

type BatchReadbackInput = {
  batchId: string;
  open: boolean;
  refreshBatch: (batchId: string) => Promise<AudioImportBatch>;
  setDetail: (detail: string) => void;
};

function pollReadback<T>(
  read: () => Promise<T>,
  isComplete: (value: T) => boolean,
  schedule: (load: () => Promise<void>) => number,
  setDetail: (detail: string) => void,
  failureDetail: string
) {
  let active = true;
  let timer: number | undefined;
  const load = async () => {
    try {
      const value = await read();
      if (!active || isComplete(value)) return;
    } catch (error) {
      if (!active) return;
      setDetail(error instanceof Error ? error.message : failureDetail);
    }
    if (active) timer = schedule(load);
  };
  void load();
  return () => {
    active = false;
    if (timer !== undefined) window.clearTimeout(timer);
  };
}

export function useAudioImportBatchReadback({
  batchId,
  open,
  refreshBatch,
  setDetail
}: BatchReadbackInput) {
  useEffect(() => {
    if (!open || !batchId) return;
    return pollReadback(
      () => refreshBatch(batchId),
      (next) => isImportBatchTerminal(next.status),
      (load) => window.setTimeout(() => void load(), 1800),
      setDetail,
      "同步批次回读失败"
    );
  }, [batchId, open, refreshBatch, setDetail]);
}

type PublishReadbackInput = {
  open: boolean;
  taskVersionId: string;
  taskVersionStatus: string;
  setTaskVersionStatus: (status: string) => void;
  setDetail: (detail: string) => void;
};

export function useAudioImportPublishReadback({
  open,
  taskVersionId,
  taskVersionStatus,
  setTaskVersionStatus,
  setDetail
}: PublishReadbackInput) {
  useEffect(() => {
    if (!open || !taskVersionId || taskVersionStatus !== "publishing") return;
    return pollReadback(
      () => getTaskVersion(taskVersionId),
      (response) => {
        if (String(response.data.status ?? "").toLowerCase() === "published") {
          setTaskVersionStatus("published");
          setDetail(`任务版本 ${taskVersionId} 已发布，可立即拉取。`);
          return true;
        }
        return false;
      },
      (load) => window.setTimeout(() => void load(), 2000),
      setDetail,
      "任务版本回读失败"
    );
  }, [
    open,
    setDetail,
    setTaskVersionStatus,
    taskVersionId,
    taskVersionStatus
  ]);
}
