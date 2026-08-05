import { useEffect, useRef, useState } from "react";

import {
  createAudioPlaybackGrant,
  getAudioSession,
  type AudioSessionDetail
} from "../../../api/client";

const value = (input: unknown) =>
  typeof input === "string" && input.trim() ? input : "未提供";

export function AudioImportSessionPanel({
  audioSessionId,
  onClose
}: {
  audioSessionId: string;
  onClose: () => void;
}) {
  const [session, setSession] = useState<AudioSessionDetail | null>(null);
  const [state, setState] = useState("loading");
  const [detail, setDetail] = useState("正在回读新音频会话。");
  const [playbackUrl, setPlaybackUrl] = useState("");
  const [playbackPending, setPlaybackPending] = useState(false);
  const [playing, setPlaying] = useState(false);
  const audioRef = useRef<HTMLAudioElement>(null);

  const load = async (active = () => true) => {
    setState("loading");
    try {
      const response = await getAudioSession(audioSessionId);
      if (!active()) return;
      setSession(response.data);
      setState("ready");
      setDetail(`会话 ${audioSessionId} 已回读。`);
    } catch (error) {
      if (!active()) return;
      setState("error");
      setDetail(error instanceof Error ? error.message : "会话读取失败");
    }
  };

  useEffect(() => {
    let active = true;
    setPlaybackUrl("");
    setPlaying(false);
    void load(() => active);
    return () => {
      active = false;
      audioRef.current?.pause();
    };
  }, [audioSessionId]);

  const togglePlayback = async () => {
    const audio = audioRef.current;
    if (audio && !audio.paused) return audio.pause();
    setPlaybackPending(true);
    try {
      if (!audio) throw new Error("播放器尚未就绪");
      const nextUrl = playbackUrl
        || (await createAudioPlaybackGrant(audioSessionId)).data.playback_url;
      if (!nextUrl) throw new Error("播放授权无可用地址");
      if (!playbackUrl) {
        audio.src = nextUrl;
        setPlaybackUrl(nextUrl);
        audio.load();
      }
      try {
        await audio.play();
      } catch (error) {
        setPlaying(false);
        setDetail(
          error instanceof Error
            ? `录音已加载，浏览器未开始播放：${error.message}`
            : "录音已加载，请再次点击播放。"
        );
      }
    } catch (error) {
      audio?.pause();
      audio?.removeAttribute("src");
      audio?.load();
      setPlaybackUrl("");
      setPlaying(false);
      setDetail(error instanceof Error ? error.message : "播放授权失败");
    } finally {
      setPlaybackPending(false);
    }
  };

  const facts: Array<[string, unknown]> | null = session ? [
    ["状态", session.status],
    ["录音 ID", session.recording_id],
    ["平台连接", session.platform_connection_id],
    ["导入批次", session.import_batch_id],
    ["根 Trace", session.root_trace_id ?? session.trace_id]
  ] : null;
  return (
    <section className="audio-import-session-panel" aria-label="新音频会话详情">
      <header>
        <div><span>新音频会话</span><strong>{audioSessionId}</strong></div>
        <button type="button" aria-label="关闭会话详情" onClick={onClose}>×</button>
      </header>
      <div className={`audio-import-session-readback is-${state}`} role="status">
        <span>{detail}</span>
        {state === "error" && <button type="button" onClick={() => void load()}>重试回读</button>}
      </div>
      {facts && <dl>{facts.map(([label, item]) => (
        <div key={label}><dt>{label}</dt><dd>{value(item)}</dd></div>
      ))}</dl>}
      <div className="audio-import-playback">
        <audio
          ref={audioRef}
          src={playbackUrl || undefined}
          preload="none"
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
          onError={() => {
            if (playbackUrl) {
              setPlaybackUrl("");
              setPlaying(false);
              setDetail("录音加载失败，请重新授权。");
            }
          }}
        />
        <button
          type="button"
          disabled={state !== "ready" || playbackPending}
          onClick={() => void togglePlayback()}
        >{playbackPending ? "获取授权中" : playing ? "暂停" : "播放录音"}</button>
        <span>播放地址仅短期进入播放器，不写入配置或存储。</span>
      </div>
    </section>
  );
}
