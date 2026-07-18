import type { AuthUser } from "../../shared/contracts/auth";
import type { EvaluationCapabilityKey } from "../../shared/contracts/evaluation";
import type { ModuleDeepLink, ModuleKey } from "../../shared/contracts/navigation";
import type { PromptFieldKey } from "../../shared/contracts/prompts";

export type EvaluationModuleProps = {
  activeTab: string;
  setActiveTab: (tab: string) => void;
  setActiveModule: (module: ModuleKey) => void;
  navigateToTarget: (target: ModuleDeepLink) => void;
  focus: ModuleDeepLink | null;
  currentUser: AuthUser;
};

export type EvaluationViewKey = "auto" | "labeling" | "prompt" | "manual" | "sets" | "compare" | "badcase";

export type EvaluationRunRecord = {
  time: string;
  title: string;
  detail: string;
  status: "完成" | "运行中" | "待确认";
};

export type EvaluationManualReviewItem = {
  id: string;
  capability: EvaluationCapabilityKey;
  queue: string;
  title: string;
  evidenceWindow: string;
  asr: string;
  candidateLabel: string;
  failureReason: string;
  confidence: number;
  owner: string;
  status: "待人工" | "已接受" | "已修改" | "已驳回" | "已转 badcase";
};

export type EvaluationDatasetProfile = {
  id: string;
  name: string;
  version: string;
  scope: string;
  size: number;
  positive: number;
  negative: number;
  source: string;
  coverage: string;
  gap: string;
  owner: string;
  status: string;
};

export type EvaluationModelCompareRow = {
  key: EvaluationCapabilityKey;
  ability: string;
  prod4: string;
  prod5: string;
  candidate: string;
  diff: string;
  winner: string;
  risk: string;
  evidence: string;
};

export type EvaluationBadcaseWorkflowItem = {
  id: string;
  capability: EvaluationCapabilityKey;
  title: string;
  severity: string;
  status: "待归因" | "待人审" | "待回流" | "已入回归";
  source: string;
  rootCause: string;
  fix: string;
  target: string;
  owner: string;
  standardTerm?: string;
  recognizedText?: string;
  errorType?: "missing_term" | "misrecognition" | "alias_gap" | "weight_issue" | "false_boost";
  expectedCount?: number;
  errorRate?: number;
  evidenceLevel?: string;
  downstreamImpact?: string;
  priority?: number;
  resourceVersion?: number;
  rootTraceId?: string;
};

export type EvaluationLabelingMetric = {
  taskKey: string;
  task: string;
  owner: string;
  onlineVersion: string;
  candidateVersion: string;
  samples: number;
  precision: number;
  recall: number;
  f1: number;
  humanAgreement: number;
  conflictRate: number;
  lowConfidenceRate: number;
  badcases: number;
  promptVersion: string;
};

export type EvaluationLabelingCase = {
  id: string;
  taskKey: string;
  label: string;
  evidenceWindow: string;
  asr: string;
  expected: string;
  predicted: string;
  issue: "误打" | "漏打" | "冲突" | "需人审";
  confidence: number;
  source: string;
  route: ModuleKey;
};

export type EvaluationPromptExperiment = {
  id: string;
  labelTask: string;
  currentVersion: string;
  candidateVersion: string;
  dataset: string;
  status: "待生成建议" | "已有建议" | "候选已创建" | "影子评测通过" | "发布草稿";
  currentF1: number;
  candidateF1: number;
  conflictRate: number;
  humanAgreement: number;
  badcaseRegression: number;
};

export type EvaluationPromptSuggestion = {
  id: string;
  field: PromptFieldKey;
  title: string;
  detail: string;
  example: string;
  impact: string;
};
