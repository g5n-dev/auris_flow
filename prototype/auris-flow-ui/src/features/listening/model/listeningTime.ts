export const clockToSeconds = (time: string) => {
  const [hours, minutes, seconds] = time.split(":").map(Number);
  return hours * 3600 + minutes * 60 + seconds;
};

export const formatSeconds = (seconds: number) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;

export const secondsToClock = (seconds: number) => {
  const normalized = Math.max(0, Math.round(seconds));
  const hh = String(Math.floor(normalized / 3600) % 24).padStart(2, "0");
  const mm = String(Math.floor((normalized % 3600) / 60)).padStart(2, "0");
  const ss = String(normalized % 60).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
};

export const normalizeClockText = (value: string) => {
  const parts = value.trim().split(":");
  if (parts.length === 2) return `${parts[0]}:${parts[1]}:00`;
  return value.trim();
};

export const extractClockFromText = (value: string) => {
  const match = value.match(/\d{2}:\d{2}(?::\d{2})?/);
  return match ? normalizeClockText(match[0]) : "";
};

export const parseClockWindow = (value: string) => {
  const [rawStart = "", rawEnd = rawStart] = value.split(/\s*[-–]\s*/);
  const start = extractClockFromText(rawStart);
  const end = extractClockFromText(rawEnd) || start;
  const startSeconds = start ? clockToSeconds(start) : 0;
  const endSeconds = end ? clockToSeconds(end) : startSeconds;
  return { start, end, startSeconds, endSeconds };
};

export const formatSignedSeconds = (seconds: number) => `${seconds > 0 ? "+" : seconds < 0 ? "-" : ""}${Math.abs(Math.round(seconds))}s`;

export const describeTimeDrift = (seconds: number) => {
  const abs = Math.abs(Math.round(seconds));
  const readable = abs >= 60 ? `${Math.floor(abs / 60)}m${String(abs % 60).padStart(2, "0")}s` : `${abs}s`;
  if (seconds < 0) return `音频早于事件 ${readable}`;
  if (seconds > 0) return `音频晚于事件 ${readable}`;
  return "事件时间与音频起点一致";
};
