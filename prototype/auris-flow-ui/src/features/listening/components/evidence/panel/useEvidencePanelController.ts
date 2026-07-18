import { useEvidencePanelState } from "./useEvidencePanelState";
import { useEvidencePanelRecovery } from "./useEvidencePanelRecovery";
import { createHotwordCorrectionActions } from "./hotwordCorrectionActions";
import type { EvidencePanelProps } from "./evidencePanelTypes";

export function useEvidencePanelController(props: EvidencePanelProps) {
  const step1 = useEvidencePanelState(props);
  const step2 = useEvidencePanelRecovery(step1);
  return createHotwordCorrectionActions(step2);
}

export type EvidencePanelController = ReturnType<typeof useEvidencePanelController>;
