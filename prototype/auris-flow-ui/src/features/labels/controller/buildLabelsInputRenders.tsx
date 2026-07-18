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
import type { LabelsCoreRenders } from "./buildLabelsCoreRenders";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { labelAutomationLevels } from "../fixtures/governanceCatalog";
import type { LabelOptimizationTextKey } from "../types";
import { BarChart3, Check, Database, GitBranch, ListFilter, ShieldCheck, Sparkles, UserCheck, Workflow } from "lucide-react";

type BuildLabelsInputRendersScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel & LabelsGovernanceModel & LabelsConflictModel & LabelsIntentRecovery & LabelsNavigationActions & LabelsOptimizationActions & LabelsReviewActions & LabelsPersistenceActions & LabelsPromptActions & LabelsEvaluationActions & LabelsReleaseActions & LabelsCoreRenders;

export function buildLabelsInputRenders(actionFeedback: BuildLabelsInputRendersScope["actionFeedback"], activeAutomation: BuildLabelsInputRendersScope["activeAutomation"], agentImprovementRows: BuildLabelsInputRendersScope["agentImprovementRows"], applyAgentImprovement: BuildLabelsInputRendersScope["applyAgentImprovement"], applyHumanChangeDraft: BuildLabelsInputRendersScope["applyHumanChangeDraft"], automationLevel: BuildLabelsInputRendersScope["automationLevel"], dagsterDraftRows: BuildLabelsInputRendersScope["dagsterDraftRows"], dagsterDraftState: BuildLabelsInputRendersScope["dagsterDraftState"], effectAttributionRows: BuildLabelsInputRendersScope["effectAttributionRows"], focus: BuildLabelsInputRendersScope["focus"], gateIsBlocked: BuildLabelsInputRendersScope["gateIsBlocked"], generateOptimizationRunDraft: BuildLabelsInputRendersScope["generateOptimizationRunDraft"], humanChangeDraft: BuildLabelsInputRendersScope["humanChangeDraft"], humanChangeRows: BuildLabelsInputRendersScope["humanChangeRows"], labelOptimizationRun: BuildLabelsInputRendersScope["labelOptimizationRun"], lockedLabelVersionId: BuildLabelsInputRendersScope["lockedLabelVersionId"], materializeDagsterResult: BuildLabelsInputRendersScope["materializeDagsterResult"], optimizationInputs: BuildLabelsInputRendersScope["optimizationInputs"], recommendedAutomation: BuildLabelsInputRendersScope["recommendedAutomation"], selectAutomationLevel: BuildLabelsInputRendersScope["selectAutomationLevel"], selectedChangeSource: BuildLabelsInputRendersScope["selectedChangeSource"], selectedExperimentMetric: BuildLabelsInputRendersScope["selectedExperimentMetric"], setActionFeedback: BuildLabelsInputRendersScope["setActionFeedback"], setHumanChangeDraft: BuildLabelsInputRendersScope["setHumanChangeDraft"], setSelectedChangeSource: BuildLabelsInputRendersScope["setSelectedChangeSource"], setSelectedExperimentMetric: BuildLabelsInputRendersScope["setSelectedExperimentMetric"], toggleOptimizationInput: BuildLabelsInputRendersScope["toggleOptimizationInput"], updateOptimizationInput: BuildLabelsInputRendersScope["updateOptimizationInput"], validateDagsterDraft: BuildLabelsInputRendersScope["validateDagsterDraft"], visibleChangeSetRows: BuildLabelsInputRendersScope["visibleChangeSetRows"]) {
  const renderOptimizationInputPanel = () => (
      <section className="module-panel wide label-input-panel">
        <PanelHeader title="本轮输入" subtitle="输入会同步到 Agent 建议、评测配置和运行草稿" icon={<ListFilter size={16} />} />
        <div className="label-input-layout">
          <div className="label-input-required">
            {([
              ["dataRange", "数据范围"],
              ["targetTag", "标签目标"],
              ["sampleSet", "样本集"],
              ["currentTagVersion", "当前标签版本"],
              ["candidateTagVersion", "候选标签版本"],
              ["modelVersion", "模型版本"],
              ["promptAssetId", "Prompt 资产"],
              ["promptVersion", "Prompt 版本"],
              ["aggregationPolicyVersion", "聚合策略版本"],
              ["evalDatasetVersion", "锁定评测集版本"],
              ["threshold", "置信阈值"],
              ["strategy", "抽取策略"]
            ] satisfies Array<[LabelOptimizationTextKey, string]>).map(([key, label]) => (
              <label key={key} className={key === "dataRange" || key === "targetTag" || key === "strategy" ? "wide" : ""}>
                <span>{key === "candidateTagVersion" && !LABEL_DEMO_MODE ? `${label}（仅展示名）` : label}</span>
                <input
                  value={optimizationInputs[key]}
                  onChange={(event) => updateOptimizationInput(key, event.target.value)}
                  aria-describedby={key === "candidateTagVersion" && !LABEL_DEMO_MODE ? "label-version-strong-id" : undefined}
                />
              </label>
            ))}
            {!LABEL_DEMO_MODE ? <small id="label-version-strong-id">运行锁定 ID：{lockedLabelVersionId || "保存 LabelVersion 后由后端返回"}</small> : null}
            <div className="label-input-toggles">
              <label>
                <input type="checkbox" checked={optimizationInputs.shadowOnly} onChange={(event) => toggleOptimizationInput("shadowOnly", event.target.checked)} />
                <span>只影子运行，不写线上</span>
              </label>
              <label>
                <input type="checkbox" checked={optimizationInputs.autoAcceptLowRisk} onChange={(event) => toggleOptimizationInput("autoAcceptLowRisk", event.target.checked)} />
                <span>允许低风险候选自动接受</span>
              </label>
            </div>
          </div>
          <div className="label-input-dagster">
            {([
              ["jobName", "job_name"],
              ["assetSelection", "asset_selection"],
              ["partitionKey", "partition_key"],
              ["runTags", "run_tags"],
              ["runConfig", "run_config"]
            ] satisfies Array<[LabelOptimizationTextKey, string]>).map(([key, label]) => (
              <label key={key} className={key === "runConfig" || key === "assetSelection" || key === "runTags" ? "wide" : ""}>
                <span>{label}</span>
                {key === "runConfig" || key === "assetSelection" || key === "runTags" ? (
                  <textarea value={optimizationInputs[key]} onChange={(event) => updateOptimizationInput(key, event.target.value)} rows={key === "runConfig" ? 4 : 2} />
                ) : (
                  <input value={optimizationInputs[key]} onChange={(event) => updateOptimizationInput(key, event.target.value)} />
                )}
              </label>
            ))}
          </div>
        </div>
        <div className="label-input-actions">
          <button type="button" className="primary" onClick={generateOptimizationRunDraft}>
            <Sparkles size={14} />
            生成运行草稿
          </button>
          <button type="button" onClick={validateDagsterDraft} disabled={dagsterDraftState === "未生成"}>
            <ShieldCheck size={14} />
            校验执行映射
          </button>
          <button type="button" onClick={materializeDagsterResult} disabled={dagsterDraftState === "未生成"}>
            <Database size={14} />
            模拟回写结果
          </button>
          <span>状态：{dagsterDraftState}</span>
        </div>
      </section>
    );

  const renderAgentHumanChangePanel = () => (
      <section className="module-panel wide label-change-panel">
        <PanelHeader title="Agent 提升与人工更改" subtitle="每条变更必须标来源、证据、Trace、影响资产和人审状态" icon={<UserCheck size={16} />} />
        <div className="label-change-layout">
          <div className="label-agent-change-column">
            <h4>Agent 提升</h4>
            {agentImprovementRows.map((item) => (
              <button key={item.title} type="button" className="label-change-card agent" onClick={() => applyAgentImprovement(item.title)}>
                <span>Agent建议 · {item.trace}</span>
                <strong>{item.title}</strong>
                <p>{item.evidence}</p>
                <div>
                  <b>{item.confidence}%</b>
                  <em>{item.uplift}</em>
                  <small>风险 {item.risk}</small>
                </div>
                <i>{item.action}</i>
              </button>
            ))}
          </div>
          <div className="label-human-change-column">
            <h4>人工修改</h4>
            <label>
              <span>修改后</span>
              <input value={humanChangeDraft.after} onChange={(event) => setHumanChangeDraft((current) => ({ ...current, after: event.target.value }))} />
            </label>
            <label>
              <span>修改理由</span>
              <textarea value={humanChangeDraft.reason} onChange={(event) => setHumanChangeDraft((current) => ({ ...current, reason: event.target.value }))} rows={3} />
            </label>
            <label>
              <span>是否覆盖 Agent 建议</span>
              <select value={humanChangeDraft.overrideAgent} onChange={(event) => setHumanChangeDraft((current) => ({ ...current, overrideAgent: event.target.value }))}>
                <option>否</option>
                <option>是</option>
                <option>部分覆盖</option>
              </select>
            </label>
            {humanChangeRows.map((item) => (
              <div key={`${item.before}-${item.after}`} className="label-human-diff-card">
                <span>{item.owner} · 覆盖 Agent：{item.override}</span>
                <strong>{item.before} → {item.after}</strong>
                <p>{item.reason}</p>
              </div>
            ))}
            <button type="button" className="primary" onClick={applyHumanChangeDraft}>
              <Check size={14} />
              写入人工 ChangeSet
            </button>
          </div>
          <div className="label-changeset-column">
            <h4>ChangeSet 审计</h4>
            <div className="label-changeset-filters">
              {(["全部", "Agent建议", "人工修改", "系统门禁"] as const).map((source) => (
                <button key={source} type="button" className={selectedChangeSource === source ? "active" : ""} onClick={() => setSelectedChangeSource(source)}>
                  {source}
                </button>
              ))}
            </div>
            <div className="label-changeset-list">
              {visibleChangeSetRows.map((row) => (
                <button key={`${row.source}-${row.object}`} type="button" onClick={() => setActionFeedback(`${row.source} / ${row.object}：${row.change}`)}>
                  <span>{row.source}</span>
                  <strong>{row.object}</strong>
                  <em>{row.change}</em>
                  <small>{row.impact}</small>
                </button>
              ))}
            </div>
          </div>
        </div>
      </section>
    );

  const renderChangeEffectPanel = () => (
      <section className="module-panel wide label-effect-panel">
        <PanelHeader title="更改效果" subtitle="当前版本 vs 候选版本；每个指标都说明提升来源，低于门禁禁止发布" icon={<BarChart3 size={16} />} />
        <div className="label-effect-summary">
          <div className={gateIsBlocked ? "blocked" : "passed"}>
            <span>发布门禁</span>
            <strong>{gateIsBlocked ? "阻断发布" : "可进入灰度"}</strong>
            <p>{gateIsBlocked ? "只能继续优化、送 Human Loop 或保持影子运行。" : "评测与人审满足当前自动化等级要求。"}</p>
          </div>
          <div>
            <span>当前版本</span>
            <strong>{optimizationInputs.currentTagVersion}</strong>
            <p>线上标签，不被本轮 Agent 直接覆盖。</p>
          </div>
          <div>
            <span>候选版本</span>
            <strong>{optimizationInputs.candidateTagVersion}</strong>
            <p>{optimizationInputs.promptVersion} / {optimizationInputs.modelVersion}</p>
          </div>
        </div>
        <div className="label-effect-table">
          <div className="label-effect-row head">
            <span>指标</span>
            <span>当前</span>
            <span>候选</span>
            <span>变化</span>
            <span>提升来源</span>
            <span>结论</span>
          </div>
          {effectAttributionRows.map((row) => (
            <button key={row.metric} type="button" className={selectedExperimentMetric === row.metric ? "label-effect-row active" : "label-effect-row"} onClick={() => setSelectedExperimentMetric(row.metric)}>
              <strong>{row.metric}</strong>
              <span>{row.current}</span>
              <span>{row.candidate}</span>
              <em>{row.delta}</em>
              <b>{row.source}</b>
              <small>{row.verdict}</small>
            </button>
          ))}
        </div>
      </section>
    );

  const renderAutomationDagsterPanel = () => (
      <section className="module-panel wide label-automation-panel">
        <PanelHeader title="自动化水平与执行映射" subtitle="业务层展示标签优化运行；诊断层展示底层任务定义、资产选择、运行请求和资产检查" icon={<Workflow size={16} />} />
        <div className="label-automation-layout">
          <div className="label-automation-levels">
            <div className="label-automation-head">
              <span>当前 {automationLevel}</span>
              <strong>{activeAutomation.name}</strong>
              <em>推荐 {recommendedAutomation.key} · {recommendedAutomation.name}</em>
            </div>
            {labelAutomationLevels.map((level) => (
              <button
                key={level.key}
                type="button"
                className={automationLevel === level.key ? "active" : ""}
                onClick={() => selectAutomationLevel(level.key)}
                disabled={!LABEL_DEMO_MODE && (level.key === "L3" || level.key === "L4")}
                title={!LABEL_DEMO_MODE && (level.key === "L3" || level.key === "L4") ? "L1→L2 阶段不开放自动发布" : undefined}
              >
                <b>{level.key}</b>
                <strong>{level.name}</strong>
                <span>{level.writePolicy}</span>
                <em>升级：{level.upgrade}</em>
                <small>{level.blockers.join(" / ")}</small>
              </button>
            ))}
          </div>
          <div className="label-runrequest-preview">
            <div className="label-runrequest-head">
              <div>
                <span>运行请求草稿</span>
                <strong>{dagsterDraftState}</strong>
              </div>
              <button type="button" onClick={generateOptimizationRunDraft}>重新生成</button>
            </div>
            {dagsterDraftRows.map(([label, value]) => (
              <button key={label} type="button" onClick={() => setActionFeedback(`${label}: ${value}`)}>
                <span>{label}</span>
                <strong>{value}</strong>
              </button>
            ))}
            <div className="label-dagster-object-grid">
              {[
                ["任务定义", optimizationInputs.jobName],
                ["AssetSelection", "candidates + eval + human_review"],
                ["PartitionsDefinition", "tenant_id × store_id × date × tag_scope"],
                ["资产生成记录", "候选标签 / 评测运行 / 人审任务"],
                ["AssetCheck", gateIsBlocked ? "blocked" : "passed"]
              ].map(([name, detail]) => (
                <button key={name} type="button" onClick={() => setActionFeedback(`${name}：${detail}`)}>
                  <span>{name}</span>
                  <strong>{detail}</strong>
                </button>
              ))}
            </div>
          </div>
        </div>
      </section>
    );

  const renderRunContextHeader = (focus: "evaluation" | "release") => (
      <section className="module-panel wide label-run-context-panel">
        <PanelHeader
          title="标签优化运行详情"
          subtitle={focus === "evaluation" ? "当前焦点：改了有没有变好、哪些指标会阻断门禁" : "当前焦点：能不能发布、为什么不能、下一步做什么"}
          icon={<GitBranch size={16} />}
        />
        <div className="label-run-context-grid">
          {[
            ["标签任务", labelOptimizationRun.taskName],
            ["当前版本", labelOptimizationRun.input.currentTagVersion],
            ["候选版本", labelOptimizationRun.input.candidateTagVersion],
            ["run_id", labelOptimizationRun.runId],
            ["trace_id", labelOptimizationRun.traceId],
            ["自动化等级", `${labelOptimizationRun.automationLevel} · ${activeAutomation.name}`],
            ["执行状态", labelOptimizationRun.dagsterStatus]
          ].map(([label, value]) => (
            <button key={label} type="button" onClick={() => setActionFeedback(`${label}：${value}`)}>
              <span>{label}</span>
              <strong>{value}</strong>
            </button>
          ))}
        </div>
        <div className="label-run-input-strip">
          {[
            ["输入范围", labelOptimizationRun.input.dataRange],
            ["标签目标", labelOptimizationRun.input.targetTag],
            ["样本集", labelOptimizationRun.input.sampleSet],
            ["Prompt/模型", `${labelOptimizationRun.input.promptVersion} / ${labelOptimizationRun.input.modelVersion}`],
            ["策略", labelOptimizationRun.input.strategy]
          ].map(([label, value]) => (
            <button key={label} type="button" onClick={() => setActionFeedback(`本轮输入 · ${label}：${value}`)}>
              <span>{label}</span>
              <strong>{value}</strong>
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
    renderOptimizationInputPanel,
    renderAgentHumanChangePanel,
    renderChangeEffectPanel,
    renderAutomationDagsterPanel,
    renderRunContextHeader
  };
}

export type LabelsInputRenders = ReturnType<typeof buildLabelsInputRenders>;
