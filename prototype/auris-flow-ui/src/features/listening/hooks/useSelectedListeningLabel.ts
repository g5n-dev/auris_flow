import type { ListeningReadModel } from "./useListeningReadModel";
import { useMemo } from "react";

export function useSelectedListeningLabel(context: ListeningReadModel) {
  const { markState } = context;
  const selectedLabel = useMemo(() => {
      if (markState === "main") return "已标记主录音";
      if (markState === "crosstalk") return "已标记串音";
      if (markState === "duplicate") return "已标记重复收录";
      return "待复核";
    }, [markState]);

  return { ...context, selectedLabel };
}

export type SelectedListeningLabel = ReturnType<typeof useSelectedListeningLabel>;
