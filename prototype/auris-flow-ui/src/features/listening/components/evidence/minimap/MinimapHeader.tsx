import { eventLinks } from "../../../../../shared/fixtures/eventLinks";
import type { LabelTrackKey } from "../../../../../shared/fixtures/labelLayers";
import { labelTrackMeta, minimapTrackFilterKeys } from "../../../fixtures/evidenceFixtures";
import type { AnnotationMinimapController } from "./conversationBoundaryActions";
import { SlidersHorizontal } from "lucide-react";

export function MinimapHeader({ controller }: { controller: AnnotationMinimapController }) {
  const { activeTrackKey, collapsed, eventAssociationsImported, hiddenTracks, layers, minimapFilteredEvents, mode, setActiveTrack, setCollapsed, setEventAssociationsImported, setHiddenTracks, setLayers, setMode } = controller;
  return (
    <div className="mm-top">
            <div className="lf">
              <span className="pl" />
              <span>全天 Minimap · 24h Bird's-eye</span>
              <div className="mm-mode" role="group" aria-label="全天视图模式切换">
                {[
                  ["day", "全天"],
                  ["segmented", "已切分"],
                  ["receipt", "事件匹配"]
                ].map(([key, label]) => (
                  <button key={key} className={mode === key ? "on" : ""} onClick={() => setMode(key as typeof mode)}>
                    {label}
                  </button>
                ))}
              </div>
              <div className="mm-track-sync" aria-label="事件匹配与标签轨道联动">
                <span className="mm-track-sync-title">
                  <SlidersHorizontal size={12} />
                  轨道
                </span>
                <select
                  className="mm-track-select"
                  value={activeTrackKey ?? "entity"}
                  onChange={(event) => {
                    const nextTrack = event.target.value as LabelTrackKey;
                    if (hiddenTracks[nextTrack]) {
                      setHiddenTracks((current) => ({ ...current, [nextTrack]: false }));
                    }
                    setActiveTrack(nextTrack);
                    setMode("receipt");
                  }}
                  aria-label="选择联动标签轨道"
                >
                  {labelTrackMeta.map((track) => (
                    <option
                      key={track.key}
                      value={track.key}
                    >
                      {hiddenTracks[track.key] ? `${track.label}（隐藏）` : track.label}
                    </option>
                  ))}
                </select>
                <b>{minimapFilteredEvents.length}/{eventLinks.length}</b>
              </div>
            </div>
            <div className="rt">
              {[
                { key: "energy", label: "能量", sw: "sw-ac", available: !hiddenTracks.vad },
                { key: "sop", label: "SOP事件", sw: "sw-am", available: !hiddenTracks.intent || !hiddenTracks.agent },
                { key: "overlap", label: "多源同录", sw: "sw-overlap", available: !hiddenTracks.cross },
                { key: "biz", label: "业务事件", sw: "sw-biz-event", available: minimapTrackFilterKeys.some((track) => !hiddenTracks[track]) },
                { key: "orphan", label: "孤立 / 漏单", sw: "sw-ro", available: !hiddenTracks.doc || !hiddenTracks.qa }
              ].map(({ key, label, sw, available }) => (
                <button
                  key={key}
                  className={[layers[key] && available ? "ki on" : "ki", available ? "" : "muted"].join(" ")}
                  onClick={() => setLayers((current) => ({ ...current, [key]: !current[key] }))}
                  aria-pressed={layers[key]}
                  title={available ? "切换 minimap 图层" : "对应标签轨道已隐藏，先恢复轨道"}
                >
                  <span className={`sw ${sw}`} />
                  {label}
                </button>
              ))}
              <button
                className={eventAssociationsImported ? "ki on import-events" : "ki import-events"}
                onClick={() => {
                  setEventAssociationsImported((current) => !current);
                  setMode("receipt");
                }}
                aria-pressed={eventAssociationsImported}
              >
                <span className="sw sw-link" />
                {eventAssociationsImported ? `已导入关联事件 ${eventLinks.length}` : "导入关联事件"}
              </button>
              <button className="mm-collapse" onClick={() => setCollapsed(!collapsed)}>
                {collapsed ? "▸ 展开" : "▾ 折叠"}
              </button>
            </div>
          </div>
  );
}
