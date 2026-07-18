import { annotationIslands, lanes, listeningDeviceBadges, quickChips } from "../../fixtures/evidenceFixtures";
import type { ReviewSample } from "../../fixtures/reviewSamples";
import { useGrantedAudioPlayback } from "../../hooks/useGrantedAudioPlayback";
import type { ListeningDeviceKey } from "../../types";
import { Pause, Play, Plus, RotateCcw, SkipBack, SkipForward } from "lucide-react";
import { useRef, useState } from "react";

export function AnnotationToolbar({ sample }: { sample: ReviewSample }) {
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState("1.0");
  const [mode, setMode] = useState<"annotate" | "listen">("annotate");
  const [zoom, setZoom] = useState(100);
  const {
    audioRef,
    playbackStatus,
    playbackMessage,
    playbackPending,
    togglePlayback,
    seekBy,
    handlePlaybackError
  } = useGrantedAudioPlayback(sample.sessionId, setPlaying);
  const dbWaveBars = [14, 22, 18, 31, 42, 36, 54, 63, 48, 28, 16, 20, 39, 58, 71, 66, 44, 25, 19, 34, 52, 76, 83, 69, 38, 22, 17, 29, 49, 62, 58, 41, 24, 18, 32, 55, 73, 88, 79, 61, 36, 21, 15, 12];
  const jumpToAnnotationEditor = () => {
    setMode("annotate");
    window.requestAnimationFrame(() => {
      const input = document.querySelector<HTMLInputElement>(".tk-operate input");
      input?.scrollIntoView({ block: "center", inline: "nearest" });
      input?.focus({ preventScroll: true });
    });
  };

  return (
    <div className="atl" data-mode={mode}>
      <audio
        ref={audioRef}
        preload="none"
        data-testid="listening-recording"
        data-audio-session-id={sample.sessionId}
        aria-label={`${sample.sessionId} 录音`}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onError={handlePlaybackError}
      />
      <div className="atg">
        <button className="atb-bn" onClick={() => seekBy(-30)} title="后退 30s">
          <SkipBack size={14} />
          30
        </button>
        <button className="atb-bn" onClick={() => seekBy(-5)} title="后退 5s">
          <SkipBack size={14} />
          5
        </button>
        <button
          className={playing ? "atb-bn pl active" : playbackPending ? "atb-bn pl pending" : "atb-bn pl"}
          onClick={() => void togglePlayback(Number(rate))}
          disabled={playbackPending}
          title={playbackPending ? playbackMessage : playbackStatus === "failed" ? `播放失败：${playbackMessage}` : "播放/暂停"}
        >
          {playing ? <Pause size={14} /> : <Play size={14} />}
        </button>
        <button className="atb-bn" onClick={() => seekBy(5)} title="前进 5s">
          5
          <SkipForward size={14} />
        </button>
        <button className="atb-bn" onClick={() => seekBy(30)} title="前进 30s">
          30
          <SkipForward size={14} />
        </button>
      </div>
      <div className="atg">
        <span className="atm">{sample.activeTime}</span>
        <span className="atm sl">/</span>
        <span className="atm fnt">{sample.sessionEnd}</span>
        {playbackStatus !== "idle" && (
          <span
            className={`atm fnt audio-playback-status is-${playbackStatus}`}
            role="status"
            title={playbackMessage}
          >
            {playbackPending ? playbackMessage : "播放失败，可重试"}
          </span>
        )}
      </div>
      <div className="toolbar-db-strip" aria-label="当前窗口 dB 波形和截断参考">
        <div className="toolbar-db-meta">
          <span>窗口 dB</span>
          <strong>-12 / -36 / -60</strong>
        </div>
        <div className="toolbar-db-wave">
          <span className="db-grid db-top" />
          <span className="db-grid db-mid" />
          <span className="db-grid db-low" />
          <span className="db-boundary start"><i>截入</i></span>
          <span className="db-boundary end"><i>截出</i></span>
          <span className="db-playhead" />
          {dbWaveBars.map((bar, index) => (
            <i
              key={`${bar}-${index}`}
              className={index < 4 || index > dbWaveBars.length - 5 ? "quiet" : bar > 72 ? "hot" : ""}
              style={{ height: `${bar}%` }}
            />
          ))}
        </div>
      </div>
      <div className="atg">
        <span className="atm fnt">速率</span>
        <select
          className="atb-in"
          value={rate}
          onChange={(event) => {
            setRate(event.target.value);
            if (audioRef.current) audioRef.current.playbackRate = Number(event.target.value);
          }}
          aria-label="播放速率"
        >
          {["0.5", "0.75", "1.0", "1.25", "1.5", "2.0"].map((item) => (
            <option key={item} value={item}>
              {item}x
            </option>
          ))}
        </select>
      </div>
      <div className="atg">
        <button className="atb-bn" onClick={() => setZoom(Math.min(180, zoom + 10))} title="放大轨道">
          +
        </button>
        <button className="atb-bn" onClick={() => setZoom(Math.max(70, zoom - 10))} title="缩小轨道">
          -
        </button>
        <button className="atb-bn" onClick={() => setZoom(100)} title="重置会话">
          <RotateCcw size={13} />
        </button>
      </div>
      <div className="mode-toggle-group">
        <button className={mode === "annotate" ? "active" : ""} onClick={() => setMode("annotate")}>
          标注
        </button>
        <button className={mode === "listen" ? "active" : ""} onClick={() => setMode("listen")}>
          调听
        </button>
      </div>
      <button className="atb-bn annotation-entry-button" onClick={jumpToAnnotationEditor}>
        <Plus size={13} />
        创建标注
      </button>
      <div className="atk">
        <span className="fl">{sample.file}</span>
        <span className="mt">zoom {zoom}% · 标签 v1.8.4 · ASR v2.3.1</span>
        <span className="mt last-saved">已保存 12:31:08</span>
      </div>
    </div>
  );
}

