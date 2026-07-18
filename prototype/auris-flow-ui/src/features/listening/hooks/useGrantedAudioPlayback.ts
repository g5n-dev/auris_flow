import { createAudioPlaybackGrant } from "../../../api/client";
import { useEffect, useRef, useState } from "react";

export type AudioPlaybackStatus = "idle" | "pending" | "failed";

export function useGrantedAudioPlayback(audioSessionId: string, setPlaying: (playing: boolean) => void) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const playbackUrlRef = useRef("");
  const playbackExpiresAtRef = useRef(0);
  const requestPendingRef = useRef(false);
  const requestGenerationRef = useRef(0);
  const [playbackStatus, setPlaybackStatus] = useState<AudioPlaybackStatus>("idle");
  const [playbackMessage, setPlaybackMessage] = useState("");

  const clearPlaybackSource = () => {
    playbackUrlRef.current = "";
    playbackExpiresAtRef.current = 0;
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      const hadSource = audio.hasAttribute("src");
      audio.removeAttribute("src");
      if (hadSource) audio.load();
    }
  };

  useEffect(() => {
    requestGenerationRef.current += 1;
    requestPendingRef.current = false;
    clearPlaybackSource();
    setPlaying(false);
    setPlaybackStatus("idle");
    setPlaybackMessage("");
    return () => {
      requestGenerationRef.current += 1;
      requestPendingRef.current = false;
      audioRef.current?.pause();
    };
  }, [audioSessionId, setPlaying]);

  const togglePlayback = async (rate = 1) => {
    const audio = audioRef.current;
    if (!audio || requestPendingRef.current) return;
    if (!audio.paused) {
      audio.pause();
      return;
    }

    const generation = requestGenerationRef.current;
    const grantIsReusable =
      Boolean(playbackUrlRef.current) && playbackExpiresAtRef.current > Date.now() + 5000;
    requestPendingRef.current = true;
    setPlaybackStatus("pending");
    setPlaybackMessage(grantIsReusable ? "正在准备播放" : "正在获取播放授权");

    try {
      if (!grantIsReusable) {
        const response = await createAudioPlaybackGrant(audioSessionId);
        if (generation !== requestGenerationRef.current) return;
        const nextUrl = response.data.playback_url;
        const nextExpiresAt = Date.parse(response.data.expires_at);
        if (!nextUrl || !Number.isFinite(nextExpiresAt)) {
          throw new Error("播放授权响应不完整");
        }
        playbackUrlRef.current = nextUrl;
        playbackExpiresAtRef.current = nextExpiresAt;
        audio.src = nextUrl;
        audio.load();
      }
      audio.playbackRate = rate;
      await audio.play();
      if (generation !== requestGenerationRef.current) return;
      setPlaybackStatus("idle");
      setPlaybackMessage("");
    } catch (error) {
      if (generation !== requestGenerationRef.current) return;
      clearPlaybackSource();
      setPlaying(false);
      setPlaybackStatus("failed");
      setPlaybackMessage(error instanceof Error && error.message ? error.message : "播放授权或录音加载失败");
    } finally {
      if (generation === requestGenerationRef.current) requestPendingRef.current = false;
    }
  };

  const seekBy = (seconds: number) => {
    const audio = audioRef.current;
    if (!audio || !playbackUrlRef.current) return;
    const duration = Number.isFinite(audio.duration) ? audio.duration : Number.POSITIVE_INFINITY;
    audio.currentTime = Math.max(0, Math.min(duration, audio.currentTime + seconds));
  };

  const handlePlaybackError = () => {
    if (!playbackUrlRef.current) return;
    requestPendingRef.current = false;
    clearPlaybackSource();
    setPlaying(false);
    setPlaybackStatus("failed");
    setPlaybackMessage("录音加载失败，请重试");
  };

  return {
    audioRef,
    playbackStatus,
    playbackMessage,
    playbackPending: playbackStatus === "pending",
    togglePlayback,
    seekBy,
    handlePlaybackError
  };
}
