import type { LabelTrackKey } from "../../shared/fixtures/labelLayers";
import type { PromptFieldKey } from "../../shared/contracts/prompts";
import type { ModuleDeepLink, ModuleKey } from "../../shared/contracts/navigation";

export type LabelsModuleProps = {
  activeTab: string;
  setActiveModule: (module: ModuleKey) => void;
  navigateToTarget: (target: ModuleDeepLink) => void;
  focus: ModuleDeepLink | null;
};

export type LabelIntentKey = "quote" | "priceObjection" | "testDrive" | "dealIntent" | "crosstalk";

export type LabelScenarioKey = "salesQa" | "testDriveFlow" | "audioQuality" | "dealInsight";

export type LabelIntentFlow = {
  key: LabelIntentKey;
  intent: string;
  stage: string;
  scene: string;
  owner: string;
  status: string;
  risk: "高" | "中" | "低";
  confidence: number;
  evidence: string;
  scope: string;
  layers: Partial<Record<LabelTrackKey, { tag: string; state: string; evidence: string }>>;
  conflicts: Array<{ label: string; source: string; detail: string; severity: "高" | "中" | "低" }>;
  suggestions: string[];
  blockers: string[];
  trace: Array<[string, string]>;
};

export type LabelExtractionState = "idle" | "running" | "completed" | "failed";

export type LabelFactReadState = "idle" | "loading" | "ready" | "empty" | "failed";

export type LabelReviewState = "待人工" | "已接受" | "已修改" | "已拒绝";

export type PromptVariant = "current" | "candidate";

export type LabelChangeSource = "Agent建议" | "人工修改" | "系统门禁";

export type AutomationLevelKey = "L0" | "L1" | "L2" | "L3" | "L4";

export type DagsterDraftState = "未生成" | "草稿已生成" | "已校验" | "已回写";

export type LabelPublishAction = "gate" | "gray" | "candidate" | "execute";

export type LabelPublishRequestStatus = "idle" | "pending" | "success" | "blocked" | "failed";

export type LabelPublishIntent = {
  action: LabelPublishAction;
  actionLabel: string;
  idempotencyKey: string;
  deploymentId?: string;
};

export type LabelPublishRequestState = {
  status: LabelPublishRequestStatus;
  action?: LabelPublishAction;
  actionLabel?: string;
  backendStatus?: string;
  runId?: string;
  traceId?: string;
  error?: string;
};

export type LabelEvalRequestStatus = "idle" | "pending" | "success" | "failed";

export type LabelEvalIntent = {
  payload: Record<string, unknown>;
  idempotencyKey: string;
  retryIdempotencyKey: string;
  runId?: string;
};

export type LabelEvalRequestState = {
  status: LabelEvalRequestStatus;
  backendStatus?: string;
  runId?: string;
  traceId?: string;
  error?: string;
};

export type LabelOptimizationInputs = {
  dataRange: string;
  targetTag: string;
  sampleSet: string;
  currentTagVersion: string;
  candidateTagVersion: string;
  modelVersion: string;
  promptAssetId: string;
  promptVersion: string;
  aggregationPolicyVersion: string;
  evalDatasetVersion: string;
  threshold: string;
  strategy: string;
  shadowOnly: boolean;
  autoAcceptLowRisk: boolean;
  jobName: string;
  assetSelection: string;
  partitionKey: string;
  runTags: string;
  runConfig: string;
};

export type LabelOptimizationTextKey = Exclude<keyof LabelOptimizationInputs, "shadowOnly" | "autoAcceptLowRisk">;

export type MetricVerdict = "通过" | "观察" | "阻断";

export type EvaluationMetric = {
  id: string;
  metric: string;
  current: string;
  candidate: string;
  delta: string;
  verdict: MetricVerdict;
  source: string;
  attribution: string;
  gateImpact: string;
  sample: string;
  requiredAction: string;
  blocking: boolean;
};

export type ReleaseGateCheck = {
  id: string;
  label: string;
  status: MetricVerdict;
  sourceMetric: string;
  detail: string;
  requiredAction: string;
  blocking: boolean;
};

export type LabelOptimizationRun = {
  runId: string;
  taskName: string;
  traceId: string;
  input: LabelOptimizationInputs;
  changeSet: Array<{ source: LabelChangeSource; object: string; change: string; impact: string }>;
  metrics: EvaluationMetric[];
  gateChecks: ReleaseGateCheck[];
  decision: {
    label: string;
    tone: MetricVerdict;
    nextActions: string[];
  };
  automationLevel: AutomationLevelKey;
  dagsterStatus: DagsterDraftState;
  dagsterRunDraft: Array<[string, string]>;
};

export type LabelCandidate = {
  id: string;
  title: string;
  level: string;
  value: string;
  source: LabelChangeSource | "模型观察" | "聚合事实";
  evidence: string;
  promptVersion: string;
  modelVersion: string;
  confidence: number;
  conflict: string;
  humanState: string;
  assetImpact: string;
  traceId: string;
  action: string;
};

export type LabelReviewTaskView = {
  id: string;
  aggregateId: string;
  title: string;
  type: string;
  detail: string;
  priority: "高" | "中" | "低";
  labelId: string;
  policyVersionId: string;
};
