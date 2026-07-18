import { useListeningState } from "./useListeningState";
import { useListeningReadModel } from "./useListeningReadModel";
import { useSelectedListeningLabel } from "./useSelectedListeningLabel";
import { createReviewDecisionActions } from "./reviewDecisionActions";
import { buildListeningPresentation } from "../model/listeningPresentation";
import { useListeningFocus } from "./useListeningFocus";
import type { ListeningFeatureProps } from "../types";

export function useListeningController(props: ListeningFeatureProps) {
  const state = useListeningState(props);
  const readModel = useListeningReadModel(state);
  const labelModel = useSelectedListeningLabel(readModel);
  const actions = createReviewDecisionActions(labelModel);
  const presentation = buildListeningPresentation(actions);
  return useListeningFocus(presentation);
}

export type ListeningController = ReturnType<typeof useListeningController>;
