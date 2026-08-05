import { layerLevelConfigs } from "../../../../../shared/fixtures/labelLayers";
import { percentToClock } from "../../../model/trackLayout";
import type { WaveformPanelController } from "./trackRegionModalActions";
import { Eye, X } from "lucide-react";

export function TrackEditor({ controller }: { controller: WaveformPanelController }) {
  const { activeSegment, activeTrack, activeTrackMeta, allTracks, createAnnotation, createFeedback, createLayer, createLayerActionLabel, draftAnnotation, hiddenTracks, labelCandidateIds, layerFormOpen, layerKindName, layerLevelConfig, layerLevelKey, layerName, layerTag, layerType, moveSegment, renderTrackRegions, selectLayerLevel, selectLayerTag, selectedRegionData, setActiveTrack, setDraftAnnotation, setHiddenTracks, setLayerFormOpen, setLayerName, setLayerType, showTracks, toggleTrack, trackLayouts, trackLevelLabel, visibleCount } = controller;
  const canReviseLabel = labelCandidateIds.length > 0;
  const labelRevisionDisabledReason = "当前任务未绑定可修订的标签候选";
  return (
    showTracks && (
          <section className={layerFormOpen ? "tk show-layer-form" : "tk"}>
            <div className="tk-operate">
              <div className="segment-control">
                <span>标注到</span>
                <button className="segment-step" onClick={() => moveSegment("prev")}>‹</button>
                <button className="segment-chip">
                  {activeSegment.label}
                  <b>{activeSegment.time}</b>
                </button>
                <button className="segment-step" onClick={() => moveSegment("next")}>›</button>
                {selectedRegionData && (
                  <span className="region-time">
                    {percentToClock(selectedRegionData.left)} → {percentToClock(selectedRegionData.left + selectedRegionData.width)}
                  </span>
                )}
              </div>
              <div className="track-select">
                {allTracks.map((track) => (
                  <button
                    key={track.key}
                    className={activeTrack === track.key ? `active ${track.color}` : hiddenTracks[track.key] ? "hidden" : ""}
                    data-track-select={track.key}
                    onClick={() => {
                      if (hiddenTracks[track.key]) {
                        setHiddenTracks((current) => ({ ...current, [track.key]: false }));
                      }
                      setActiveTrack(track.key);
                    }}
                    title={hiddenTracks[track.key] ? "恢复轨道" : "切换轨道"}
                  >
                    {track.label}
                  </button>
                ))}
              </div>
              <input
                value={draftAnnotation}
                disabled={!canReviseLabel}
                title={!canReviseLabel ? labelRevisionDisabledReason : "输入人工修订后的标签值。"}
                onChange={(event) => setDraftAnnotation(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") createAnnotation();
                }}
                placeholder={`新建${activeTrackMeta.label}`}
              />
              <button className="create-annotation" onClick={() => createAnnotation()} disabled={!canReviseLabel || !draftAnnotation.trim()} title={!canReviseLabel ? labelRevisionDisabledReason : "加入当前人审决定。"}>
                创建标注
              </button>
              <button className="add-layer-inline" onClick={() => setLayerFormOpen(true)} disabled={!canReviseLabel} title={!canReviseLabel ? labelRevisionDisabledReason : "新增标签层修订。"}>
                + 添加标签层
              </button>
              <span className={createFeedback ? "tk-stat tk-feedback" : "tk-stat"}>
                {createFeedback || `可见 ${visibleCount}/${allTracks.length}`}
              </span>
            </div>
            <div className="track-quick-tags">
              {["报价金额", "试驾邀约", "价格异议", "单据冲突", "串音复核", "Agent确认", "售后跟进"].map((tag) => (
                <button key={tag} onClick={() => createAnnotation(tag)} disabled={!canReviseLabel} title={!canReviseLabel ? labelRevisionDisabledReason : `修订为${tag}`}>
                  {tag}
                </button>
              ))}
            </div>
            <div className="tk-vscroll">
              <div className="tk-in">
                <div className="tk-lab">
                  {trackLayouts.map(({ track, height }) => (
                    <div key={track.key} className="tl" data-track-row={track.key} style={{ height, flex: "0 0 auto" }}>
                      <span className={`ds ds-${track.color}`} />
                      <span className="nm">{track.label}</span>
                      <span className="ic">{trackLevelLabel(track)}</span>
                      <button className="vs" onClick={() => toggleTrack(track.key)} title={`隐藏${track.label}`} aria-label={`隐藏${track.label}`}>
                        <Eye size={12} aria-hidden="true" />
                      </button>
                    </div>
                  ))}
                  <div className="tl tl-add">
                    <button onClick={() => setLayerFormOpen(true)} disabled={!canReviseLabel} title={!canReviseLabel ? labelRevisionDisabledReason : "新增标签层修订。"}>+ 添加标签层</button>
                  </div>
                </div>
                <div className="tk-cv">
                  {trackLayouts.map((layout) => renderTrackRegions(layout))}
                  <div className="tk-cur" />
                </div>
              </div>
            </div>

            {layerFormOpen && (
              <div className="layer-form inline-layer-form op">
                <div className="lf-head">
                  <div>
                    <span>新增标注轨道</span>
                    <strong>{layerKindName}</strong>
                    <em>当前片段 {activeSegment.label} · {activeSegment.time}</em>
                  </div>
                  <button type="button" aria-label="关闭新增标注轨道" onClick={() => setLayerFormOpen(false)}>
                    <X size={14} />
                  </button>
                </div>
                <div className="lf-level-block">
                  <span>目标层级</span>
                  <div className="lf-levels" role="group" aria-label="选择标签层级">
                    {layerLevelConfigs.map((config) => (
                      <button
                        key={config.key}
                        className={layerLevelKey === config.key ? "on" : ""}
                        data-level-key={config.key}
                        onClick={() => selectLayerLevel(config.key)}
                      >
                        <b>L{config.level}</b>
                        <span>{config.label.replace(/^L\d+\s*/, "")}</span>
                      </button>
                    ))}
                  </div>
                </div>
                <div className="lf-config">
                  <label className="lf-name">
                    <span>轨道显示名称</span>
                    <input value={layerName} onChange={(event) => setLayerName(event.target.value)} placeholder="例如 客户复购线索" />
                  </label>
                  <label className="lf-type">
                    <span>标注形态</span>
                    <select value={layerType} onChange={(event) => setLayerType(event.target.value)}>
                      {layerLevelConfig.types.map((type) => (
                        <option key={type} value={type}>
                          {type}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                <div className="lf-tag-panel">
                  <span>候选标签</span>
                  <div className="lf-tag-list" aria-label={`L${layerLevelConfig.level} 可选标签`}>
                    {layerLevelConfig.tags.map((tag) => (
                      <button key={tag} className={layerTag === tag ? "on" : ""} onClick={() => selectLayerTag(tag)}>
                        {tag}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="lf-anchor">
                  <span>创建结果</span>
                  <b>{layerName.trim() || layerTag}</b>
                  <small>L{layerLevelConfig.level} · {layerLevelConfig.category} · {layerType} · 预置标签「{layerTag}」</small>
                </div>
                <div className="lf-actions">
                  <button onClick={() => setLayerFormOpen(false)}>取消</button>
                  <button className="pr" data-create-layer onClick={createLayer} disabled={!canReviseLabel} title={!canReviseLabel ? labelRevisionDisabledReason : "加入当前人审决定。"}>{createLayerActionLabel}</button>
                </div>
              </div>
            )}
          </section>
          )
  );
}
