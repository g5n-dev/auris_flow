import type { LabelsModuleProps } from "../types";
import type { LabelsCoreState } from "./useLabelsCoreState";
import type { LabelsReleaseState } from "./useLabelsReleaseState";
import type { LabelsCandidateModel } from "./buildLabelsCandidateModel";
import type { LabelsFocusModel } from "./useLabelsFocus";
import type { LabelsChangeModel } from "./buildLabelsChangeModel";
import type { LabelsGovernanceModel } from "./buildLabelsGovernanceModel";
import type { LabelsConflictModel } from "./buildLabelsConflictModel";
import type { LabelsIntentRecovery } from "./useLabelsIntentRecovery";
import type { LabelsNavigationActions } from "./buildLabelsNavigationActions";
import type { LabelsOptimizationActions } from "./buildLabelsOptimizationActions";
import type { LabelsReviewActions } from "./buildLabelsReviewActions";
import type { LabelsPersistenceActions } from "./buildLabelsPersistenceActions";
import type { LabelsPromptActions } from "./buildLabelsPromptActions";
import type { LabelsEvaluationActions } from "./buildLabelsEvaluationActions";
import type { LabelsReleaseActions } from "./buildLabelsReleaseActions";
import { StackedFacts } from "../../../shared/ui/FactDisplays";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { Check, GitBranch, Headphones, Plus, Settings, Sparkles, UserCheck } from "lucide-react";

type BuildLabelsCoreRendersScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel & LabelsGovernanceModel & LabelsConflictModel & LabelsIntentRecovery & LabelsNavigationActions & LabelsOptimizationActions & LabelsReviewActions & LabelsPersistenceActions & LabelsPromptActions & LabelsEvaluationActions & LabelsReleaseActions;

