import { segmentBars, trackSegments } from "../../../fixtures/evidenceFixtures";
import type { WaveformPanelController } from "./trackRegionModalActions";

export function WaveformOverview({ controller }: { controller: WaveformPanelController }) {
  const { activeSegment, activeTrackMeta, draftAnnotation, setActiveSegmentIndex, setDraftAnnotation, showWaveform } = controller;
  return (
    showWaveform && (
          <section className="wv">
            <div className="wv-ov">
              <span className="ki">
                <span className="sw sw-ch-l" />
                声道 L
              </span>
              <span className="ki">
                <span className="sw sw-ch-r" />
                声道 R
              </span>
              <span className="c-ac">SNR 23.8dB</span>
            </div>
            <div className="wv-grid">
              <div className="wv-lab has-sel">
                <div className="ti">音频波形</div>
                <div className="meta">136 px/sec</div>
                <div className="sel-id">{activeSegment.label}</div>
                <div className="sel-time">{activeSegment.time}</div>
                <div className="sel-tx">当前标注目标：{activeTrackMeta.label} · {draftAnnotation || "等待输入"}</div>
                <button className="clr" onClick={() => setDraftAnnotation("")}>清空草稿</button>
              </div>
              <div className="wv-scroll">
                <div className="waveform an-wave">
                  {segmentBars.map((height, i) => (
                    <button
                      key={i}
                      style={{ height: `${height}%` }}
                      className={i > 38 && i < 49 ? "hot" : i % 7 === 0 ? "right" : ""}
                      onClick={() => setActiveSegmentIndex(Math.min(trackSegments.length - 1, Math.floor(i / 19)))}
                      aria-label={`波形位置 ${i}`}
                    />
                  ))}
                  <div className="playhead">
                    <span>{activeSegment.time}</span>
                  </div>
                </div>
                <div className="tline">
                  {["12:23", "12:25", "12:27", "12:29", "12:31", "12:33"].map((time) => (
                    <span key={time}>{time}</span>
                  ))}
                </div>
              </div>
            </div>
          </section>
          )
  );
}
