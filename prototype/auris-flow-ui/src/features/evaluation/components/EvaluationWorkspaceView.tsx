import type { EvaluationController } from "../controller/useEvaluationController";

export function EvaluationWorkspaceView({ controller }: { controller: EvaluationController }) {
  const { currentView, evaluationNotice, renderCurrentView } = controller;
  return (
    <div className={`module-grid eval-grid evaluation-view-${currentView}`}>
      <div
        className={`operation-toast evaluation-operation-toast is-${evaluationNotice.status}`}
        role="status"
        aria-live="polite"
      >
        <strong>{evaluationNotice.title}</strong>
        <span>{evaluationNotice.detail}</span>
      </div>
      {renderCurrentView()}
    </div>
  );
}
