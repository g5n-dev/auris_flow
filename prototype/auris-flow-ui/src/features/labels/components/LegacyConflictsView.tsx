import type { LabelsController } from "../controller/useLabelsController";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { AlertTriangle, GitBranch, Headphones, UserCheck } from "lucide-react";

export function LegacyConflictsView({ controller }: { controller: LabelsController }) {
  const { activeConflict, activeIntent, activeReviewTask, applyConflictDecision, conflictCases, conflictDecision, conflictImpactRows, conflictNote, editableDraftTagName, navigateToTarget, openLabelAsset, openLabelEvidence, openLabelIntentDetail, renderHumanLoopWorkbench, renderLabelEntityNotice, reviewState, reviewTasks, setActiveModule, setConflictDecision, setConflictNote, setSelectedConflictKey, setSelectedReviewId } = controller;
  return (
    (
          <div className="module-grid label-grid label-grid-conflicts">
            {renderLabelEntityNotice()}
            <section className="module-panel wide label-command-panel">
              <PanelHeader title="标签冲突" subtitle="冲突样本、字段差异、Human Loop 和发布阻断" icon={<AlertTriangle size={16} />} />
              <div className="label-governance-strip">
                {[
                  ["冲突样本", "42", "待仲裁 6", "amber"],
                  ["高风险", `${activeIntent.risk}`, activeIntent.scene, activeIntent.risk === "高" ? "red" : "green"],
                  ["人审队列", `${reviewTasks.length}`, reviewState, "blue"],
                  ["影响资产", "4 下游", "需审批", "violet"]
                ].map(([label, value, meta, tone]) => (
                  <button key={label} type="button" className={`label-governance-card ${tone}`}>
                    <span>{label}</span>
                    <strong>{value}</strong>
                    <em>{meta}</em>
                  </button>
                ))}
              </div>
            </section>
            <section className="module-panel label-conflict-workspace">
              <PanelHeader title="冲突仲裁台" subtitle="样本队列、版本差异、证据链和发布阻断集中处理" icon={<GitBranch size={16} />} />
              <div className="label-conflict-workbench">
                <div className="label-conflict-queue" aria-label="冲突样本队列">
                  <div className="label-conflict-queue-head">
                    <span>冲突样本</span>
                    <strong>{conflictCases.length}</strong>
                  </div>
                  {conflictCases.map((conflict) => (
                    <button
                      key={conflict.key}
                      type="button"
                      className={`label-conflict-case severity-${conflict.severity} ${activeConflict.key === conflict.key ? "active" : ""}`}
                      onClick={() => {
                        setSelectedConflictKey(conflict.key);
                        setConflictDecision("待仲裁");
                      }}
                    >
                      <span>{conflict.severity}风险</span>
                      <strong>{conflict.label}</strong>
                      <em>{conflict.source}</em>
                      <b>{activeConflict.key === conflict.key ? conflictDecision : "待仲裁"}</b>
                    </button>
                  ))}
                  <button type="button" className="label-conflict-create" onClick={() => openLabelEvidence("冲突补样", "evidence")}>
                    <Headphones size={14} />
                    回到证据片段补样本
                  </button>
                </div>

                <div className="label-conflict-main">
                  <div className="label-conflict-summary">
                    <div>
                      <span>{activeConflict.source}</span>
                      <strong>{activeConflict.label}</strong>
                      <p>{activeConflict.detail}</p>
                    </div>
                    <div className={`label-conflict-risk risk-${activeConflict.severity}`}>
                      <span>仲裁状态</span>
                      <b>{conflictDecision}</b>
                    </div>
                  </div>

                  <div className="label-version-diff-grid">
                    <button type="button" onClick={() => openLabelEvidence("线上版本证据")}>
                      <span>线上版本</span>
                      <strong>{activeConflict.current}</strong>
                      <em>v1.8.4 · 生效结果</em>
                    </button>
                    <button type="button" onClick={() => setActiveModule("labels")}>
                      <span>候选版本</span>
                      <strong>{activeConflict.candidate}</strong>
                      <em>v1.9.0-rc2 · 草稿资产</em>
                    </button>
                    <button type="button" onClick={() => openLabelAsset(activeConflict.asset, activeConflict.evidence)}>
                      <span>证据引用</span>
                      <strong>{activeConflict.evidence}</strong>
                      <em>{activeConflict.asset}</em>
                    </button>
                  </div>

                  <div className="label-conflict-evidence-flow" aria-label="冲突证据链">
                    {[
                      ["ASR/音频", activeIntent.evidence],
                      ["单据/事件", activeIntent.layers.doc?.evidence ?? "无强单据冲突"],
                      ["标签规则", activeIntent.layers.qa?.tag ?? editableDraftTagName],
                      ["Human Loop", activeReviewTask.id],
                      ["发布门禁", activeConflict.blocker]
                    ].map(([node, detail], index) => (
                      <button
                        key={node}
                        type="button"
                        onClick={() => {
                          if (index < 2) openLabelEvidence(String(node));
                          else if (index === 3) navigateToTarget({ module: "labels", tab: "review", objectKind: "labelReview", objectId: activeReviewTask.id, title: String(node), origin: { label: "标签治理 / 冲突证据链", module: "labels", objectLabel: activeConflict.key } });
                          else if (index === 4) openLabelIntentDetail(String(node));
                          else openLabelAsset(activeConflict.asset, String(node));
                        }}
                      >
                        <b>{index + 1}</b>
                        <strong>{node}</strong>
                        <span>{detail}</span>
                      </button>
                    ))}
                  </div>

                  <div className="label-conflict-impact-grid">
                    {conflictImpactRows.map(([label, value, detail]) => (
                      <button
                        key={label}
                        type="button"
                        onClick={() => {
                          if (label === "证据片段") openLabelEvidence("冲突影响证据");
                          else if (label === "下游资产") openLabelAsset(activeConflict.asset, "冲突影响资产");
                          else openLabelIntentDetail(String(label));
                        }}
                      >
                        <span>{label}</span>
                        <strong>{value}</strong>
                        <em>{detail}</em>
                      </button>
                    ))}
                  </div>

                  <label className="label-conflict-note">
                    <span>仲裁说明</span>
                    <textarea value={conflictNote} onChange={(event) => setConflictNote(event.target.value)} rows={3} />
                  </label>

                  <div className="label-conflict-decision-bar">
                    {[
                      ["接受候选", "写候选版本"],
                      ["修改规则", "补正负例"],
                      ["转人工仲裁", "进入 Human Loop"],
                      ["阻断发布", "等待复核"]
                    ].map(([decision, hint]) => (
                      <button
                        key={decision}
                        type="button"
                        className={conflictDecision === decision ? "active" : ""}
                        onClick={() => applyConflictDecision(decision)}
                      >
                        <strong>{decision}</strong>
                        <span>{hint}</span>
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            </section>
            <section className="module-panel wide label-human-panel">
              <PanelHeader title="Human Loop" subtitle="接受、修改、拒绝都会回写候选版本" icon={<UserCheck size={16} />} />
              <div className="label-review-list">
                {reviewTasks.map((task) => (
                  <button
                    key={task.id}
                    type="button"
                    className={`label-review-card risk-${task.priority} ${activeReviewTask.id === task.id ? "selected" : ""}`}
                    onClick={() => setSelectedReviewId(task.id)}
                  >
                    <div>
                      <span>{task.id}</span>
                      <b>{reviewState}</b>
                    </div>
                    <strong>{task.title}</strong>
                    <em>{task.type}</em>
                    <p>{task.detail}</p>
                  </button>
                ))}
              </div>
              {renderHumanLoopWorkbench()}
            </section>
          </div>
        )
  );
}
