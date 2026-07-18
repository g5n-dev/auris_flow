import type { LabelsModuleProps } from "../types";
import type { LabelsCoreState } from "./useLabelsCoreState";
import type { LabelsReleaseState } from "./useLabelsReleaseState";
import type { LabelsCandidateModel } from "./buildLabelsCandidateModel";
import type { LabelsFocusModel } from "./useLabelsFocus";
import type { LabelsChangeModel } from "./buildLabelsChangeModel";
import type { LabelsGovernanceModel } from "./buildLabelsGovernanceModel";
import type { LabelsConflictModel } from "./buildLabelsConflictModel";
import { useEffect } from "react";

type UseLabelsIntentRecoveryScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel & LabelsGovernanceModel & LabelsConflictModel;

export function useLabelsIntentRecovery(activeIntent: UseLabelsIntentRecoveryScope["activeIntent"], activeIntentKey: UseLabelsIntentRecoveryScope["activeIntentKey"], activeScenario: UseLabelsIntentRecoveryScope["activeScenario"], draftMatch: UseLabelsIntentRecoveryScope["draftMatch"], setAutomationLevel: UseLabelsIntentRecoveryScope["setAutomationLevel"], setBatchDecisionReceipt: UseLabelsIntentRecoveryScope["setBatchDecisionReceipt"], setConflictNote: UseLabelsIntentRecoveryScope["setConflictNote"], setDagsterDraftState: UseLabelsIntentRecoveryScope["setDagsterDraftState"], setDraftInputs: UseLabelsIntentRecoveryScope["setDraftInputs"], setExtractionState: UseLabelsIntentRecoveryScope["setExtractionState"], setLabelAggregates: UseLabelsIntentRecoveryScope["setLabelAggregates"], setLabelFactReadError: UseLabelsIntentRecoveryScope["setLabelFactReadError"], setLabelFactReadState: UseLabelsIntentRecoveryScope["setLabelFactReadState"], setLabelObservations: UseLabelsIntentRecoveryScope["setLabelObservations"], setOptimizationInputs: UseLabelsIntentRecoveryScope["setOptimizationInputs"], setPromptInputs: UseLabelsIntentRecoveryScope["setPromptInputs"], setPromptVariant: UseLabelsIntentRecoveryScope["setPromptVariant"], setReleaseChecks: UseLabelsIntentRecoveryScope["setReleaseChecks"], setReleaseDecision: UseLabelsIntentRecoveryScope["setReleaseDecision"], setReleaseInputs: UseLabelsIntentRecoveryScope["setReleaseInputs"], setReviewInputs: UseLabelsIntentRecoveryScope["setReviewInputs"], setSelectedCandidateId: UseLabelsIntentRecoveryScope["setSelectedCandidateId"], setSelectedCandidateIds: UseLabelsIntentRecoveryScope["setSelectedCandidateIds"], setSelectedPromptField: UseLabelsIntentRecoveryScope["setSelectedPromptField"]) {
  useEffect(() => {
      const nextTagName = draftMatch?.tag ?? activeIntent.intent;
      setDraftInputs({
        tagName: nextTagName,
        definition: `${nextTagName} 用于识别「${activeIntent.stage}」中的关键业务意图，并保持与 ASR、单据事件、质检标签和资产版本的可追踪关系。`,
        trigger: "命中证据句、同一客户组上下文和当前项目标签版本；低置信或冲突时只写候选结果。",
        conflict: activeIntent.conflicts[0]?.detail ?? "无强冲突；仍需要保留人工确认和回滚记录。",
        positive: draftMatch?.evidence ?? activeIntent.evidence,
        negative: activeIntent.blockers[0] ?? "语义不足或缺少证据引用"
      });
      setReviewInputs({
        assignee: activeIntent.owner,
        note: `${activeIntent.intent} 需核对证据句、单据字段和标签层级，确认后写入 v1.9.0-rc2 候选版本。`
      });
      setConflictNote(activeIntent.conflicts[0]?.detail ?? "低风险抽检通过后可进入灰度观察。");
      setReleaseInputs({
        note: `${activeIntent.intent} 候选标签版本发布说明：只影响当前项目的事件标签资产和评测样本资产。`,
        traffic: activeIntent.risk === "高" ? "10" : "25",
        approver: activeIntent.owner,
        rollback: "v1.8.4",
        blockerReason: activeIntent.blockers[0] ?? "无强阻断，保留发布前抽检。",
        action: activeIntent.risk === "高" ? "灰度观察" : "发布候选"
      });
      setReleaseChecks({
        "影响资产已确认": true,
        "Human Loop 已处理": activeIntent.risk !== "高",
        "回滚路径已确认": true
      });
      setSelectedCandidateId(`LC-${activeIntent.key}-01`);
      setSelectedCandidateIds([]);
      setBatchDecisionReceipt(null);
      setExtractionState("idle");
      setLabelFactReadState("idle");
      setLabelFactReadError("");
      setLabelObservations([]);
      setLabelAggregates([]);
      setSelectedCandidateIds([]);
      setBatchDecisionReceipt(null);
      setPromptVariant("candidate");
      setSelectedPromptField("definition");
      setReleaseDecision(activeIntent.risk === "高" ? "灰度观察" : "发布候选");
      setPromptInputs({
        system: "你是汽车销售质检标签抽取器。只能基于证据句、同一会话上下文、业务单据和当前标签版本输出候选标签；不得直接覆盖线上标签。",
        definition: `${nextTagName} 用于识别「${activeIntent.stage}」中的业务意图，必须同时给出标签域、标签组、标签、标签值/动作四层归属。`,
        positive: draftMatch?.evidence ?? activeIntent.evidence,
        negative: activeIntent.blockers[0] ?? "仅介绍车型、仅复述指导价、缺少客户上下文或被串音污染时不能判定为有效标签。",
        schema: `{"label_domain":"汽车销售质检","label_group":"${activeIntent.scene}","label":"${nextTagName}","value_or_action":"候选版本","evidence_span":"string","confidence":0.0,"conflict_reason":"string","trace_id":"string"}`,
        conflict: activeIntent.conflicts[0]?.detail ?? "未命中冲突时仍要输出 source_doc_id、prompt_version、model_version 供发布门禁追踪。",
        postprocess: "confidence < 0.78、金额字段冲突、Human Loop 未完成或影响资产未确认时，只写 LabelCandidate，进入 badcase/人审，不写线上标签。"
      });
      setOptimizationInputs((current) => ({
        ...current,
        dataRange: `${activeScenario.source} / ${activeIntent.scope}`,
        targetTag: `汽车销售质检 / ${activeIntent.scene} / ${nextTagName} / ${activeIntent.risk === "高" ? "转人工复核" : "候选接受"}`,
        sampleSet: `${activeScenario.output} · ${activeIntent.confidence + 160} 样本`,
        candidateTagVersion: "v1.9.0-rc2",
        modelVersion: activeIntent.key === "crosstalk" ? "audio-boundary-tagger-2026.06" : "tagger-llm-2026.06",
        promptVersion: activeIntent.key === "crosstalk" ? "prompt_crosstalk_guard_v07_rc1" : "prompt_quote_guard_v19_rc2",
        threshold: activeIntent.risk === "高" ? "0.82" : "0.78",
        strategy: activeIntent.risk === "高" ? "证据优先 + 冲突不覆盖 + 人审门禁" : "证据优先 + 低风险批量接受",
        partitionKey: `2025-05-26|aurora-center|${activeIntent.key}`,
        runTags: `prompt_version=${activeIntent.key === "crosstalk" ? "prompt_crosstalk_guard_v07_rc1" : "prompt_quote_guard_v19_rc2"}, model_version=${activeIntent.key === "crosstalk" ? "audio-boundary-tagger-2026.06" : "tagger-llm-2026.06"}, tag_version=v1.9.0-rc2`,
        runConfig: `{"ops":{"extract_label_candidates":{"config":{"threshold":${activeIntent.risk === "高" ? 0.82 : 0.78},"shadow_only":true,"strategy":"${activeIntent.risk === "高" ? "human_gate" : "low_risk_batch"}"}}}}`
      }));
      setAutomationLevel(activeIntent.risk === "高" ? "L2" : "L3");
      setDagsterDraftState("未生成");
    }, [activeIntentKey, activeScenario.output, activeScenario.source, draftMatch?.evidence, draftMatch?.tag, activeIntent.intent, activeIntent.stage, activeIntent.evidence, activeIntent.risk, activeIntent.owner, activeIntent.conflicts, activeIntent.blockers, activeIntent.scope, activeIntent.scene, activeIntent.confidence, activeIntent.key]);

  return {

  };
}

export type LabelsIntentRecovery = ReturnType<typeof useLabelsIntentRecovery>;