export function buildLabelsCoreRenders(actionFeedback: BuildLabelsCoreRendersScope["actionFeedback"], activeAutomation: BuildLabelsCoreRendersScope["activeAutomation"], activeIntent: BuildLabelsCoreRendersScope["activeIntent"], activeReviewTask: BuildLabelsCoreRendersScope["activeReviewTask"], agentImprovementRows: BuildLabelsCoreRendersScope["agentImprovementRows"], applyReviewDecision: BuildLabelsCoreRendersScope["applyReviewDecision"], automationLevel: BuildLabelsCoreRendersScope["automationLevel"], createLabelDraft: BuildLabelsCoreRendersScope["createLabelDraft"], dagsterDraftState: BuildLabelsCoreRendersScope["dagsterDraftState"], editableDraftTagName: BuildLabelsCoreRendersScope["editableDraftTagName"], handleIntentAction: BuildLabelsCoreRendersScope["handleIntentAction"], hasAuthoritativeCandidate: BuildLabelsCoreRendersScope["hasAuthoritativeCandidate"], hasBoundReviewTask: BuildLabelsCoreRendersScope["hasBoundReviewTask"], humanChangeDraft: BuildLabelsCoreRendersScope["humanChangeDraft"], labelCandidates: BuildLabelsCoreRendersScope["labelCandidates"], labelEntityAction: BuildLabelsCoreRendersScope["labelEntityAction"], labelPublishPending: BuildLabelsCoreRendersScope["labelPublishPending"], labelPublishRequest: BuildLabelsCoreRendersScope["labelPublishRequest"], modifyLabelRule: BuildLabelsCoreRendersScope["modifyLabelRule"], openLabelEvidence: BuildLabelsCoreRendersScope["openLabelEvidence"], optimizationInputs: BuildLabelsCoreRendersScope["optimizationInputs"], recommendedAutomation: BuildLabelsCoreRendersScope["recommendedAutomation"], releaseCheckItems: BuildLabelsCoreRendersScope["releaseCheckItems"], releaseChecks: BuildLabelsCoreRendersScope["releaseChecks"], releaseInputs: BuildLabelsCoreRendersScope["releaseInputs"], reviewDecisionActions: BuildLabelsCoreRendersScope["reviewDecisionActions"], reviewInputs: BuildLabelsCoreRendersScope["reviewInputs"], reviewState: BuildLabelsCoreRendersScope["reviewState"], saveCandidateVersion: BuildLabelsCoreRendersScope["saveCandidateVersion"], selectedMetricRow: BuildLabelsCoreRendersScope["selectedMetricRow"], sendLabelHumanLoop: BuildLabelsCoreRendersScope["sendLabelHumanLoop"], setActionFeedback: BuildLabelsCoreRendersScope["setActionFeedback"], setActiveModule: BuildLabelsCoreRendersScope["setActiveModule"], setReleaseChecks: BuildLabelsCoreRendersScope["setReleaseChecks"], submitReleaseGate: BuildLabelsCoreRendersScope["submitReleaseGate"], updateReleaseInput: BuildLabelsCoreRendersScope["updateReleaseInput"], updateReviewInput: BuildLabelsCoreRendersScope["updateReviewInput"]) {
  const renderLabelDataActions = () => (
      <div className="label-data-actions" aria-label="标签数据创建修改入口">
        <button type="button" onClick={createLabelDraft}>
          <Plus size={14} />
          <span>新建候选标签</span>
          <strong>LabelDraft</strong>
          <em>写候选版本</em>
        </button>
        <button type="button" onClick={modifyLabelRule}>
          <Settings size={14} />
          <span>修改当前规则</span>
          <strong>RuleCandidate</strong>
          <em>保存正负例</em>
        </button>
        <button
          type="button"
          onClick={sendLabelHumanLoop}
          disabled={labelEntityAction !== null || !hasAuthoritativeCandidate || !hasBoundReviewTask}
          title={!hasAuthoritativeCandidate ? "请先运行抽取并读取后端 LabelAggregate" : !hasBoundReviewTask ? "当前 LabelAggregate 缺少 review_task_id" : undefined}
        >
          <UserCheck size={14} />
          <span>送 Human Loop</span>
          <strong>{reviewState}</strong>
          <em>人工接受/修改/拒绝</em>
        </button>
        <button type="button" className="primary" onClick={saveCandidateVersion}>
          <Check size={14} />
          <span>保存</span>
          <strong>v1.9.0-rc2</strong>
          <em>影子评测</em>
        </button>
      </div>
    );

  const saveReleaseConfig = () => {
      const missingChecks = releaseCheckItems.filter((item) => !releaseChecks[item]);
      setActionFeedback(
        missingChecks.length > 0
          ? `发布配置已保存，但仍有 ${missingChecks.join("、")} 未确认。`
          : `${editableDraftTagName} 发布配置已保存：${releaseInputs.traffic}% ${releaseInputs.action}，回滚到 ${releaseInputs.rollback}。`
      );
    };

  const renderReleaseGateEditor = () => (
      <div className="label-release-panel">
        <StackedFacts
          facts={[
            ["当前版本", "v1.8.4"],
            ["候选版本", "v1.9.0-rc2"],
            ["影响资产", activeIntent.scene],
            ["发布状态", reviewState === "已拒绝" ? "人工拒绝" : activeIntent.blockers.length > 0 ? "阻断待处理" : "可进入灰度"]
          ]}
        />
        <div className="label-release-form">
          <label className="wide">
            <span>发布说明</span>
            <textarea value={releaseInputs.note} onChange={(event) => updateReleaseInput("note", event.target.value)} rows={3} />
          </label>
          <label>
            <span>灰度比例</span>
            <input value={releaseInputs.traffic} onChange={(event) => updateReleaseInput("traffic", event.target.value)} inputMode="numeric" />
            <em>%</em>
          </label>
          <label>
            <span>审批人</span>
            <input value={releaseInputs.approver} onChange={(event) => updateReleaseInput("approver", event.target.value)} />
          </label>
          <label>
            <span>回滚部署 ID</span>
            <input value={releaseInputs.rollback} onChange={(event) => updateReleaseInput("rollback", event.target.value)} placeholder="release_prod_stable_001" />
          </label>
          <label>
            <span>发布动作</span>
            <select value={releaseInputs.action} onChange={(event) => updateReleaseInput("action", event.target.value)}>
              <option>灰度观察</option>
              <option>发布候选</option>
              <option>仅影子评测</option>
              <option>阻断发布</option>
            </select>
          </label>
          <label className="wide">
            <span>阻断原因 / 放行依据</span>
            <textarea value={releaseInputs.blockerReason} onChange={(event) => updateReleaseInput("blockerReason", event.target.value)} rows={2} />
          </label>
        </div>
        <div className="label-release-checks" aria-label="发布确认项">
          {releaseCheckItems.map((item) => (
            <label key={item}>
              <input type="checkbox" checked={releaseChecks[item]} onChange={(event) => setReleaseChecks((current) => ({ ...current, [item]: event.target.checked }))} />
              <span>{item}</span>
            </label>
          ))}
        </div>
        <div className="label-release-actions">
          <button type="button" onClick={() => handleIntentAction(`${editableDraftTagName} 已生成 Agent Trace，可交给人工仲裁。`)}>
            转人工仲裁
          </button>
          <button type="button" onClick={saveReleaseConfig}>
            保存发布配置
          </button>
          <button type="button" className="primary" onClick={submitReleaseGate} disabled={labelPublishPending}>
            {labelPublishPending && labelPublishRequest.action === "gate" ? "提交发布检查 · pending" : "提交发布检查"}
          </button>
        </div>
      </div>
    );

  const renderHumanLoopWorkbench = () => (
      <div className="label-human-workbench">
        <div className="label-human-current">
          <span>当前人工任务</span>
          <strong>{activeReviewTask.id} · {activeReviewTask.title}</strong>
          <p>{activeReviewTask.detail}</p>
        </div>
        <div className="label-human-route">
          {[
            ["触发来源", activeReviewTask.type],
            ["证据入口", "调听工作台 / 当前片段"],
            ["影响范围", "候选标签、评测样本、业务洞察"],
            ["回写结果", reviewState]
          ].map(([label, value]) => (
            <button key={label} type="button" onClick={() => setActiveModule(label === "证据入口" ? "listening" : label === "影响范围" ? "assets" : "labels")}>
              <span>{label}</span>
              <strong>{value}</strong>
            </button>
          ))}
        </div>
        <div className="label-review-form">
          <label>
            <span>处理人</span>
            <input value={reviewInputs.assignee} onChange={(event) => updateReviewInput("assignee", event.target.value)} />
          </label>
          <label>
            <span>人工处理说明</span>
            <textarea value={reviewInputs.note} onChange={(event) => updateReviewInput("note", event.target.value)} rows={3} />
          </label>
        </div>
        <div className="label-review-actions">
          {reviewDecisionActions.map((action) => (
            <button
              key={action.state}
              type="button"
              className={reviewState === action.state ? "active" : ""}
              onClick={() => applyReviewDecision(action.state, action.label, `${action.detail} 处理人：${reviewInputs.assignee}；说明：${reviewInputs.note}`)}
            >
              {action.label}
            </button>
          ))}
        </div>
        <button type="button" className="label-human-evidence-link" onClick={() => openLabelEvidence("Human Loop 当前任务")}>
          <Headphones size={14} />
          进入证据审查处理当前任务
        </button>
      </div>
    );

  const renderLabelClosedLoopStrip = () => (
      <section className="module-panel wide label-loop-panel">
        <PanelHeader title="标签优化闭环台" subtitle="输入范围 → Agent 提升 → 人工修改 → 效果评价 → 自动化等级 → 执行回写" icon={<GitBranch size={16} />} />
        <div className="label-loop-dashboard" aria-label="标签优化闭环阶段">
          {[
            {
              title: "输入",
              status: dagsterDraftState === "未生成" ? "待生成草稿" : "已同步",
              owner: "标签运营",
              blocker: optimizationInputs.dataRange,
              action: "编辑本轮输入"
            },
            {
              title: "Agent 提升",
              status: `${labelCandidates.filter((candidate) => candidate.source === "Agent建议").length} 条建议`,
              owner: "Tagger Agent",
              blocker: agentImprovementRows[0].uplift,
              action: "查看建议证据"
            },
            {
              title: "人工修改",
              status: reviewState,
              owner: reviewInputs.assignee,
              blocker: reviewState === "待人工" ? "高风险候选未确认" : humanChangeDraft.after,
              action: "打开 ChangeSet"
            },
            {
              title: "效果评价",
              status: selectedMetricRow[4],
              owner: "Eval Gate",
              blocker: `${selectedMetricRow[0]} ${selectedMetricRow[1]} → ${selectedMetricRow[2]}`,
              action: "对比指标"
            },
            {
              title: "自动化等级",
              status: `${automationLevel} · ${activeAutomation.name}`,
              owner: activeAutomation.owner,
              blocker: `推荐 ${recommendedAutomation.key} · ${recommendedAutomation.name}`,
              action: "调整等级"
            },
            {
              title: "执行回写",
              status: dagsterDraftState,
              owner: "执行队列",
              blocker: optimizationInputs.jobName,
              action: "生成/校验/回写"
            }
          ].map(({ title, status, owner, blocker, action }, index) => (
            <button
              key={title}
              type="button"
              className={`label-loop-stage ${index === 0 || index === 5 ? "active" : ""}`}
              onClick={() => setActionFeedback(`${title}：${status}。责任方 ${owner}；当前入口：${action}；阻断/说明：${blocker}`)}
            >
              <b>{index + 1}</b>
              <strong>{title}</strong>
              <span>{status}</span>
              <em>{owner}</em>
              <small>{blocker}</small>
            </button>
          ))}
        </div>
        <div className="label-loop-feedback" role="status">
          <Sparkles size={14} />
          <span>{actionFeedback}</span>
        </div>
      </section>
    );

  return {
    renderLabelDataActions,
    saveReleaseConfig,
    renderReleaseGateEditor,
    renderHumanLoopWorkbench,
    renderLabelClosedLoopStrip
  };
}

export type LabelsCoreRenders = ReturnType<typeof buildLabelsCoreRenders>;
