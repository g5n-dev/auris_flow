import type { EvaluationModuleProps } from "../types";
import type { EvaluationState } from "./useEvaluationState";
import type { EvaluationSelection } from "./buildEvaluationSelection";
import type { EvaluationFocusRecovery } from "./useEvaluationFocusRecovery";
import type { EvaluationRunRecord, EvaluationViewKey } from "../types";

type BuildEvaluationContextActionsScope = EvaluationModuleProps & EvaluationState & EvaluationSelection & EvaluationFocusRecovery;

export function buildEvaluationContextActions(badcaseDrafts: BuildEvaluationContextActionsScope["badcaseDrafts"], currentView: BuildEvaluationContextActionsScope["currentView"], manualReviews: BuildEvaluationContextActionsScope["manualReviews"], navigateToTarget: BuildEvaluationContextActionsScope["navigateToTarget"], runRecords: BuildEvaluationContextActionsScope["runRecords"], selectedBadcaseWorkflow: BuildEvaluationContextActionsScope["selectedBadcaseWorkflow"], selectedCapability: BuildEvaluationContextActionsScope["selectedCapability"], selectedCompareRow: BuildEvaluationContextActionsScope["selectedCompareRow"], selectedLabelingCase: BuildEvaluationContextActionsScope["selectedLabelingCase"], setRunRecords: BuildEvaluationContextActionsScope["setRunRecords"]) {
  const selectedBadcaseDraft = badcaseDrafts[selectedBadcaseWorkflow.id] ?? {
      rootCause: selectedBadcaseWorkflow.rootCause,
      fix: selectedBadcaseWorkflow.fix,
      target: selectedBadcaseWorkflow.target,
      owner: selectedBadcaseWorkflow.owner
    };

  const runRecordTuples: Array<[string, string, string]> = runRecords.map((record) => [
      record.time,
      record.title,
      `${record.detail} · ${record.status}`
    ]);

  const openEvaluationCaseEvidence = (item = selectedLabelingCase) => {
      navigateToTarget({
        module: "listening",
        objectKind: "evaluationCase",
        objectId: item.id,
        focusMode: item.label.includes("串音") ? "matrix" : "evidence",
        title: item.label,
        detail: `${item.issue} / ${item.evidenceWindow}`,
        origin: { label: "评测中心 / 打标样本", module: "evaluation", objectLabel: item.id }
      });
    };

  const openEvaluationBadcaseEvidence = (item = selectedBadcaseWorkflow) => {
      navigateToTarget({
        module: "listening",
        objectKind: "evaluationBadcase",
        objectId: item.id,
        focusMode: item.capability === "diarization" ? "matrix" : "evidence",
        title: item.title,
        detail: `${item.severity} / ${item.source}`,
        origin: { label: "评测中心 / badcase", module: "evaluation", objectLabel: item.id }
      });
    };

  const openEvaluationAssetLineage = (title = selectedCompareRow.ability) => {
      const assetKey =
        selectedCompareRow.key === "asr" || selectedCompareRow.key === "asr-hotword"
          ? "auris/model/asr_transcripts"
          : selectedCompareRow.key === "boundary" || selectedCompareRow.key === "diarization"
            ? "auris/audio/voice_segments"
            : selectedCompareRow.key === "tagging" || selectedCompareRow.key === "prompt"
              ? "auris/label/event_tags"
              : "auris/eval/quality_metrics";
      navigateToTarget({
        module: "assets",
        tab: "lineage",
        objectKind: "asset",
        objectId: assetKey,
        focusMode: "lineage",
        title,
        detail: `${selectedCompareRow.ability} / ${selectedCompareRow.evidence}`,
        origin: { label: "评测中心 / 模型对比", module: "evaluation", objectLabel: selectedCompareRow.key }
      });
    };

  const gateRows = [
      { label: "可发布", value: "ASR / 标签", detail: "无强阻断，进入发布候选", tone: "green" },
      { label: "观察", value: "边界 F1", detail: "候选需继续影子评测", tone: "amber" },
      { label: "需人工", value: `${manualReviews.filter((item) => item.status === "待人工").length} 条`, detail: "低置信、冲突和串音样本", tone: "violet" },
      { label: "阻断", value: selectedCapability.blocker === "观察" ? "1 项" : "0 项", detail: selectedCapability.evidence, tone: selectedCapability.blocker === "观察" ? "red" : "blue" }
    ];

  const tabNarrative: Record<EvaluationViewKey, { title: string; subtitle: string }> = {
      auto: {
        title: "自动化评测控制台",
        subtitle: "配置、运行和查看门禁。"
      },
      labeling: {
        title: "打标评测",
        subtitle: "评估标签版本、人审一致性和回流证据。"
      },
      prompt: {
        title: "Prompt 优化闭环",
        subtitle: "从 badcase 生成候选 Prompt 和发布草稿。"
      },
      manual: {
        title: "Human Review 队列",
        subtitle: "低置信、冲突、边界和串音样本进入人工决策，决策回写评测链路。"
      },
      sets: {
        title: "评测集构建",
        subtitle: "从证据、badcase 和冲突样本构建评测集。"
      },
      compare: {
        title: "候选模型对比台",
        subtitle: "同集对比模型，定位提升和发布风险。"
      },
      badcase: {
        title: "badcase 归因与回流板",
        subtitle: "按状态推进归因、人审、回流和回归集写入，形成模型闭环。"
      }
    };

  const narrative = tabNarrative[currentView];

  const pushRunRecord = (title: string, detail: string, status: EvaluationRunRecord["status"] = "完成") => {
      const time = new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
      setRunRecords((current) => [{ time, title, detail, status }, ...current].slice(0, 8));
    };

  const shortTrace = (trace?: string) => (trace ? trace.slice(0, 12) : "no-trace");

  return {
    selectedBadcaseDraft,
    runRecordTuples,
    openEvaluationCaseEvidence,
    openEvaluationBadcaseEvidence,
    openEvaluationAssetLineage,
    gateRows,
    tabNarrative,
    narrative,
    pushRunRecord,
    shortTrace
  };
}

export type EvaluationContextActions = ReturnType<typeof buildEvaluationContextActions>;
