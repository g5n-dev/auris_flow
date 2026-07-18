import { clamp } from "../../../../../shared/runtime/math";
import { extensionDecisionLabels } from "../../../fixtures/boundaryFixtures";
import type { AnnotationMinimapController } from "./conversationBoundaryActions";
import { ChevronDown, Headphones, SkipBack, SkipForward } from "lucide-react";

export function BoundaryExtensionCard({ controller }: { controller: AnnotationMinimapController }) {
  const { boundaryPreview, extensionDrafts, extensionDrag, extensionLockRanges, extensionSourceExpanded, finishExtensionRangeDrag, mergedExtensionCount, moveExtensionRangeDrag, nudgeExtensionLock, previewExtensionCount, selectExtensionCandidate, selectedExtensionId, setExtensionSourceExpanded, startBoundaryPreview, startExtensionRangeDrag, updateExtensionDecision, visibleExtensionRanges, visibleNextExtensionRanges, visiblePreviousExtensionRanges } = controller;
  return (
    <section className={`session-boundary-card boundary-extension-card ${extensionSourceExpanded ? "expanded" : "collapsed"}`}>
                        <div className="boundary-extension-head">
                          <div>
                            <span>前后来源扩展</span>
                            <strong>相邻 wav / 流式 chunk 作为边界精调候选源</strong>
                          </div>
                          <button
                            type="button"
                            className="boundary-extension-toggle"
                            aria-expanded={extensionSourceExpanded}
                            onClick={() => setExtensionSourceExpanded((current) => !current)}
                          >
                            {extensionSourceExpanded ? "收起来源" : `展开来源 ${visibleExtensionRanges.length}`}
                            <ChevronDown size={14} />
                          </button>
                        </div>
                        <div className="boundary-extension-summary" aria-label="前后来源扩展摘要">
                          <span>向前 {visiblePreviousExtensionRanges.length} 段</span>
                          <span>向后 {visibleNextExtensionRanges.length} 段</span>
                          <span>待并入 {mergedExtensionCount}</span>
                          <span>试听 {previewExtensionCount}</span>
                        </div>
                        {extensionSourceExpanded && (
                          <div className="boundary-extension-grid">
                            {visibleExtensionRanges.map((candidate) => {
                              const decision = extensionDrafts[candidate.id] ?? "idle";
                              const lock = extensionLockRanges[candidate.id];
                              const candidateDuration = Math.max(1, candidate.end - candidate.start);
                              const lockLeft = ((lock.start - candidate.start) / candidateDuration) * 100;
                              const lockWidth = ((lock.end - lock.start) / candidateDuration) * 100;
                              const isExtensionDragging = extensionDrag?.candidateId === candidate.id;
                              const activePreviewClip =
                                boundaryPreview?.kind === "extension" && boundaryPreview.id === candidate.id && boundaryPreview.playing
                                  ? boundaryPreview.clip
                                  : null;
                              return (
                                <article
                                  key={candidate.id}
                                  className={`boundary-extension-item ${candidate.direction} ${decision} ${selectedExtensionId === candidate.id ? "selected" : ""}`}
                                  onClick={() => selectExtensionCandidate(candidate)}
                                  tabIndex={0}
                                  onKeyDown={(event) => {
                                    if (event.key === "Enter" || event.key === " ") selectExtensionCandidate(candidate);
                                  }}
                                >
                                  <div className="boundary-extension-meta">
                                    <span>{candidate.direction === "previous" ? "向前扩展" : "向后扩展"}</span>
                                    <strong>{candidate.label} · {candidate.sourceKind}</strong>
                                    <em>{candidate.candidateRange}</em>
                                    <b>{Math.round(candidate.confidence * 100)}%</b>
                                  </div>
                                  <p>{candidate.recommendation}</p>
                                  <div className="boundary-extension-file">
                                    <span>{candidate.wallStart} - {candidate.wallEnd}</span>
                                    <code>{candidate.file}</code>
                                    <i>{extensionDecisionLabels[decision]}</i>
                                  </div>
                                  <div className="boundary-extension-locker">
                                    <div className="boundary-extension-locker-head">
                                      <span>自由选取范围</span>
                                      <strong>{lock.startClock} - {lock.endClock}</strong>
                                      <em>拖左右手柄改边界，拖中段平移选区；点击空白轨道移动最近边界</em>
                                    </div>
                                    <div
                                      className={`boundary-extension-rail ${isExtensionDragging ? "dragging" : ""}`}
                                      role="slider"
                                      aria-label={`${candidate.label} 自由选取范围`}
                                      aria-valuemin={candidate.start}
                                      aria-valuemax={candidate.end}
                                      aria-valuenow={lock.start}
                                      onPointerDown={(event) => startExtensionRangeDrag(candidate, event)}
                                      onPointerMove={(event) => moveExtensionRangeDrag(candidate, event)}
                                      onPointerUp={(event) => finishExtensionRangeDrag(event)}
                                      onPointerCancel={(event) => finishExtensionRangeDrag(event)}
                                      onClick={(event) => event.stopPropagation()}
                                    >
                                      <span className="boundary-extension-rail-fill" />
                                      <span
                                        className={`boundary-extension-window ${isExtensionDragging ? `dragging ${extensionDrag.mode}` : ""}`}
                                        style={{
                                          left: `${clamp(lockLeft, 0, 100)}%`,
                                          width: `${Math.max(6, Math.min(100, lockWidth))}%`
                                        }}
                                        data-extension-drag-mode="window"
                                      >
                                        <button
                                          type="button"
                                          className="boundary-extension-handle start"
                                          aria-label={`拖动 ${candidate.label} 开始边界`}
                                          data-extension-drag-mode="start"
                                        />
                                        <span className="boundary-extension-window-label">{Math.round(lock.end - lock.start)}s</span>
                                        <button
                                          type="button"
                                          className="boundary-extension-handle end"
                                          aria-label={`拖动 ${candidate.label} 结束边界`}
                                          data-extension-drag-mode="end"
                                        />
                                      </span>
                                    </div>
                                    <div className="boundary-extension-preview-actions">
                                      {[
                                        { clip: "before" as const, label: "试听前段", Icon: SkipBack },
                                        { clip: "source" as const, label: "试听选区", Icon: Headphones },
                                        { clip: "after" as const, label: "试听后段", Icon: SkipForward }
                                      ].map(({ clip, label, Icon }) => (
                                        <button
                                          key={clip}
                                          type="button"
                                          className={activePreviewClip === clip ? "active" : ""}
                                          onClick={(event) => startBoundaryPreview(candidate, clip, event)}
                                        >
                                          <Icon size={12} />
                                          {label}
                                        </button>
                                      ))}
                                    </div>
                                    <div className="boundary-extension-lock-actions">
                                      <button
                                        type="button"
                                        onClick={(event) => {
                                          event.stopPropagation();
                                          nudgeExtensionLock(candidate, -5);
                                        }}
                                      >
                                        左移 5s
                                      </button>
                                      <button
                                        type="button"
                                        onClick={(event) => {
                                          event.stopPropagation();
                                          nudgeExtensionLock(candidate, 5);
                                        }}
                                      >
                                        右移 5s
                                      </button>
                                    </div>
                                    <div className={activePreviewClip ? "boundary-extension-preview-state active" : "boundary-extension-preview-state"}>
                                      <span>{activePreviewClip ? "弹窗试听中" : "弹窗待播放"}</span>
                                      <strong>{activePreviewClip ? boundaryPreview?.windowText : `${lock.startClock} - ${lock.endClock}`}</strong>
                                    </div>
                                  </div>
                                  <div className="boundary-extension-evidence">
                                    {candidate.evidence.map((item) => (
                                      <span key={item}>{item}</span>
                                    ))}
                                  </div>
                                  <div className="boundary-extension-actions">
                                    <button
                                      type="button"
                                      className={decision === "preview" ? "active" : ""}
                                      onClick={(event) => updateExtensionDecision(candidate, "preview", event)}
                                    >
                                      标记试听
                                    </button>
                                    <button
                                      type="button"
                                      className={decision === "merged" ? "active" : ""}
                                      onClick={(event) => updateExtensionDecision(candidate, "merged", event)}
                                    >
                                      并入选区
                                    </button>
                                    <button
                                      type="button"
                                      className={decision === "split" ? "active" : ""}
                                      onClick={(event) => updateExtensionDecision(candidate, "split", event)}
                                    >
                                      拆出新会话
                                    </button>
                                  </div>
                                </article>
                              );
                            })}
                          </div>
                        )}
                      </section>
  );
}
