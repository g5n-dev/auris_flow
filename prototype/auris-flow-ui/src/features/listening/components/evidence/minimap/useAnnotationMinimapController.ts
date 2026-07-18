import { useAnnotationMinimapState } from "./useAnnotationMinimapState";
import { buildAnnotationAssociationModel } from "./annotationAssociationModel";
import { buildAnnotationBoundaryModel } from "./annotationBoundaryModel";
import { createBoundaryExtensionActions } from "./boundaryExtensionActions";
import { createConversationBoundaryActions } from "./conversationBoundaryActions";
import type { AnnotationMinimapProps } from "./annotationMinimapTypes";

export function useAnnotationMinimapController(props: AnnotationMinimapProps) {
  const step1 = useAnnotationMinimapState(props);
  const step2 = buildAnnotationAssociationModel(step1);
  const step3 = buildAnnotationBoundaryModel(step2);
  const step4 = createBoundaryExtensionActions(step3);
  return createConversationBoundaryActions(step4);
}

export type AnnotationMinimapController = ReturnType<typeof useAnnotationMinimapController>;
