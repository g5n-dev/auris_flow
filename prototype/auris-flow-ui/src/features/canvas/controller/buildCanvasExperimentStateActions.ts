import { getControlledExperiment, type ControlledExperiment } from "../../../api/client";
import type { CanvasActionScope } from "./canvasActionScope";

export function buildCanvasExperimentStateActions(scope: CanvasActionScope) {
  const {
    setControlledExperiment,
    setExperimentMode,
    setMetricDraftState,
    setSelectedExperimentMetricKey
  } = scope;

  const applyControlledExperiment = (experiment: ControlledExperiment) => {
    const modeByStatus: Record<ControlledExperiment["status"], string> = {
      draft: "草稿",
      running: "灰度中",
      paused: "暂停",
      stopped: "暂停",
      decided: "已决策"
    };
    setControlledExperiment(experiment);
    setExperimentMode(modeByStatus[experiment.status]);
    setSelectedExperimentMetricKey(experiment.primary_metric.metric_key);
    setMetricDraftState("发布闸门");
  };

  const reloadControlledExperiment = async (experimentId: string) => {
    const detail = (await getControlledExperiment(experimentId)).data;
    applyControlledExperiment(detail);
    return detail;
  };

  return { applyControlledExperiment, reloadControlledExperiment };
}

export type CanvasExperimentStateActions = ReturnType<typeof buildCanvasExperimentStateActions>;
