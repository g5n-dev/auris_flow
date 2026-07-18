import type { EvaluationModuleProps } from "../types";
import type { EvaluationState } from "./useEvaluationState";
import type { EvaluationSelection } from "./buildEvaluationSelection";
import type { EvaluationFocusRecovery } from "./useEvaluationFocusRecovery";
import type { EvaluationContextActions } from "./buildEvaluationContextActions";
import type { HotwordPollingActions } from "./buildHotwordPollingActions";
import { getBackendRun } from "../../../api/client";
import { useEffect } from "react";

type UseHotwordVersionRecoveryScope = EvaluationModuleProps & EvaluationState & EvaluationSelection & EvaluationFocusRecovery & EvaluationContextActions & HotwordPollingActions;

export function useHotwordVersionRecovery(captureHotwordEvalResult: UseHotwordVersionRecoveryScope["captureHotwordEvalResult"], currentUser: UseHotwordVersionRecoveryScope["currentUser"], currentView: UseHotwordVersionRecoveryScope["currentView"], discoverHotwordCandidateVersion: UseHotwordVersionRecoveryScope["discoverHotwordCandidateVersion"], hotwordPollGenerationRef: UseHotwordVersionRecoveryScope["hotwordPollGenerationRef"], hotwordPollTimerRef: UseHotwordVersionRecoveryScope["hotwordPollTimerRef"], pollHotwordBuildRun: UseHotwordVersionRecoveryScope["pollHotwordBuildRun"], pollHotwordEvalRun: UseHotwordVersionRecoveryScope["pollHotwordEvalRun"], setEvaluationAction: UseHotwordVersionRecoveryScope["setEvaluationAction"], setEvaluationNotice: UseHotwordVersionRecoveryScope["setEvaluationNotice"], setHotwordVersionLoading: UseHotwordVersionRecoveryScope["setHotwordVersionLoading"]) {
  useEffect(() => {
      if (currentView !== "compare" && currentView !== "badcase") return;
      let active = true;
      setHotwordVersionLoading(true);
      void discoverHotwordCandidateVersion(false)
        .then(async (version) => {
          if (!active || !version) return;
          const generation = hotwordPollGenerationRef.current + 1;
          if (version.status === "validating" && version.buildRunId) {
            hotwordPollGenerationRef.current = generation;
            setEvaluationAction("hotword_build");
            void pollHotwordBuildRun(version.id, version.buildRunId, generation, version.rootTraceId ?? undefined)
              .finally(() => {
                if (active && hotwordPollGenerationRef.current === generation) setEvaluationAction(null);
              });
            return;
          }
          if (version.evalRunId && ["review_required", "approved", "published", "gate_blocked"].includes(version.status)) {
            const run = await getBackendRun(version.evalRunId);
            if (active) captureHotwordEvalResult(run.data.raw);
            return;
          }
          if (!version.evalRunId || version.status !== "evaluating") return;
          hotwordPollGenerationRef.current = generation;
          setEvaluationAction("hotword_eval");
          void pollHotwordEvalRun(version.id, version.evalRunId, generation, version.rootTraceId ?? undefined)
            .finally(() => {
              if (active && hotwordPollGenerationRef.current === generation) setEvaluationAction(null);
            });
        })
        .catch((error) => {
          if (!active) return;
          setEvaluationNotice({
            status: "error",
            title: "词包版本恢复失败",
            detail: error instanceof Error ? error.message : "无法从 API 恢复候选版本。"
          });
        })
        .finally(() => {
          if (active) setHotwordVersionLoading(false);
        });
      return () => {
        active = false;
        hotwordPollGenerationRef.current += 1;
        if (hotwordPollTimerRef.current !== null) {
          window.clearTimeout(hotwordPollTimerRef.current);
          hotwordPollTimerRef.current = null;
        }
      };
    }, [currentView, currentUser.userId]);

  return {

  };
}

export type HotwordVersionRecovery = ReturnType<typeof useHotwordVersionRecovery>;
