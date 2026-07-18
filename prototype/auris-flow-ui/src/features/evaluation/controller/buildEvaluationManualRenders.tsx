import type { EvaluationModuleProps } from "../types";
import type { EvaluationState } from "./useEvaluationState";
import type { EvaluationSelection } from "./buildEvaluationSelection";
import type { EvaluationFocusRecovery } from "./useEvaluationFocusRecovery";
import type { EvaluationContextActions } from "./buildEvaluationContextActions";
import type { HotwordPollingActions } from "./buildHotwordPollingActions";
import type { HotwordVersionRecovery } from "./useHotwordVersionRecovery";
import type { EvaluationRunActions } from "./buildEvaluationRunActions";
import type { EvaluationBadcaseActions } from "./buildEvaluationBadcaseActions";
import type { HotwordGateModel } from "./buildHotwordGateModel";
import type { HotwordReleaseActions } from "./buildHotwordReleaseActions";
import type { EvaluationLabelPromptActions } from "./buildEvaluationLabelPromptActions";
import type { EvaluationPrimaryRenders } from "./buildEvaluationPrimaryRenders";
import type { ModuleKey } from "../../../shared/contracts/navigation";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { evaluationDatasets } from "../catalog";
import { CalibrationWorkspace } from "../components/CalibrationWorkspaceLazy";
import type { EvaluationManualReviewItem } from "../types";
import { BarChart3, Database, Headphones, Plus, ShieldCheck, SlidersHorizontal, Sparkles, UserCheck } from "lucide-react";
import { Suspense } from "react";

type BuildEvaluationManualRendersScope = EvaluationModuleProps & EvaluationState & EvaluationSelection & EvaluationFocusRecovery & EvaluationContextActions & HotwordPollingActions & HotwordVersionRecovery & EvaluationRunActions & EvaluationBadcaseActions & HotwordGateModel & HotwordReleaseActions & EvaluationLabelPromptActions & EvaluationPrimaryRenders;

