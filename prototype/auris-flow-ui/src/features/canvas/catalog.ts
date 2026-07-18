import staticCatalog from "../../modules/staticCatalog";
import type { asrServiceProfileSource, audioNodeRuntimeParamsSource, executionStateMetaSource } from "./fixtures/audioRuntime";
import type { baseDagsterBindingsSource } from "./fixtures/dagsterBindings";
import type { experimentMetricContextSource, experimentMetricLineageSource, experimentMetricObservationsSource, experimentMetricSuggestionsSource, taskExperimentArmsSource, taskExperimentMetricsSource } from "./fixtures/experimentFixtures";
import type { canvasIntentsSource, loginRiskApiContractsSource } from "./fixtures/intentsMapping";
import type { loginRiskDagsterCompatibilitySource, loginRiskScenarioPoliciesSource, taskCanvasVariantsSource, taskFlowStagesSource, taskTypeBlueprintsSource } from "./fixtures/taskBlueprints";

const canvasCatalog = (staticCatalog as {
  canvasCatalog: {
    intents: typeof canvasIntentsSource;
    apiContracts: typeof loginRiskApiContractsSource;
    compatibility: typeof loginRiskDagsterCompatibilitySource;
    scenarioPolicies: typeof loginRiskScenarioPoliciesSource;
    taskTypeBlueprints: typeof taskTypeBlueprintsSource;
    flowStages: typeof taskFlowStagesSource;
    canvasVariants: typeof taskCanvasVariantsSource;
    experimentArms: typeof taskExperimentArmsSource;
    experimentMetrics: typeof taskExperimentMetricsSource;
    experimentContext: typeof experimentMetricContextSource;
    experimentSuggestions: typeof experimentMetricSuggestionsSource;
    experimentObservations: typeof experimentMetricObservationsSource;
    experimentLineage: typeof experimentMetricLineageSource;
    asrServiceProfile: typeof asrServiceProfileSource;
    audioNodeRuntimeParams: typeof audioNodeRuntimeParamsSource;
    executionStateMeta: typeof executionStateMetaSource;
    baseBindings: typeof baseDagsterBindingsSource;
  };
}).canvasCatalog;

export const canvasIntents = canvasCatalog.intents;
export const loginRiskApiContracts = canvasCatalog.apiContracts;
export const loginRiskDagsterCompatibility = canvasCatalog.compatibility;
export const loginRiskScenarioPolicies = canvasCatalog.scenarioPolicies;
export const taskTypeBlueprints = canvasCatalog.taskTypeBlueprints;
export const taskFlowStages = canvasCatalog.flowStages;
export const taskCanvasVariants = canvasCatalog.canvasVariants;
export const taskExperimentArms = canvasCatalog.experimentArms;
export const taskExperimentMetrics = canvasCatalog.experimentMetrics;
export const experimentMetricContext = canvasCatalog.experimentContext;
export const experimentMetricSuggestions = canvasCatalog.experimentSuggestions;
export const experimentMetricObservations = canvasCatalog.experimentObservations;
export const experimentMetricLineage = canvasCatalog.experimentLineage;
export const asrServiceProfile = canvasCatalog.asrServiceProfile;
export const audioNodeRuntimeParams = canvasCatalog.audioNodeRuntimeParams;
export const executionStateMeta = canvasCatalog.executionStateMeta;
export const baseDagsterBindings = canvasCatalog.baseBindings;

export const asrTaskBindingRows = [
  ["服务引用", asrServiceProfile.serviceId, "从设置 / 模型配置读取"],
  ["Provider 参数", "ali / volc / self_hosted / open_source", "任务版本只保存 provider_ref 和参数，不直接写裸 endpoint"],
  ["Pipeline 开关", "vad=true, diar=true, asr=true", "同一音频服务内部执行，UI 拆成逻辑产物便于质检"],
  ["输出资产", "voice_segments / speaker_turns / transcript_asset", "按资产分别物化，便于回填和血缘追踪"],
  ["对齐视图", "speaker_transcript_view", "ASR segment 与 speaker_turns overlap join"],
  ["失败策略", "阶段级失败不覆盖已确认资产", "ASR 失败可复用 VAD/Diar；Diar 低置信转人工复核"],
  ["词包版本", "hwpv-auto-sales-v1-8", "生产只允许 published；候选版本只能用于 shadow"]
];
