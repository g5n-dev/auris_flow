import { matrixTimes } from "../../../fixtures/evidenceFixtures";
import type { AnnotationMinimapController } from "./conversationBoundaryActions";

export function MinimapCanvas({ controller }: { controller: AnnotationMinimapController }) {
  const { activeAssociation, activeEdit, activeEvent, activeTarget, associationEdits, confidencePct, eventAssociationsImported, minimapFilteredEvents, mode, orderedLanes, selectedWindow, setActiveAssociation, setSelectedWindow, showAssociationLayer, showBizLayer, showDocAssociations, showEnergyLayer, showOverlapLayer, showVoiceAssociations, updateActiveAssociation, visibleAssociationTargets, visibleEmployees } = controller;
  return (
    <div className={`mm-svg-wrap mode-${mode}`}>
                <div className="mm-axis">
                  {matrixTimes.map((time) => (
                    <span key={time}>{time}</span>
                  ))}
                </div>
                <div className="mm-ribbon">
                  {orderedLanes.map((lane, laneIndex) => (
                    <button
                      key={lane.sub}
                      className={visibleEmployees[lane.sub] ? "mm-ribbon-row" : "mm-ribbon-row off"}
                      onClick={() => setSelectedWindow(laneIndex % 2 === 0 ? "12:23 - 12:33" : "12:18 - 12:29")}
                    >
                      <span className="mm-ribbon-lbl">{lane.name}</span>
                      <span className="mm-ribbon-cells">
                        {Array.from({ length: 72 }, (_, i) => {
                          const isFocus = lane.sub === "A-1001" && i > 34 && i < 42;
                          const isOverlap =
                            (lane.sub === "B-2001" && i > 35 && i < 43) || (lane.sub === "Hall-Mic-1" && i > 37 && i < 45);
                          const isBiz = i === 39 || i === 55;
                          return (
                            <i
                              key={i}
                              className={[
                                "mm-evt",
                                lane.hue,
                                isFocus && showEnergyLayer ? "focus" : "",
                                isOverlap && showOverlapLayer ? "overlap" : "",
                                isBiz && showBizLayer ? "biz" : ""
                              ].join(" ")}
                              style={{ opacity: visibleEmployees[lane.sub] ? 0.18 + (((i * 13 + laneIndex * 9) % 80) / 100) : 0.05 }}
                            />
                          );
                        })}
                      </span>
                    </button>
                  ))}
                </div>
                {showAssociationLayer && (
                  <div className="mm-association-layer" aria-label="语音事件与单据事件关联">
                    <div className="mm-assoc-map">
                      {showVoiceAssociations && <span className="mm-assoc-label voice">语音事件</span>}
                      {showDocAssociations && <span className="mm-assoc-label doc">单据事件</span>}
                      {showVoiceAssociations && showDocAssociations && (
                        <svg className="mm-assoc-svg" viewBox="0 0 100 56" preserveAspectRatio="none" aria-hidden="true">
                          {activeTarget && (
                            <>
                              <line
                                x1={activeEvent.left + activeEvent.width / 2}
                                y1="17"
                                x2={activeTarget.targetLeft + activeTarget.targetWidth / 2}
                                y2="39"
                                className={`mm-assoc-line ${activeEvent.tone} ${activeEdit.status === "人工确认" ? "confirmed" : ""}`}
                              />
                              <circle cx={activeEvent.left + activeEvent.width / 2} cy="17" r="1.7" className="mm-assoc-handle voice" />
                              <circle cx={activeTarget.targetLeft + activeTarget.targetWidth / 2} cy="39" r="1.7" className="mm-assoc-handle doc" />
                            </>
                          )}
                        </svg>
                      )}
                      {showVoiceAssociations && (
                        <div className="mm-assoc-row voice">
                          {minimapFilteredEvents.map((event) => {
                            const edit = associationEdits[event.id] ?? { confidence: event.confidence, targetId: event.id, status: "AI建议" };
                            return (
                              <button
                                key={`voice-${event.id}`}
                                className={`mm-assoc-node voice ${event.tone} ${activeAssociation === event.id ? "active" : ""}`}
                                style={{ left: `${event.left}%`, width: `${Math.max(7, event.width)}%` }}
                                onClick={() => {
                                  setActiveAssociation(event.id);
                                  setSelectedWindow(event.window.replace("-", " - "));
                                }}
                                title={`${event.window} · ${event.asr} · 置信度 ${Math.round(edit.confidence * 100)}%`}
                              >
                                <span>{event.type.replace("事件", "")}</span>
                                <b>{Math.round(edit.confidence * 100)}%</b>
                              </button>
                            );
                          })}
                        </div>
                      )}
                      {showDocAssociations && (
                        <div className="mm-assoc-row doc">
                          {visibleAssociationTargets.map((target) => {
                            const linked = activeEdit.targetId === target.id;
                            return (
                              <button
                                key={`doc-${target.id}`}
                                className={`mm-assoc-node doc ${target.tone} ${linked ? "linked" : ""}`}
                                style={{ left: `${target.targetLeft}%`, width: `${target.targetWidth}%` }}
                                onClick={() => {
                                  updateActiveAssociation({
                                    targetId: target.id,
                                    confidence: target.id === activeEvent.id ? activeEvent.confidence : Math.min(0.98, Math.max(0.58, activeEdit.confidence - 0.08)),
                                    status: target.id === activeEvent.id ? "AI建议" : "人工调整"
                                  });
                                }}
                                title={`调整 ${activeEvent.type} → ${target.docEvent} · ${target.field}`}
                              >
                                <span>{target.doc.replace(/ .*/, "")}</span>
                                {linked && <b>目标</b>}
                              </button>
                            );
                          })}
                        </div>
                      )}
                    </div>
                    <aside className="mm-assoc-editor">
                      <div className="mm-ae-head">
                        <span>{activeEdit.status}</span>
                        <b>{confidencePct}%</b>
                      </div>
                      <strong>{activeEvent.type}</strong>
                      <p>{activeEvent.asr}</p>
                      <div className="mm-ae-target">
                        <span>目标</span>
                        <b>{activeTarget ? activeTarget.docEvent : "未关联"}</b>
                      </div>
                      <label className="mm-ae-range">
                        <span>置信度</span>
                        <input
                          type="range"
                          min="0"
                          max="100"
                          value={confidencePct}
                          onChange={(event) => updateActiveAssociation({ confidence: Number(event.target.value) / 100, status: "人工调整" })}
                        />
                      </label>
                      <div className="mm-ae-actions">
                        <button onClick={() => updateActiveAssociation({ status: "人工确认" })}>确认关联</button>
                        <button
                          onClick={() =>
                            updateActiveAssociation({
                              targetId: "",
                              confidence: 0.2,
                              status: "解除关联"
                            })
                          }
                        >
                          解除
                        </button>
                      </div>
                    </aside>
                  </div>
                )}
                {eventAssociationsImported && mode === "receipt" && minimapFilteredEvents.length === 0 && (
                  <div className="mm-assoc-empty">当前标签轨道没有可匹配事件，恢复轨道或切换到单据/质检/意图层继续定位。</div>
                )}
                <div className="mm-win" title={selectedWindow} />
                <div className="mm-cur" />
              </div>
  );
}