export function buildEvaluationManualRenders(currentUser: BuildEvaluationManualRendersScope["currentUser"], datasetDraft: BuildEvaluationManualRendersScope["datasetDraft"], decideManualReview: BuildEvaluationManualRendersScope["decideManualReview"], feedbackDraft: BuildEvaluationManualRendersScope["feedbackDraft"], manualMode: BuildEvaluationManualRendersScope["manualMode"], manualReviews: BuildEvaluationManualRendersScope["manualReviews"], navigateToTarget: BuildEvaluationManualRendersScope["navigateToTarget"], openEvaluationCaseEvidence: BuildEvaluationManualRendersScope["openEvaluationCaseEvidence"], saveDatasetDraft: BuildEvaluationManualRendersScope["saveDatasetDraft"], selectedDataset: BuildEvaluationManualRendersScope["selectedDataset"], selectedManualReview: BuildEvaluationManualRendersScope["selectedManualReview"], setActiveModule: BuildEvaluationManualRendersScope["setActiveModule"], setDatasetDraft: BuildEvaluationManualRendersScope["setDatasetDraft"], setManualMode: BuildEvaluationManualRendersScope["setManualMode"], setSelectedDatasetId: BuildEvaluationManualRendersScope["setSelectedDatasetId"], setSelectedManualId: BuildEvaluationManualRendersScope["setSelectedManualId"]) {
  const renderStandardManualView = () => (
      <>
        <section className="module-panel evaluation-manual-queue">
          <PanelHeader title="Human Review 队列" subtitle="按失败类型筛选当前待处理样本" icon={<UserCheck size={16} />} />
          <div className="evaluation-review-list">
            {manualReviews.map((item) => (
              <button key={item.id} type="button" className={item.id === selectedManualReview.id ? "active" : ""} onClick={() => setSelectedManualId(item.id)}>
                <span>{item.queue}</span>
                <strong>{item.title}</strong>
                <em>{item.evidenceWindow}</em>
                <b>{item.status}</b>
              </button>
            ))}
          </div>
        </section>
        <section className="module-panel wide evaluation-review-detail">
          <PanelHeader title={selectedManualReview.title} subtitle={`${selectedManualReview.queue} / ${selectedManualReview.evidenceWindow}`} icon={<Headphones size={16} />} />
          <div className="evaluation-review-body">
            <div className="evaluation-evidence-card">
              <span>证据片段</span>
              <strong>{selectedManualReview.evidenceWindow}</strong>
              <p>{selectedManualReview.asr}</p>
              <button type="button" onClick={() => openEvaluationCaseEvidence()}>进入调听证据</button>
            </div>
            <div className="evaluation-review-fields">
              <div><span>候选标签</span><strong>{selectedManualReview.candidateLabel}</strong></div>
              <div><span>失败原因</span><strong>{selectedManualReview.failureReason}</strong></div>
              <div><span>置信度</span><strong>{selectedManualReview.confidence}%</strong></div>
              <div><span>负责人</span><strong>{selectedManualReview.owner}</strong></div>
            </div>
          </div>
        </section>
        <section className="module-panel evaluation-review-actions-panel">
          <PanelHeader title="人审决策" subtitle="操作会写入 Human Loop 和评测回流草稿" icon={<ShieldCheck size={16} />} />
          <div className="evaluation-decision-grid">
            {[
              ["已接受", "接受候选"],
              ["已修改", "修改后接受"],
              ["已驳回", "驳回"],
              ["已转 badcase", "转 badcase"]
            ].map(([status, label]) => (
              <button key={status} type="button" className={selectedManualReview.status === status ? "active" : ""} onClick={() => decideManualReview(status as EvaluationManualReviewItem["status"])}>
                {label}
              </button>
            ))}
          </div>
          <div className="evaluation-feedback-status">
            <Sparkles size={15} />
            <span>{feedbackDraft}</span>
          </div>
        </section>
      </>
    );

  const renderManualView = () => (
      <>
        <section className="module-panel wide evaluation-manual-mode-bar">
          <div>
            <strong>{manualMode === "review" ? "常规人审" : "盲审校准"}</strong>
            <span>
              {manualMode === "review"
                ? "处理单条低置信、冲突和 badcase 回流。"
                : "A/B 独立评审、第三方仲裁与不可变金标发布。"}
            </span>
          </div>
          <div className="evaluation-manual-mode-switch" role="group" aria-label="人审工作模式">
            <button type="button" className={manualMode === "review" ? "active" : ""} onClick={() => setManualMode("review")}>常规人审</button>
            <button type="button" className={manualMode === "calibration" ? "active" : ""} onClick={() => setManualMode("calibration")}>盲审校准</button>
          </div>
        </section>
        {manualMode === "calibration" ? (
          <Suspense fallback={<section className="module-panel wide calibration-loading">正在加载盲审校准工作台...</section>}>
            <CalibrationWorkspace
              currentUser={{
                id: currentUser.userId,
                role: currentUser.role,
                name: currentUser.name
              }}
              onOpenEvidence={(evidenceRef, sourceCaseId) => navigateToTarget({
                module: "listening",
                objectKind: "evaluationCase",
                objectId: sourceCaseId,
                focusMode: "evidence",
                title: "盲审校准证据",
                detail: evidenceRef,
                origin: { label: "评测中心 / 盲审校准", module: "evaluation", objectLabel: sourceCaseId }
              })}
            />
          </Suspense>
        ) : renderStandardManualView()}
      </>
    );

  const renderSetsView = () => (
      <>
        <section className="module-panel wide evaluation-set-builder">
          <PanelHeader title="评测集构建器" subtitle="管理版本、来源、分层覆盖和补样缺口" icon={<Database size={16} />} />
          <div className="evaluation-dataset-cards">
            {evaluationDatasets.map((dataset) => (
              <button key={dataset.id} type="button" className={dataset.id === selectedDataset.id ? "active" : ""} onClick={() => setSelectedDatasetId(dataset.id)}>
                <span>{dataset.version}</span>
                <strong>{dataset.name}</strong>
                <em>{dataset.scope}</em>
                <b>{dataset.status}</b>
              </button>
            ))}
          </div>
          <div className="evaluation-set-layout">
            <section className="evaluation-coverage-panel">
              <div className="evaluation-section-title">
                <BarChart3 size={15} />
                <span>分层覆盖</span>
              </div>
              {[
                ["总样本", `${selectedDataset.size}`, selectedDataset.coverage],
                ["正样本", `${selectedDataset.positive}`, "命中目标能力"],
                ["负样本", `${selectedDataset.negative}`, "排除边界和反例"],
                ["缺口", selectedDataset.gap, selectedDataset.source]
              ].map(([label, value, detail]) => (
                <div key={label} className="evaluation-coverage-row">
                  <span>{label}</span>
                  <strong>{value}</strong>
                  <em>{detail}</em>
                </div>
              ))}
            </section>
            <section className="evaluation-dataset-form">
              <div className="evaluation-section-title">
                <SlidersHorizontal size={15} />
                <span>保存构建草稿</span>
              </div>
              <label>
                <span>目标样本数</span>
                <input value={datasetDraft.targetSize} onChange={(event) => setDatasetDraft((current) => ({ ...current, targetSize: event.target.value }))} />
              </label>
              <label>
                <span>负责人</span>
                <input value={datasetDraft.owner} onChange={(event) => setDatasetDraft((current) => ({ ...current, owner: event.target.value }))} />
              </label>
              <label>
                <span>分层策略</span>
                <input value={datasetDraft.layer} onChange={(event) => setDatasetDraft((current) => ({ ...current, layer: event.target.value }))} />
              </label>
              <label>
                <span>补样说明</span>
                <textarea rows={3} value={datasetDraft.note} onChange={(event) => setDatasetDraft((current) => ({ ...current, note: event.target.value }))} />
              </label>
              <button type="button" className="evaluation-primary-action" onClick={saveDatasetDraft}>保存评测集草稿</button>
            </section>
          </div>
        </section>
        <section className="module-panel evaluation-source-panel">
          <PanelHeader title="添加来源" subtitle="来源只进入当前评测集草稿" icon={<Plus size={16} />} />
          <div className="evaluation-source-list">
            {[
              ["调听证据", "从当前会话边界、ASR 和标签轨道加入样本", "listening"],
              ["badcase", "把失败样本加入回归集", "evaluation"],
              ["资产回填", "从资产质量失败和回填结果补样", "assets"],
              ["标签冲突", "从标签治理冲突样本加入正反例", "labels"]
            ].map(([label, detail, route]) => (
              <button key={label} type="button" onClick={() => setActiveModule(route as ModuleKey)}>
                <strong>{label}</strong>
                <span>{detail}</span>
              </button>
            ))}
          </div>
        </section>
      </>
    );

  return {
    renderStandardManualView,
    renderManualView,
    renderSetsView
  };
}

export type EvaluationManualRenders = ReturnType<typeof buildEvaluationManualRenders>;
