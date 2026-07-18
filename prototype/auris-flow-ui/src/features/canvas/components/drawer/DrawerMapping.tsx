import type { CanvasController } from "../../controller/useCanvasController";
import { loginRiskApiContracts } from "../../catalog";
import { Check, Sparkles, X } from "lucide-react";

export function DrawerMapping({ controller }: { controller: CanvasController }) {
  const { activeIntent, activeIntentKey, activeMappingSuggestions, appliedMappingCount, applyTrustedMappings, confirmedMappingCount, contractNodeMap, drawerTab, mappingCompletionPct, mappingConfidenceThreshold, mappingStateLabel, mappingTotal, pendingMappingCount, rejectedMappingCount, relatedNodeCount, resetActiveMappings, selectedMapping, selectedNodeId, setMappingConfidenceThreshold, setMappingDecision, setSelectedMappingId, setSelectedNodeId, trustedMappingCount, updateMappingPolicy, updateMappingTarget } = controller;
  return (
    <>
      {drawerTab === "mapping" && (
                  <section className="ai-map-card mapping-workbench">
                    <div className="ai-map-head">
                      <strong>
                        <Sparkles size={15} />
                        AI 映射助手
                      </strong>
                      <button type="button" onClick={resetActiveMappings}>
                        重新生成
                      </button>
                    </div>
                    <div className="mapping-summary">
                      <span>关联链路</span>
                      <b>{activeIntent.step}</b>
                      <span>写入范围</span>
                      <b>当前草稿 · {relatedNodeCount} 节点</b>
                      <span>应用进度</span>
                      <b>{appliedMappingCount}/{mappingTotal} · {mappingCompletionPct}%</b>
                      <span>人审状态</span>
                      <b>{pendingMappingCount} 待确认 / {confirmedMappingCount} 已确认 / {rejectedMappingCount} 已拒绝</b>
                    </div>
                    <div className="mapping-progress" aria-label="映射应用进度">
                      <span style={{ width: `${mappingCompletionPct}%` }} />
                    </div>
                    <div className="mapping-threshold">
                      <label>
                        <span>自动可信阈值</span>
                        <input
                          type="range"
                          min="80"
                          max="98"
                          value={mappingConfidenceThreshold}
                          onChange={(event) => setMappingConfidenceThreshold(Number(event.target.value))}
                        />
                        <b>{mappingConfidenceThreshold}%</b>
                      </label>
                      <em>{trustedMappingCount} 项达到阈值，可直接应用；低于阈值必须人工确认。</em>
                    </div>

                    <div className="mapping-workbench-grid">
                      {selectedMapping && (
                        <div className="mapping-editor">
                          <div className="mapping-editor-head">
                            <span>人工调整映射</span>
                            <strong>{selectedMapping.targetAsset}</strong>
                          </div>
                          <label>
                            来源字段
                            <input value={`${selectedMapping.sourceLabel}.${selectedMapping.sourceField}`} readOnly />
                          </label>
                          <label>
                            目标字段
                            <select value={selectedMapping.targetField} onChange={(event) => updateMappingTarget(selectedMapping.id, event.target.value)}>
                              {selectedMapping.targetOptions.map((option) => (
                                <option key={option} value={option}>
                                  {option}
                                </option>
                              ))}
                            </select>
                          </label>
                          <label>
                            匹配策略
                            <select value={selectedMapping.policy} onChange={(event) => updateMappingPolicy(selectedMapping.id, event.target.value)}>
                              {selectedMapping.policyOptions.map((option) => (
                                <option key={option} value={option}>
                                  {option}
                                </option>
                              ))}
                            </select>
                          </label>
                          <div className="mapping-reason-box">
                            <span>智能建议依据</span>
                            <p>{selectedMapping.reason}</p>
                            {selectedMapping.evidence.map((item) => (
                              <button key={item} type="button" onClick={() => setSelectedNodeId(selectedMapping.sourceNodeId)}>
                                <Check size={13} />
                                {item}
                              </button>
                            ))}
                          </div>
                          <div className="mapping-editor-actions">
                            <button type="button" onClick={() => setMappingDecision(selectedMapping.id, "rejected")}>
                              <X size={14} />
                              拒绝
                            </button>
                            <button type="button" onClick={() => setMappingDecision(selectedMapping.id, "confirmed")}>
                              <Check size={14} />
                              确认映射
                            </button>
                            <button type="button" onClick={() => setMappingDecision(selectedMapping.id, "applied")}>
                              写入草稿
                            </button>
                          </div>
                        </div>
                      )}

                      <div className="mapping-suggestion-list" aria-label="AI 映射建议列表">
                        {activeMappingSuggestions.map((item) => (
                          <button
                            key={item.id}
                            type="button"
                            className={[
                              "mapping-row",
                              `state-${item.state}`,
                              selectedMapping?.id === item.id ? "active" : ""
                            ].join(" ")}
                            onClick={() => {
                              setSelectedMappingId(item.id);
                              setSelectedNodeId("ai");
                            }}
                          >
                            <div>
                              <em>{item.sourceLabel}</em>
                              <span>{item.sourceField}</span>
                              <strong>{item.targetField}</strong>
                            </div>
                            <b>{item.confidence}%</b>
                            <small>{mappingStateLabel[item.state]}</small>
                            <i>{item.joinKey}</i>
                          </button>
                        ))}
                      </div>
                    </div>

                    <button className="apply-mapping" type="button" onClick={applyTrustedMappings}>
                      <Check size={15} />
                      应用已确认和可信映射
                    </button>

                    {activeIntentKey === "review" && (
                      <div className="api-contract-mini mapping-contract-mini">
                        <span>接口字段约束</span>
                        {loginRiskApiContracts.slice(0, 5).map((contract) => (
                          <button
                            key={contract.name}
                            type="button"
                            onClick={() => setSelectedNodeId(contractNodeMap[contract.name] ?? selectedNodeId)}
                          >
                            <b>{contract.name}</b>
                            <strong>{contract.response}</strong>
                            <em>{contract.dagster}</em>
                          </button>
                        ))}
                      </div>
                    )}
                  </section>
                )}
    </>
  );
}
