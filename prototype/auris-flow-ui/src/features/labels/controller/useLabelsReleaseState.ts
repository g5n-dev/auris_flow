import type { LabelsModuleProps } from "../types";
import type { LabelsCoreState } from "./useLabelsCoreState";
import type { BackendActionReceipt, LabelVersionEvaluationLock } from "../../../api/client";
import type { PromptFieldKey } from "../../../shared/contracts/prompts";
import type { AutomationLevelKey, DagsterDraftState, LabelChangeSource, LabelEvalIntent, LabelEvalRequestState, LabelOptimizationInputs, LabelPublishIntent, LabelPublishRequestState, PromptVariant } from "../types";
import { useRef, useState } from "react";

export function useLabelsReleaseState() {
  const [labelAgentBackendRun, setLabelAgentBackendRun] = useState<BackendActionReceipt | null>(null);

  const [labelExtractionBackendRun, setLabelExtractionBackendRun] = useState<BackendActionReceipt | null>(null);

  const [promptVariant, setPromptVariant] = useState<PromptVariant>("candidate");

  const [selectedPromptField, setSelectedPromptField] = useState<PromptFieldKey>("definition");

  const [promptInputs, setPromptInputs] = useState<Record<PromptFieldKey, string>>({
      system: "你是汽车销售质检标签抽取器，只根据证据句、上下文和业务单据输出候选标签。",
      definition: "识别销售是否形成可被业务复核的报价承诺，并保留证据句、金额字段和来源单据。",
      positive: "如果现在下单，可以优惠 3.5 万，落地大概 28.19 万左右。",
      negative: "官方指导价是 31.69 万，仅介绍车型价格，不代表成交承诺。",
      schema: "{\"label_code\":\"quote_commitment\",\"evidence_span\":\"string\",\"confidence\":0.0,\"trace_id\":\"string\"}",
      conflict: "当 ASR 金额、报价单金额和试驾单上下文字段冲突时，只写 LabelCandidate 并进入 Human Loop。",
      postprocess: "confidence < 0.78、串音污染或缺少单据引用时，不写线上标签。"
    });

  const [releaseDecision, setReleaseDecision] = useState("灰度观察");

  const [automationLevel, setAutomationLevel] = useState<AutomationLevelKey>("L2");

  const [dagsterDraftState, setDagsterDraftState] = useState<DagsterDraftState>("未生成");

  const [backendLabelVersionId, setBackendLabelVersionId] = useState("");

  const [labelRootTraceId, setLabelRootTraceId] = useState("");

  const [backendPromptCandidateId, setBackendPromptCandidateId] = useState("");

  const [backendPromptVersionId, setBackendPromptVersionId] = useState("");

  const [backendLabelBadcaseIds, setBackendLabelBadcaseIds] = useState<string[]>([]);

  const [backendReleaseDeploymentId, setBackendReleaseDeploymentId] = useState("");

  const [backendReleaseDeployment, setBackendReleaseDeployment] = useState<BackendActionReceipt | null>(null);

  const [labelEvaluationLock, setLabelEvaluationLock] = useState<LabelVersionEvaluationLock | null>(null);

  const [labelEvalRun, setLabelEvalRun] = useState<BackendActionReceipt | null>(null);

  const [labelEvalRequest, setLabelEvalRequest] = useState<LabelEvalRequestState>({ status: "idle" });

  const labelEvalPreflightRef = useRef(false);

  const labelEvalInFlightRef = useRef(false);

  const labelEvalPollGenerationRef = useRef(0);

  const lastLabelEvalIntentRef = useRef<LabelEvalIntent | null>(null);

  const [labelPublishRequest, setLabelPublishRequest] = useState<LabelPublishRequestState>({ status: "idle" });

  const labelPublishInFlightRef = useRef(false);

  const labelPublishPollGenerationRef = useRef(0);

  const lastLabelPublishIntentRef = useRef<LabelPublishIntent | null>(null);

  const resetLabelEvalState = () => {
      labelEvalPollGenerationRef.current += 1;
      labelEvalPreflightRef.current = false;
      labelEvalInFlightRef.current = false;
      lastLabelEvalIntentRef.current = null;
      setLabelEvaluationLock(null);
      setLabelEvalRun(null);
      setLabelEvalRequest({ status: "idle" });
    };

  const labelBadcaseActionHint = "写入 labeling badcase，并绑定当前候选、标签版本、Prompt 与证据 Trace。";

  const [selectedChangeSource, setSelectedChangeSource] = useState<"全部" | LabelChangeSource>("全部");

  const [humanChangeDraft, setHumanChangeDraft] = useState({
      after: "金额冲突进入人工确认，不自动覆盖报价承诺标签",
      reason: "ASR 报价金额与报价单字段不一致，先保留候选版本并送 Human Loop。",
      overrideAgent: "否"
    });

  const [optimizationInputs, setOptimizationInputs] = useState<LabelOptimizationInputs>({
      dataRange: "2025-05-26 / 极光中心店 / 12:23-12:33",
      targetTag: "汽车销售质检 / 报价与成交 / 报价承诺 / 候选接受",
      sampleSet: "quote-risk-v3 · 248 样本",
      currentTagVersion: "v1.8.4",
      candidateTagVersion: "v1.9.0-rc2",
      modelVersion: "tagger-llm-2026.06",
      promptAssetId: "prompt_asset_quote_guard",
      promptVersion: "prompt_quote_guard_v19_rc2",
      aggregationPolicyVersion: "label-aggregation-v1.9.0-rc2",
      evalDatasetVersion: "evalset_quote_risk_v12",
      threshold: "0.78",
      strategy: "证据优先 + 冲突不覆盖",
      shadowOnly: true,
      autoAcceptLowRisk: false,
      jobName: "label_optimization_canvas_v3_job",
      assetSelection: "auris/label/candidates, auris/eval/label_quality, auris/human/review_queue",
      partitionKey: "2025-05-26|aurora-center|quote-risk",
      runTags: "prompt_version=prompt_quote_guard_v19_rc2, model_version=tagger-llm-2026.06, tag_version=v1.9.0-rc2",
      runConfig: "{\"ops\":{\"extract_label_candidates\":{\"config\":{\"threshold\":0.78,\"shadow_only\":true}}}}"
    });

  return {
    labelAgentBackendRun,
    setLabelAgentBackendRun,
    labelExtractionBackendRun,
    setLabelExtractionBackendRun,
    promptVariant,
    setPromptVariant,
    selectedPromptField,
    setSelectedPromptField,
    promptInputs,
    setPromptInputs,
    releaseDecision,
    setReleaseDecision,
    automationLevel,
    setAutomationLevel,
    dagsterDraftState,
    setDagsterDraftState,
    backendLabelVersionId,
    setBackendLabelVersionId,
    labelRootTraceId,
    setLabelRootTraceId,
    backendPromptCandidateId,
    setBackendPromptCandidateId,
    backendPromptVersionId,
    setBackendPromptVersionId,
    backendLabelBadcaseIds,
    setBackendLabelBadcaseIds,
    backendReleaseDeploymentId,
    setBackendReleaseDeploymentId,
    backendReleaseDeployment,
    setBackendReleaseDeployment,
    labelEvaluationLock,
    setLabelEvaluationLock,
    labelEvalRun,
    setLabelEvalRun,
    labelEvalRequest,
    setLabelEvalRequest,
    labelEvalPreflightRef,
    labelEvalInFlightRef,
    labelEvalPollGenerationRef,
    lastLabelEvalIntentRef,
    labelPublishRequest,
    setLabelPublishRequest,
    labelPublishInFlightRef,
    labelPublishPollGenerationRef,
    lastLabelPublishIntentRef,
    resetLabelEvalState,
    labelBadcaseActionHint,
    selectedChangeSource,
    setSelectedChangeSource,
    humanChangeDraft,
    setHumanChangeDraft,
    optimizationInputs,
    setOptimizationInputs
  };
}

export type LabelsReleaseState = ReturnType<typeof useLabelsReleaseState>;
