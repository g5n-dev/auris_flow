import { simpleTurns } from "../../../../shared/fixtures/listeningSamples";
import { simpleWaveBars } from "../../fixtures/evidenceFixtures";
import type { ReviewSample } from "../../fixtures/reviewSamples";
import { useGrantedAudioPlayback } from "../../hooks/useGrantedAudioPlayback";
import { Download, Pause, Play, SkipBack, SkipForward } from "lucide-react";
import type { CSSProperties } from "react";

export function SimpleAudioPlayer({
  playing,
  setPlaying,
  progress,
  activeTurnIndex,
  setActiveTurnIndex,
  sample
}: {
  playing: boolean;
  setPlaying: (value: boolean) => void;
  progress: number;
  activeTurnIndex: number;
  setActiveTurnIndex: (value: number) => void;
  sample: ReviewSample;
}) {
  const {
    audioRef,
    playbackStatus,
    playbackMessage,
    playbackPending,
    togglePlayback,
    seekBy,
    handlePlaybackError
  } = useGrantedAudioPlayback(sample.sessionId, setPlaying);
  const clampTurn = (index: number) => Math.max(0, Math.min(simpleTurns.length - 1, index));
  return (
    <div className="simple-audio-bar" style={{ "--played": `${progress}%` } as CSSProperties}>
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
      <button
        className={playing ? "simple-play is-playing" : playbackPending ? "simple-play is-pending" : "simple-play"}
        onClick={() => void togglePlayback()}
        disabled={playbackPending}
        title={playbackPending ? playbackMessage : playbackStatus === "failed" ? `播放失败：${playbackMessage}` : "播放/暂停"}
      >
        {playing ? <Pause size={13} /> : <Play size={13} />}
      </button>
      <div className="simple-time">
        <strong>{simpleTurns[activeTurnIndex].time.slice(3)}</strong>
        <span>/ 02:42</span>
        {playbackStatus !== "idle" && (
          <em className={`audio-playback-status is-${playbackStatus}`} role="status" title={playbackMessage}>
            {playbackPending ? playbackMessage : "播放失败，可重试"}
          </em>
        )}
      </div>
      <div className="simple-wave" onClick={() => setActiveTurnIndex(clampTurn(activeTurnIndex + 1))}>
        <div className="simple-wave-bars">
          {simpleWaveBars.map((bar, index) => (
            <span key={index} className={bar.role} style={{ height: `${bar.level}%` }} />
          ))}
        </div>
        <div className="simple-wave-played">
          {simpleWaveBars.map((bar, index) => (
            <span key={index} className={bar.role} style={{ height: `${bar.level}%` }} />
          ))}
        </div>
        <div className="simple-wave-segs">
          {simpleTurns.map((turn, index) => (
            <button
              key={turn.eventId}
              className={index === activeTurnIndex ? "active" : ""}
              style={{ left: `${12 + index * 18}%` }}
              onClick={(event) => {
                event.stopPropagation();
                setActiveTurnIndex(index);
              }}
              aria-label={`跳转到 ${turn.time}`}
            />
          ))}
        </div>
        <div className="simple-wave-head" />
      </div>
      <button className="simple-speed">1.0x</button>
      <button
        className="simple-audio-icon"
        onClick={() => {
          seekBy(-5);
          setActiveTurnIndex(clampTurn(activeTurnIndex - 1));
        }}
      >
        <SkipBack size={15} />
      </button>
      <button
        className="simple-audio-icon"
        onClick={() => {
          seekBy(5);
          setActiveTurnIndex(clampTurn(activeTurnIndex + 1));
        }}
      >
        <SkipForward size={15} />
      </button>
      <button className="simple-audio-icon">
        <Download size={15} />
      </button>
    </div>
  );
}