export function BadgeBar({
  activeDeviceKey,
  onSelectDevice,
  activeDevice
}: {
  activeDeviceKey: ListeningDeviceKey;
  onSelectDevice: (key: ListeningDeviceKey) => void;
  activeDevice: (typeof listeningDeviceBadges)[number];
}) {
  return (
    <div className="bdg-bar">
      <span className="ti">设备焦点</span>
      <div className="chips">
        {listeningDeviceBadges.map((badge) => (
          <button
            key={badge.key}
            className={[
              "bdg-chip",
              badge.key === activeDeviceKey ? "active" : "",
              badge.key === "B-2001" || badge.key === "Hall-Mic" ? "warn" : ""
            ].join(" ")}
            aria-pressed={badge.key === activeDeviceKey}
            onClick={() => onSelectDevice(badge.key)}
            title={`${badge.role} · 点击聚焦证据`}
          >
            <span className="dot" style={{ background: badge.color, color: badge.color }} />
            <span className="nm">{badge.name}</span>
            <span className="seg-ct">{badge.count}</span>
            <span className="bdg-src">{badge.src}</span>
            <span className="bdg-batt-txt">{badge.battery}</span>
          </button>
        ))}
      </div>
      <div className="bdg-meta">
        <span>
          当前 <b>{activeDevice.name}</b>
        </span>
        <span className={activeDevice.mark === "crosstalk" ? "alert" : ""}>{activeDevice.summary}</span>
      </div>
    </div>
  );
}

