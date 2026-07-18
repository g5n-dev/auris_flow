import { BoundaryExtensionCard } from "./BoundaryExtensionCard";
import { BoundaryTuneCard } from "./BoundaryTuneCard";
import type { AnnotationMinimapController } from "./conversationBoundaryActions";
import { Check, Link2, X } from "lucide-react";

export function SessionBoundaryDialog({ controller }: { controller: AnnotationMinimapController }) {
  const { selectedSlice, selectedSourceShape, sessionBoundary, sessionRangeText, setSelectedSliceId, syncStateMeta } = controller;
  return (
    selectedSlice && (
                <div
                  className="session-boundary-scrim"
                  role="presentation"
                  onMouseDown={(event) => event.target === event.currentTarget && setSelectedSliceId(null)}
                >
                  <section className="session-boundary-panel" role="dialog" aria-modal="true" aria-label={`${selectedSlice.label} 会话边界详情`}>
                    <div className="session-boundary-head">
                      <div>
                        <span>会话边界编辑</span>
                        <strong>
                          {selectedSlice.label} · 来源片段 {selectedSlice.sourceStart}s-{selectedSlice.sourceEnd}s
                        </strong>
                        <p>{selectedSlice.file}</p>
                      </div>
                      <button type="button" onClick={() => setSelectedSliceId(null)} aria-label="关闭会话边界详情">
                        <X size={16} />
                      </button>
                    </div>

                    <div className="session-boundary-body">
                      <div className="session-boundary-grid">
                        {[
                          ["来源形态", selectedSourceShape],
                          ["完整对话", sessionRangeText],
                          ["片段墙钟", `${selectedSlice.wallStart} - ${selectedSlice.wallEnd}`],
                          ["置信度", `${Math.round(selectedSlice.confidence * 100)}%`],
                          ["同步状态", syncStateMeta.label]
                        ].map(([label, value]) => (
                          <div key={label} className="session-boundary-stat">
                            <span>{label}</span>
                            <b>{value}</b>
                          </div>
                        ))}
                      </div>

                      <section className="session-boundary-card">
                        <span>系统建议</span>
                        <p>{selectedSlice.reason}</p>
                        <small>{selectedSlice.operationHint}</small>
                      </section>

                      <BoundaryTuneCard controller={controller} />

                      <BoundaryExtensionCard controller={controller} />

                      <section className="session-boundary-card evidence">
                      <span>参考原因</span>
                      <div>
                        {selectedSlice.references.map((item) => (
                          <p key={item}>
                            <Check size={13} />
                            {item}
                          </p>
                        ))}
                      </div>
                    </section>

                    <section className="session-boundary-card evidence">
                      <span>边界判断</span>
                      <div>
                        {selectedSlice.boundary.map((item) => (
                          <p key={item}>
                            <Link2 size={13} />
                            {item}
                          </p>
                        ))}
                      </div>
                    </section>

                      <div className="session-boundary-save-summary">
                        <span>将保存完整对话窗口</span>
                        <strong>{sessionRangeText}</strong>
                        <em>开始 {sessionBoundary.start.toFixed(1)}s / 结束 {sessionBoundary.end.toFixed(1)}s</em>
                      </div>
                    </div>
                  </section>
                </div>
              )
  );
}
