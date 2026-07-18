import type { WaveformPanelController } from "./trackRegionModalActions";
import { ManualLabelWorkflowCard } from "./ManualLabelWorkflowCard";
import { AlertTriangle, Check, Headphones, Link2, Pause, Plus, SkipBack, SkipForward, SlidersHorizontal, X } from "lucide-react";

export function TrackRegionDialog({ controller }: { controller: WaveformPanelController }) {
  const { activeSegment, createAnnotation, manualLabelWorkflow, modalRegion, modalRegionDraft, modalRegionEnd, modalRegionTime, modalSourceSlice, modalTrack, regionEdits, saveModalAnnotationDraft, savingAnnotationId, setTrackRegionModalId, startTrackPreview, submitModalAnnotationDraft, trackPreviewState, updateModalRegion, updateRegionPosition } = controller;
  const manualDraftFrozen = Boolean(manualLabelWorkflow.draft);
  // React 18 must receive the native boolean attribute as a string; its types expose inert as boolean.
  const manualDraftInert = manualDraftFrozen
    ? ({ inert: "" } as unknown as { inert: boolean })
    : {};
  return (
    modalRegion && modalTrack && (
            <div
              className="session-boundary-scrim track-region-scrim"
              role="presentation"
              onMouseDown={(event) => event.target === event.currentTarget && setTrackRegionModalId(null)}
            >
              <section className="session-boundary-panel track-region-panel" role="dialog" aria-modal="true" aria-label={`${modalTrack.label} 标签轨道编辑`}>
                <div className="session-boundary-head">
                  <div>
                    <span>标签轨道编辑</span>
                    <strong>
                      {modalTrack.label} · {modalRegion.label}
                    </strong>
                    <p>
                      {modalSourceSlice
                        ? `${modalSourceSlice.label} · ${modalSourceSlice.sourceStart}s-${modalSourceSlice.sourceEnd}s`
                        : "当前标签区域"}
                    </p>
                  </div>
                  <button type="button" onClick={() => setTrackRegionModalId(null)} aria-label="关闭标签轨道编辑">
                    <X size={16} />
                  </button>
                </div>

                <div className="session-boundary-body track-region-body">
                  <div className="session-boundary-grid track-region-stats">
                    {[
                      ["轨道", modalTrack.label],
                      ["时间窗", modalRegionTime],
                      ["标签类型", modalRegion.tone || "默认"],
                      ["来源", modalSourceSlice?.label ?? "当前窗口"],
                      ["状态", regionEdits[modalRegion.id] ? "草稿已修改" : "原始建议"]
                    ].map(([label, value]) => (
                      <div key={label} className="session-boundary-stat">
                        <span>{label}</span>
                        <b>{value}</b>
                      </div>
                    ))}
                  </div>

                  <section
                    className="session-boundary-card track-region-editor-card"
                    aria-disabled={manualDraftFrozen}
                    {...manualDraftInert}
                  >
                    <span>标注内容</span>
                    <div className="track-region-form">
                      <label>
                        <span>标签名称</span>
                        <input value={modalRegion.label} onChange={(event) => updateModalRegion({ label: event.target.value })} />
                      </label>
                      <label>
                        <span>标签值 / 归一化值</span>
                        <input
                          value={modalRegionDraft?.value ?? ""}
                          onChange={(event) => updateModalRegion({ value: event.target.value })}
                          placeholder="例如 3.5万 / true / 待复核"
                        />
                      </label>
                      <label>
                        <span>字段 Key</span>
                        <input
                          value={modalRegionDraft?.fieldKey ?? ""}
                          onChange={(event) => updateModalRegion({ fieldKey: event.target.value })}
                          placeholder="qa.amount_conflict"
                        />
                      </label>
                      <label>
                        <span>标注类型</span>
                        <select value={modalRegion.tone} onChange={(event) => updateModalRegion({ tone: event.target.value })}>
                          {[
                            ["tg", "普通标签"],
                            ["tg er", "风险标签"],
                            ["asr", "ASR 片段"],
                            ["asr er", "ASR 风险"],
                            ["bz", "业务意图"],
                            ["s1", "销售说话人"],
                            ["s0", "客户说话人"],
                            ["manual", "人工标注"],
                            ["doc-node violet", "单据事件"]
                          ].map(([value, label]) => (
                            <option key={value} value={value}>
                              {label}
                            </option>
                          ))}
                        </select>
                      </label>
                    </div>
                    <div className="track-region-review-form">
                      <label>
                        <span>复核结论</span>
                        <select value={modalRegionDraft?.reviewState ?? "AI建议"} onChange={(event) => updateModalRegion({ reviewState: event.target.value })}>
                          {["AI建议", "人工确认", "待人工复核", "驳回标签", "转串音复核", "需回填单据"].map((state) => (
                            <option key={state} value={state}>
                              {state}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label>
                        <span>置信度</span>
                        <input
                          type="number"
                          min="0"
                          max="100"
                          value={modalRegionDraft?.confidence ?? 0}
                          onChange={(event) => updateModalRegion({ confidence: Number(event.target.value) })}
                        />
                      </label>
                      <label>
                        <span>负责人</span>
                        <input value={modalRegionDraft?.assignee ?? ""} onChange={(event) => updateModalRegion({ assignee: event.target.value })} />
                      </label>
                    </div>
                    <div className="track-region-evidence-form">
                      <label>
                        <span>证据引用</span>
                        <input
                          value={modalRegionDraft?.evidenceRef ?? ""}
                          onChange={(event) => updateModalRegion({ evidenceRef: event.target.value })}
                          placeholder="W2 / ASR-S2 / quote#BJ-041"
                        />
                      </label>
                      <label>
                        <span>写入目标</span>
                        <input
                          value={modalRegionDraft?.writeTarget ?? ""}
                          onChange={(event) => updateModalRegion({ writeTarget: event.target.value })}
                          placeholder="auris/label/segment_annotations"
                        />
                      </label>
                      <label className="wide">
                        <span>片段文本 / 标签依据</span>
                        <textarea
                          value={modalRegionDraft?.sourceText ?? ""}
                          onChange={(event) => updateModalRegion({ sourceText: event.target.value })}
                          rows={3}
                        />
                      </label>
                      <label className="wide">
                        <span>人工备注</span>
                        <textarea
                          value={modalRegionDraft?.note ?? ""}
                          onChange={(event) => updateModalRegion({ note: event.target.value })}
                          rows={2}
                          placeholder="修改原因、回填或训练备注"
                        />
                      </label>
                    </div>
                    <div className="track-region-window-slider" aria-label="滑动调整标签时间窗">
                      <div className="track-region-slider-head">
                        <span>
                          <SlidersHorizontal size={13} />
                          滑动轨道控制
                        </span>
                        <strong>{modalRegionTime}</strong>
                      </div>
                      <div className="track-region-slider-lane">
                        <i style={{ left: `${modalRegion.left}%`, width: `${modalRegion.width}%` }} />
                        <b className="start" style={{ left: `${modalRegion.left}%` }} />
                        <b className="end" style={{ left: `${modalRegionEnd}%` }} />
                      </div>
                      <div className="track-region-range-inputs">
                        <label>
                          <span>开始</span>
                          <input
                            type="range"
                            min="0"
                            max={Math.max(0, modalRegionEnd - 2)}
                            step="0.25"
                            value={modalRegion.left}
                            onChange={(event) => {
                              const nextLeft = Number(event.target.value);
                              updateRegionPosition(modalRegion, nextLeft, modalRegionEnd - nextLeft);
                            }}
                          />
                        </label>
                        <label>
                          <span>结束</span>
                          <input
                            type="range"
                            min={Math.min(100, modalRegion.left + 2)}
                            max="100"
                            step="0.25"
                            value={modalRegionEnd}
                            onChange={(event) => {
                              const nextEnd = Number(event.target.value);
                              updateRegionPosition(modalRegion, modalRegion.left, nextEnd - modalRegion.left);
                            }}
                          />
                        </label>
                      </div>
                    </div>
                    <div className="track-region-time-editor">
                      <button type="button" onClick={() => updateRegionPosition(modalRegion, modalRegion.left - 0.25, modalRegion.width)}>
                        <SkipBack size={13} />
                        左移约 1.5s
                      </button>
                      <button type="button" onClick={() => updateRegionPosition(modalRegion, modalRegion.left + 0.25, modalRegion.width)}>
                        右移约 1.5s
                        <SkipForward size={13} />
                      </button>
                      <button type="button" onClick={() => updateRegionPosition(modalRegion, modalRegion.left, modalRegion.width + 0.35)}>
                        + 扩展
                      </button>
                      <button type="button" onClick={() => updateRegionPosition(modalRegion, modalRegion.left, modalRegion.width - 0.35)}>
                        - 收缩
                      </button>
                      <button type="button" onClick={() => updateRegionPosition(modalRegion, activeSegment.left, activeSegment.width)}>
                        <Check size={13} />
                        对齐当前片段
                      </button>
                    </div>
                  </section>

                  <ManualLabelWorkflowCard controller={controller} />

                  <section className="session-boundary-card track-region-preview-card">
                    <span>独立试听</span>
                    <p>只试听标签前后片段。</p>
                    <div className="track-region-preview-actions">
                      {[
                        { clip: "before" as const, label: "前 5s", Icon: SkipBack },
                        { clip: "current" as const, label: "当前标签", Icon: Headphones },
                        { clip: "after" as const, label: "后 5s", Icon: SkipForward }
                      ].map(({ clip, label, Icon }) => {
                        const activePreview = trackPreviewState?.regionId === modalRegion.id && trackPreviewState.clip === clip;
                        const playingPreview = activePreview && trackPreviewState?.playing;
                        return (
                          <button
                            key={clip}
                            type="button"
                            className={activePreview ? "active" : ""}
                            onClick={() => startTrackPreview(modalRegion, clip as "before" | "current" | "after")}
                          >
                            {playingPreview ? <Pause size={13} /> : <Icon size={13} />}
                            {playingPreview ? "暂停" : `试听${label}`}
                          </button>
                        );
                      })}
                    </div>
                    <div className="track-region-preview-status">
                      <span>预览窗口</span>
                      <strong>{trackPreviewState?.regionId === modalRegion.id ? trackPreviewState.windowText : "未开始试听"}</strong>
                      <em>{trackPreviewState?.regionId === modalRegion.id && trackPreviewState.playing ? "弹窗试听中" : "弹窗待播放"}</em>
                    </div>
                  </section>

                  <section
                    className="session-boundary-card track-region-quick-card"
                    aria-disabled={manualDraftFrozen}
                    {...manualDraftInert}
                  >
                    <span>快捷处理</span>
                    <div className="track-region-quick-actions">
                      <button
                        type="button"
                        onClick={() => updateModalRegion({ reviewState: "人工确认", confidence: Math.max(modalRegionDraft?.confidence ?? 0, 96), tone: modalRegion.tone.replace(" er", "") })}
                      >
                        <Check size={13} />
                        确认标签
                      </button>
                      <button
                        type="button"
                      onClick={() => updateModalRegion({ reviewState: "驳回标签", confidence: Math.min(modalRegionDraft?.confidence ?? 0, 35), note: "证据不足。" })}
                      >
                        <X size={13} />
                        驳回标签
                      </button>
                      <button
                        type="button"
                        onClick={() => updateModalRegion({ reviewState: "待人工复核", tone: modalRegion.tone.includes("er") ? modalRegion.tone : `${modalRegion.tone} er`, note: "需核对 ASR、单据和声纹。" })}
                      >
                        <AlertTriangle size={13} />
                        风险复核
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          createAnnotation(`${modalRegion.label} 人工确认`);
                          updateModalRegion({ reviewState: "人工确认", assignee: "当前登录用户", writeTarget: "auris/label/manual_annotations" });
                        }}
                      >
                        <Plus size={13} />
                        复制为人工标签
                      </button>
                    </div>
                  </section>

                  <section className="session-boundary-card evidence">
                    <span>写入影响</span>
                    <div>
                      <p>
                        <Check size={13} />
                        只写当前标签草稿，携带字段、值、置信度和复核状态。
                      </p>
                      <p>
                        <Link2 size={13} />
                        保存后回写 LabelDraft / SegmentAnnotation，关联 {modalRegionDraft?.evidenceRef ?? "当前证据"}。
                      </p>
                    </div>
                  </section>

                  <div className="session-boundary-save-summary">
                    <span>将保存标签区域</span>
                    <strong>
                      {modalTrack.label} · {modalRegion.label} · {modalRegionDraft?.value}
                    </strong>
                    <em>{modalRegionTime} · {modalRegionDraft?.reviewState} · {modalRegionDraft?.fieldKey}</em>
                  </div>

                  <div className="session-boundary-actions track-region-actions">
                    <button type="button" onClick={() => updateModalRegion({ tone: "manual" })} disabled={manualDraftFrozen}>
                      标为人工复核
                    </button>
                    <button type="button" onClick={() => setTrackRegionModalId(null)}>
                      取消
                    </button>
                    <button
                      type="button"
                      className="primary"
                      onClick={() => manualLabelWorkflow.status === "draft"
                        ? void submitModalAnnotationDraft()
                        : void saveModalAnnotationDraft()}
                      disabled={
                        savingAnnotationId === modalRegion.id
                        || manualDraftFrozen && manualLabelWorkflow.status !== "draft"
                        || !["ready", "draft"].includes(manualLabelWorkflow.status)
                        || manualLabelWorkflow.status !== "draft" && !manualLabelWorkflow.selectedLabelId
                      }
                    >
                      {manualLabelWorkflow.status === "draft"
                        ? "提交冻结草稿"
                        : manualLabelWorkflow.status === "submitted"
                          ? "事实已提交"
                          : savingAnnotationId === modalRegion.id
                            ? "保存中..."
                            : "保存标签草稿"}
                    </button>
                  </div>
                </div>
              </section>
            </div>
          )
  );
}