export function IslandBar({
  activeIsland,
  setActiveIsland,
  selectedWindow,
  setSelectedWindow
}: {
  activeIsland: string;
  setActiveIsland: (value: string) => void;
  selectedWindow: string;
  setSelectedWindow: (value: string) => void;
}) {
  const [filter, setFilter] = useState("all");
  const currentIndex = Math.max(0, annotationIslands.findIndex((item) => item.id === activeIsland));
  const go = (direction: -1 | 1) => {
    const next = annotationIslands[Math.max(0, Math.min(annotationIslands.length - 1, currentIndex + direction))];
    setActiveIsland(next.id);
    setSelectedWindow(next.id === "S129" ? "12:23 - 12:33" : `${next.time} - ${next.time.replace(/:(\d+)/, (_, minute) => `:${String(Number(minute) + 8).padStart(2, "0")}`)}`);
  };

  return (
    <div className="ib">
      <div className="ib-l">
        <div>
          <div className="ti">会话 · 当前小时</div>
          <div className="ct">{selectedWindow}</div>
        </div>
        <div className="ib-nav">
          <button onClick={() => go(-1)} aria-label="上一个待审会话">
            ‹
          </button>
          <button onClick={() => go(1)} aria-label="下一个待审会话">
            ›
          </button>
        </div>
      </div>
      <div className="ib-fil" role="group" aria-label="会话筛选">
        {[
          ["all", "全部 128"],
          ["pen", "待审 24"],
          ["low", "低置信 18"],
          ["rev", "已审 71"],
          ["skp", "跳过 6"]
        ].map(([key, label]) => (
          <button key={key} className={filter === key ? "ib-pill on" : "ib-pill"} onClick={() => setFilter(key)}>
            {label}
          </button>
        ))}
      </div>
      <div className="ib-stream">
        {annotationIslands.map((item) => (
          <button
            key={item.id}
            className={`iv ${item.status} ${activeIsland === item.id ? "cur" : ""}`}
            onClick={() => {
              setActiveIsland(item.id);
              setSelectedWindow(item.id === "S129" ? "12:23 - 12:33" : `${item.time} - ${item.time.slice(0, 3)}${String(Number(item.time.slice(3)) + 8).padStart(2, "0")}`);
            }}
          >
            <span className="st" />
            <span className="h">
              <span className="tm">{item.time}</span>
              <span className="id">{item.id}</span>
            </span>
            <span className="b">{item.note}</span>
            <span className="mn">
              {item.bars.map((height, index) => (
                <i key={index} style={{ height: `${height}%` }} />
              ))}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

export function SlidingChipRail({
  activeChip,
  setActiveChip
}: {
  activeChip: string;
  setActiveChip: (value: string) => void;
}) {
  const railRef = useRef<HTMLDivElement>(null);

  const slide = (direction: "left" | "right") => {
    railRef.current?.scrollBy({
      left: direction === "left" ? -220 : 220,
      behavior: "smooth"
    });
  };

  return (
    <section className="chip-rail-wrap" aria-label="滑动筛选 Chip">
      <button className="chip-arrow" onClick={() => slide("left")} aria-label="向左滑动 Chip">
        ‹
      </button>
      <div className="chip-rail" ref={railRef}>
        {quickChips.map((chip) => (
          <button
            key={chip.label}
            className={`filter-chip ${chip.tone} ${activeChip === chip.label ? "active" : ""}`}
            onClick={() => setActiveChip(chip.label)}
          >
            <span>{chip.label}</span>
            <b>{chip.count}</b>
          </button>
        ))}
      </div>
      <button className="chip-arrow" onClick={() => slide("right")} aria-label="向右滑动 Chip">
        ›
      </button>
    </section>
  );
}

export function StatusRail({ selectedLabel, agentState }: { selectedLabel: string; agentState: "pending" | "accepted" | "rejected" }) {
  return (
    <div className="status-rail">
      <div>
        <span>状态</span>
        <strong>{selectedLabel}</strong>
      </div>
      <div>
        <span>Agent</span>
        <strong className={agentState === "accepted" ? "ok" : agentState === "rejected" ? "bad" : "warn"}>
          {agentState === "accepted" ? "已接受" : agentState === "rejected" ? "已拒绝" : "待确认"}
        </strong>
      </div>
      <div>
        <span>当前窗口</span>
        <strong>12:23-12:33</strong>
      </div>
      <div>
        <span>主录音</span>
        <strong>销售A工牌</strong>
      </div>
    </div>
  );
}

export function Minimap({
  selectedWindow,
  setSelectedWindow,
  compact = false
}: {
  selectedWindow: string;
  setSelectedWindow: (value: string) => void;
  compact?: boolean;
}) {
  return (
    <section className={compact ? "minimap compact" : "minimap"}>
      <div className="section-head">
        <div>
          <span>同日同店音频 Minimap</span>
          <strong>{selectedWindow}</strong>
        </div>
        <div className="legend">
          <span className="legend-item main">主录音</span>
          <span className="legend-item cross">串音</span>
          <span className="legend-item dup">重复</span>
          <span className="legend-item doc">单据</span>
        </div>
      </div>
      <div className="time-scale">
        {["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "18:00"].map((time) => (
          <span key={time}>{time}</span>
        ))}
      </div>
      <div className="lane-stack">
        {lanes.map((lane, laneIndex) => (
          <button
            className="lane-row"
            key={lane.name}
            onClick={() => setSelectedWindow(laneIndex % 2 === 0 ? "12:23 - 12:33" : "12:18 - 12:29")}
          >
            <div className="lane-label">
              <strong>{lane.name}</strong>
              <span>{lane.sub}</span>
            </div>
            <div className="lane-track">
              {Array.from({ length: 54 }, (_, i) => {
                const isMain = laneIndex === 0 && i > 20 && i < 27;
                const isCross = (laneIndex === 1 && i > 21 && i < 28) || (laneIndex === 2 && i > 23 && i < 31);
                const isDoc = (i === 12 && laneIndex === 2) || (i === 44 && laneIndex === 0);
                const isGap = i > 48;
                return (
                  <span
                    key={i}
                    className={[
                      "lane-cell",
                      lane.hue,
                      isMain ? "main" : "",
                      isCross ? "cross" : "",
                      isDoc ? "doc" : "",
                      isGap ? "gap" : ""
                    ].join(" ")}
                    style={{ opacity: 0.25 + (((i * 17 + laneIndex * 11) % 70) / 100) }}
                  />
                );
              })}
              {laneIndex === 0 && <span className="selection-window" />}
              {laneIndex === 1 && <span className="cross-link link-one" />}
              {laneIndex === 2 && <span className="cross-link link-two" />}
            </div>
          </button>
        ))}
      </div>
    </section>
  );
}
