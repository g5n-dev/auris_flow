import type { EvaluationModuleProps } from "../types";
import type { EvaluationState } from "./useEvaluationState";
import type { EvaluationSelection } from "./buildEvaluationSelection";
import { listHotwordBadcases } from "../../../api/client";
import type { EvaluationCapabilityKey } from "../../../shared/contracts/evaluation";
import { deepLinkBadcaseRegistry } from "../../../shared/fixtures/deepLinkBadcases";
import { evaluationBadcaseFromApi } from "../badcaseViews";
import { useEffect } from "react";

type UseEvaluationFocusRecoveryScope = EvaluationModuleProps & EvaluationState & EvaluationSelection;

export function useEvaluationFocusRecovery(currentView: UseEvaluationFocusRecoveryScope["currentView"], focus: UseEvaluationFocusRecoveryScope["focus"], selectedCapabilityKey: UseEvaluationFocusRecoveryScope["selectedCapabilityKey"], setBadcaseCapabilityFilter: UseEvaluationFocusRecoveryScope["setBadcaseCapabilityFilter"], setBadcaseWorkflow: UseEvaluationFocusRecoveryScope["setBadcaseWorkflow"], setEvaluationNotice: UseEvaluationFocusRecoveryScope["setEvaluationNotice"], setHotwordBadcaseRecovery: UseEvaluationFocusRecoveryScope["setHotwordBadcaseRecovery"], setSelectedBadcaseId: UseEvaluationFocusRecoveryScope["setSelectedBadcaseId"], setSelectedCapabilityKey: UseEvaluationFocusRecoveryScope["setSelectedCapabilityKey"], setSelectedDatasetId: UseEvaluationFocusRecoveryScope["setSelectedDatasetId"], setSelectedLabelingCaseId: UseEvaluationFocusRecoveryScope["setSelectedLabelingCaseId"]) {
  useEffect(() => {
      if (focus?.module !== "evaluation") return;
      if (focus.objectKind === "evaluationBadcase" && focus.objectId) {
        setSelectedBadcaseId(focus.objectId);
        const capability = (deepLinkBadcaseRegistry[focus.objectId]?.capability as EvaluationCapabilityKey) ?? selectedCapabilityKey;
        setSelectedCapabilityKey(capability);
        setBadcaseCapabilityFilter(capability === "asr-hotword" ? "asr-hotword" : "all");
        setHotwordBadcaseRecovery(capability === "asr-hotword" ? "loading" : "idle");
        setEvaluationNotice({
          status: capability === "asr-hotword" ? "pending" : "success",
          title: capability === "asr-hotword" ? `正在恢复 badcase：${focus.objectId}` : `已定位 badcase：${focus.title ?? focus.objectId}`,
          detail: `${focus.origin?.label ?? "关联跳转"} → ${focus.objectId}。`
        });
      }
      if (focus.objectKind === "evaluationCase" && focus.objectId) {
        setSelectedLabelingCaseId(focus.objectId);
        setEvaluationNotice({
          status: "success",
          title: `已定位打标样本：${focus.title ?? focus.objectId}`,
          detail: `${focus.origin?.label ?? "关联跳转"} → ${focus.objectId}。`
        });
      }
      if (focus.objectKind === "evaluationDataset" && focus.objectId) {
        setSelectedDatasetId(focus.objectId);
      }
      if (focus.objectKind === "evaluationCapability" && focus.objectId) {
        setSelectedCapabilityKey(focus.objectId as EvaluationCapabilityKey);
      }
    }, [focus?.module, focus?.objectKind, focus?.objectId, focus?.title]);

  useEffect(() => {
      if (currentView !== "badcase") return;
      let active = true;
      setHotwordBadcaseRecovery("loading");
      void listHotwordBadcases({ limit: 100 })
        .then((response) => {
          if (!active) return;
          const apiItems = response.data.items.map(evaluationBadcaseFromApi);
          setBadcaseWorkflow((current) => [
            ...current.filter((item) => item.capability !== "asr-hotword"),
            ...apiItems
          ]);
          const focusedId = focus?.objectKind === "evaluationBadcase" ? focus.objectId : null;
          if (focusedId && !apiItems.some((item) => item.id === focusedId)) {
            setHotwordBadcaseRecovery("missing");
            setEvaluationNotice({
              status: "error",
              title: "ASR 热词 Badcase 恢复已阻断",
              detail: `${focusedId} 未出现在当前项目 /badcases?capability=asr-hotword 响应中；不会回退到 B-2031。`
            });
            return;
          }
          setHotwordBadcaseRecovery("resolved");
          if (focusedId) {
            setEvaluationNotice({
              status: "success",
              title: `ASR 热词 Badcase 已恢复：${focusedId}`,
              detail: `${apiItems.length} 个后端对象 · Trace ${response.meta?.trace_id ?? "no-trace"}`
            });
          }
        })
        .catch((error) => {
          if (!active) return;
          setBadcaseWorkflow((current) => current.filter((item) => item.capability !== "asr-hotword"));
          setHotwordBadcaseRecovery("error");
          setEvaluationNotice({
            status: "error",
            title: "ASR 热词 Badcase 读取失败",
            detail: `${error instanceof Error ? error.message : "BFF 请求失败"}；未使用本地 seed 兜底。`
          });
        });
      return () => {
        active = false;
      };
    }, [currentView, focus?.objectKind, focus?.objectId]);

  return {

  };
}

export type EvaluationFocusRecovery = ReturnType<typeof useEvaluationFocusRecovery>;
