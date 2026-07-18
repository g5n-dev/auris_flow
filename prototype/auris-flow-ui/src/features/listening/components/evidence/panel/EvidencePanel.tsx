import type { EvidencePanelProps } from "./evidencePanelTypes";
import { EvidencePanelView } from "./EvidencePanelView";
import { useEvidencePanelController } from "./useEvidencePanelController";

export function EvidencePanel(props: EvidencePanelProps) {
  const controller = useEvidencePanelController(props);
  return <EvidencePanelView controller={controller} />;
}
