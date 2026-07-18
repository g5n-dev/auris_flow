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
import type { LabelsInputRenders } from "./buildLabelsInputRenders";
import type { LabelsDecisionRenders } from "./buildLabelsDecisionRenders";
import type { LabelsWorkbenchRenders } from "./buildLabelsWorkbenchRenders";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { promptAdapterRows, promptBackendContracts } from "../fixtures/governanceCatalog";
import { Database, ShieldCheck } from "lucide-react";

type BuildLabelsContractRendersScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel & LabelsGovernanceModel & LabelsConflictModel & LabelsIntentRecovery & LabelsNavigationActions & LabelsOptimizationActions & LabelsReviewActions & LabelsPersistenceActions & LabelsPromptActions & LabelsEvaluationActions & LabelsReleaseActions & LabelsCoreRenders & LabelsInputRenders & LabelsDecisionRenders & LabelsWorkbenchRenders;

export function buildLabelsContractRenders(actionFeedback: BuildLabelsContractRendersScope["actionFeedback"], applyCandidateAction: BuildLabelsContractRendersScope["applyCandidateAction"], labelEntityAction: BuildLabelsContractRendersScope["labelEntityAction"], labelPublishPending: BuildLabelsContractRendersScope["labelPublishPending"], labelPublishRequest: BuildLabelsContractRendersScope["labelPublishRequest"], releaseDecision: BuildLabelsContractRendersScope["releaseDecision"], releaseGateRows: BuildLabelsContractRendersScope["releaseGateRows"], renderReleaseGateEditor: BuildLabelsContractRendersScope["renderReleaseGateEditor"], setActionFeedback: BuildLabelsContractRendersScope["setActionFeedback"], setReleaseDecision: BuildLabelsContractRendersScope["setReleaseDecision"], submitReleaseGate: BuildLabelsContractRendersScope["submitReleaseGate"]) {
  const renderBackendContractPanel = () => (
      <section className="module-panel wide label-api-contract-panel">
        <PanelHeader title="Auris 后端接口抽象" subtitle="业务 UI 只依赖内部 API；外部框架只作为后端适配器" icon={<Database size={16} />} />
        <div className="label-api-grid">
          {promptBackendContracts.map(([object, api, purpose]) => (
            <button key={object} type="button" onClick={() => setActionFeedback(`${object} 对接 ${api}：${purpose}`)}>
              <span>{object}</span>
              <strong>{api}</strong>
              <em>{purpose}</em>
            </button>
          ))}
        </div>
        <div className="label-adapter-grid">
          {promptAdapterRows.map(([name, capability, role]) => (
            <button key={name} type="button" onClick={() => setActionFeedback(`${name} 仅作为后端适配：${capability}`)}>
              <strong>{name}</strong>
              <span>{capability}</span>
              <em>{role}</em>
            </button>
          ))}
        </div>
      </section>
    );

  const renderReleaseGateDeepened = () => (
      <section className="module-panel wide label-release-gate-panel">
        <PanelHeader title="发布门禁" subtitle="低于阈值时禁止发布，只能送 Human Loop 或继续优化" icon={<ShieldCheck size={16} />} />
        <div className="label-release-gate-grid">
          {releaseGateRows.map(([label, passed, detail]) => (
            <button key={label} type="button" className={passed ? "passed" : "blocked"} onClick={() => setActionFeedback(`${label}：${detail}`)}>
              <span>{passed ? "通过" : "阻断"}</span>
              <strong>{label}</strong>
              <em>{detail}</em>
            </button>
          ))}
        </div>
        <div className="label-release-gate-body">
          {renderReleaseGateEditor()}
          <aside className="label-release-side">
            <div>
              <span>当前发布决策</span>
              <strong>{releaseDecision}</strong>
              <p>{actionFeedback}</p>
            </div>
            <div className="label-gate-actions">
              <button type="button" onClick={() => setReleaseDecision("仅影子评测")}>仅影子评测</button>
              <button type="button" disabled={labelEntityAction !== null} onClick={() => void applyCandidateAction("human")}>送 Human Loop</button>
              <button type="button" className="primary" onClick={submitReleaseGate} disabled={labelPublishPending}>
                {labelPublishPending && labelPublishRequest.action === "gate" ? "提交发布门禁 · pending" : "提交发布门禁"}
              </button>
            </div>
          </aside>
        </div>
      </section>
    );

  return {
    renderBackendContractPanel,
    renderReleaseGateDeepened
  };
}

export type LabelsContractRenders = ReturnType<typeof buildLabelsContractRenders>;
