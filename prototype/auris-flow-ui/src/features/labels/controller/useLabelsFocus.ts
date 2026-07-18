import type { LabelsModuleProps } from "../types";
import type { LabelsCoreState } from "./useLabelsCoreState";
import type { LabelsReleaseState } from "./useLabelsReleaseState";
import type { LabelsCandidateModel } from "./buildLabelsCandidateModel";
import { labelIntentFlows } from "../fixtures/scenarioCatalog";
import type { LabelIntentKey } from "../types";
import { useEffect } from "react";

type UseLabelsFocusScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel;

export function useLabelsFocus(focus: UseLabelsFocusScope["focus"], setActionFeedback: UseLabelsFocusScope["setActionFeedback"], setActiveIntentKey: UseLabelsFocusScope["setActiveIntentKey"], setSelectedCandidateId: UseLabelsFocusScope["setSelectedCandidateId"], setSelectedReviewId: UseLabelsFocusScope["setSelectedReviewId"]) {
  useEffect(() => {
      if (focus?.module !== "labels") return;
      if (focus.objectKind === "labelIntent" && focus.objectId && labelIntentFlows.some((item) => item.key === focus.objectId)) {
        setActiveIntentKey(focus.objectId as LabelIntentKey);
        setActionFeedback(`${focus.origin?.label ?? "关联跳转"} 已定位标签意图：${focus.title ?? focus.objectId}。`);
      }
      if (focus.objectKind === "labelCandidate" && focus.objectId) {
        const [, intentKey] = focus.objectId.match(/^LC-([^-]+)/) ?? [];
        if (intentKey && labelIntentFlows.some((item) => item.key === intentKey)) {
          setActiveIntentKey(intentKey as LabelIntentKey);
        }
        setSelectedCandidateId(focus.objectId);
        setActionFeedback(`${focus.origin?.label ?? "关联跳转"} 已定位候选标签：${focus.objectId}。`);
      }
      if (focus.objectKind === "labelReview" && focus.objectId) {
        setSelectedReviewId(focus.objectId);
        setActionFeedback(`${focus.origin?.label ?? "关联跳转"} 已定位 Human Loop：${focus.objectId}。`);
      }
    }, [focus?.module, focus?.objectKind, focus?.objectId, focus?.title]);

  return {

  };
}

export type LabelsFocusModel = ReturnType<typeof useLabelsFocus>;
