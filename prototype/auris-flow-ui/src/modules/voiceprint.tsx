import { BookOpen, Download, GitBranch, Headphones, Link2, ListFilter, Play, Plus, Radio, ShieldCheck } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import {
  apiRequest,
  normalizeActionReceipt,
  stableIdempotencyKey,
  type ApiEnvelope,
  type BackendActionReceipt
} from "../api/client";
import type { VoiceprintRecord } from "../shared/contracts/voiceprint";

export type { VoiceprintRecord } from "../shared/contracts/voiceprint";

type VoiceprintDimensionBand = {
  key: string;
  label: string;
  range: string;
  dims: number;
  active: number;
  weight: number;
  drift: number;
  quality: number;
  tone: "blue" | "teal" | "amber" | "violet" | "red";
};

type VoiceprintProjectionPoint = {
  label: string;
  x: number;
  y: number;
  score: number;
  tone: "sample" | "center" | "risk";
};

export type VoiceprintFlowKey = "identity" | "capture" | "quality" | "fusion" | "submit";
type EnrollmentFeedback = {
  status: "idle" | "pending" | "success" | "error";
  title: string;
  detail: string;
};

type VoiceprintEnrollmentPayload = {
  enrollment_id?: string;
  voiceprint_id: string;
  quality?: Record<string, number>;
  consistency?: Record<string, number>;
  [key: string]: unknown;
};

async function submitVoiceprintEnrollment(
  payload: VoiceprintEnrollmentPayload
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>("/v1/voiceprint-enrollments", {
    method: "POST",
    headers: {
      "Idempotency-Key": stableIdempotencyKey(`voiceprint_enrollment_${payload.voiceprint_id}`, payload)
    },
    body: JSON.stringify(payload)
  });
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

type VoiceprintGateCheck = {
  key: string;
  label: string;
  value: string;
  threshold: string;
  score: number;
  passed: boolean;
  detail: string;
};

export function getVoiceprintGate(record: VoiceprintRecord) {
  const minConsistency = Math.min(record.consistency.ab, record.consistency.ac, record.consistency.bc);
  const checks: VoiceprintGateCheck[] = [
    {
      key: "duration",
      label: "有效时长",
      value: `${record.quality.duration}`,
      threshold: ">= 80",
      score: record.quality.duration,
      passed: record.quality.duration >= 80,
      detail: "VAD 有效人声长度满足建模窗口"
    },
    {
      key: "snr",
      label: "SNR",
      value: `${record.quality.snr}`,
      threshold: ">= 75",
      score: record.quality.snr,
      passed: record.quality.snr >= 75,
      detail: "背景噪声不会主导声纹特征"
    },
    {
      key: "purity",
      label: "纯净度",
      value: `${record.quality.purity}`,
      threshold: ">= 75",
      score: record.quality.purity,
      passed: record.quality.purity >= 75,
      detail: "重叠人声、串音和截断风险可控"
    },
    {
      key: "stability",
      label: "稳定性",
      value: `${record.quality.stability}`,
      threshold: ">= 80",
      score: record.quality.stability,
      passed: record.quality.stability >= 80,
      detail: "A/B/C 样本围绕同一模板中心聚合"
    },
    {
      key: "consistency",
      label: "最低一致性",
      value: minConsistency.toFixed(3),
      threshold: ">= 0.820",
      score: Math.round(minConsistency * 100),
      passed: minConsistency >= 0.82,
      detail: "三段样本两两 cosine 相似度满足入库阈值"
    }
  ];
  const canEnroll = record.quality.overall >= 85 && checks.every((check) => check.passed) && record.status === "可入库";
  return {
    checks,
    minConsistency,
    canEnroll,
    failedCount: checks.filter((check) => !check.passed).length,
    verdict: canEnroll ? "可提交入库" : record.status === "需复核" ? "建议复核" : "建议补采/绑定身份"
  };
}

export function getVoiceprintEmbeddingProfile(record: VoiceprintRecord): {
  totalDims: number;
  activeDims: number;
  compressionRatio: number;
  bands: VoiceprintDimensionBand[];
  projection: VoiceprintProjectionPoint[];
} {
  const totalDims = 512;
  const minConsistency = Math.min(record.consistency.ab, record.consistency.ac, record.consistency.bc);
  const dimGroups = [
    { key: "channel", label: "声道归一", range: "D001-D048", dims: 48, quality: record.quality.purity, weight: 0.12, tone: "blue" as const },
    { key: "f0", label: "基频/F0", range: "D049-D112", dims: 64, quality: record.quality.stability, weight: 0.15, tone: "teal" as const },
    { key: "formant", label: "共振峰", range: "D113-D208", dims: 96, quality: Math.round((record.quality.purity + record.quality.stability) / 2), weight: 0.2, tone: "violet" as const },
    { key: "timbre", label: "音色谱包络", range: "D209-D352", dims: 144, quality: record.quality.overall, weight: 0.28, tone: "blue" as const },
    { key: "rhythm", label: "能量节律", range: "D353-D432", dims: 80, quality: record.quality.duration, weight: 0.13, tone: "amber" as const },
    { key: "noise", label: "噪声/混响", range: "D433-D512", dims: 80, quality: record.quality.snr, weight: 0.12, tone: record.quality.snr >= 75 ? ("teal" as const) : ("red" as const) }
  ];
  const bands = dimGroups.map((group) => {
    const drift = Math.max(2, Math.round((100 - group.quality) * (group.key === "noise" ? 0.72 : 0.48)));
    return {
      ...group,
      active: Math.max(6, Math.min(group.dims, Math.round(group.dims * Math.max(0.32, group.quality / 100)))),
      drift
    };
  });
  const activeDims = bands.reduce((sum, band) => sum + band.active, 0);
  const spread = Math.max(6, Math.round((1 - minConsistency) * 120));
  const sampleScores = record.samples.map((sample) => sample[3]);
  const projection: VoiceprintProjectionPoint[] = [
    {
      label: "模板中心",
      x: 50,
      y: 50,
      score: record.quality.overall,
      tone: "center"
    },
    {
      label: "A",
      x: Math.max(16, Math.min(84, 46 - spread * 0.25 + (sampleScores[0] - 80) * 0.24)),
      y: Math.max(18, Math.min(82, 48 - (record.consistency.ab - 0.78) * 34)),
      score: sampleScores[0] ?? record.quality.overall,
      tone: "sample"
    },
    {
      label: "B",
      x: Math.max(16, Math.min(84, 53 + spread * 0.18 + (sampleScores[1] - 80) * 0.18)),
      y: Math.max(18, Math.min(82, 45 + (1 - record.consistency.bc) * 70)),
      score: sampleScores[1] ?? record.quality.overall,
      tone: "sample"
    },
    {
      label: "C",
      x: Math.max(16, Math.min(84, 50 + (1 - record.consistency.ac) * 58)),
      y: Math.max(18, Math.min(82, 56 + spread * 0.26)),
      score: sampleScores[2] ?? record.quality.overall,
      tone: minConsistency < 0.78 ? "risk" : "sample"
    }
  ];
  return {
    totalDims,
    activeDims,
    compressionRatio: Math.round((activeDims / totalDims) * 100),
    bands,
    projection
  };
}

const voiceprintFlowCopy: Record<VoiceprintFlowKey, { title: string; subtitle: string; output: string }> = {
  identity: {
    title: "身份确认",
    subtitle: "确认 employee_id、speaker_id、门店、设备和授权状态，避免模板错绑。",
    output: "人员实体 ↔ speaker_id 绑定关系"
  },
  capture: {
    title: "样本采集",
    subtitle: "采集固定文本、随机短句和自由说话三类样本，覆盖稳定性与真实业务变化。",
    output: "A/B/C 三段 wav 证据"
  },
  quality: {
    title: "质量检测",
    subtitle: "只判断音频能不能建模，不直接判断身份；核心看有效时长、SNR、纯净度和重叠风险。",
    output: "通过 / 复核 / 补采 Gate"
  },
  fusion: {
    title: "模板融合",
    subtitle: "检查 A/B/C 一致性后按质量和偏离程度加权生成融合模板。",
    output: "声纹模板版本和一致性证据"
  },
  submit: {
    title: "人工确认",
    subtitle: "入库是有状态变更的动作，必须记录操作者、版本、样本证据和确认时间。",
    output: "员工声纹基线 / 审计日志"
  }
};

export function VoiceprintEnrollmentStage({
  record,
  gate,
  selectedFlow,
  setSelectedFlow,
  submitted,
  submitting,
  receipt,
  submit
}: {
  record: VoiceprintRecord;
  gate: ReturnType<typeof getVoiceprintGate>;
  selectedFlow: VoiceprintFlowKey;
  setSelectedFlow: (flow: VoiceprintFlowKey) => void;
  submitted: boolean;
  submitting: boolean;
  receipt?: BackendActionReceipt;
  submit: () => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const animationSeedRef = useRef(0);
  const selectedFlowCopy = voiceprintFlowCopy[selectedFlow];
  const embeddingProfile = getVoiceprintEmbeddingProfile(record);
  const templateDimensions = embeddingProfile.totalDims;
  const activeDimensions = embeddingProfile.activeDims;
  const passedGateCount = gate.checks.filter((check) => check.passed).length;
  const templateDimensionLabel = `${templateDimensions}维`;
  const activeDimensionLabel = `${activeDimensions}维`;
  const qualityEnergyLabel = (Math.max(0.03, (100 - record.quality.snr) / 180)).toFixed(2);
  const receiptStatus = typeof receipt?.status === "string" ? receipt.status : "";
  const enrollStateLabel = receiptStatus === "enrolled" ? "已入库" : submitted ? "已提交复核" : gate.canEnroll ? "可入库" : gate.failedCount > 1 ? "需补采" : "待复核";
  const submitLabel = submitting
    ? "提交中"
    : receiptStatus === "enrolled"
    ? "已入库"
    : submitted
    ? "已提交复核"
    : gate.canEnroll
    ? "确认入库"
    : "质检未通过";
  const templateRiskLabel = gate.canEnroll ? "模板稳定 · 低风险" : gate.failedCount > 1 ? "模板波动 · 高风险" : "模板待复核 · 中风险";
  const stageCenterCopy = gate.canEnroll ? "样本可进入员工声纹基线，提交前仍需确认身份绑定和证据链。" : "样本存在质量或身份风险，需要补采、复核或修正绑定后再入库。";

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;
    const renderingContext = canvas.getContext("2d");
    if (!renderingContext) return undefined;
    const ctx = renderingContext;
    let raf = 0;
    let clock = 0;
    const seed = record.id.split("").reduce((sum, char) => sum + char.charCodeAt(0), 19);
    animationSeedRef.current = seed;
    const scoreFactor = Math.max(0.36, record.quality.overall / 100);
    const modePointCount = Math.max(76, Math.min(150, Math.round(embeddingProfile.activeDims / 3.4)));
    const modeAccent = "45, 118, 204";
    const modeWave = 0.92;
    const draw = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const width = Math.max(1, Math.floor(rect.width * dpr));
      const height = Math.max(1, Math.floor(rect.height * dpr));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const w = rect.width;
      const h = rect.height;
      clock += 0.006 + record.quality.stability / 32000;
      ctx.clearRect(0, 0, w, h);

      const centerX = w > 900 ? w * 0.42 : w * 0.5;
      const centerY = h * 0.42;
      const pulse = 1 + Math.sin(clock * 1.25) * 0.035 + (record.quality.overall - 70) / 900;
      const playback = (clock * 0.18) % 1;
      const playhead = playback * w;
      const energyPulse = 0.5 + Math.sin(clock * 8.2 + seed * 0.01) * 0.5;

      const softBg = ctx.createLinearGradient(0, 0, w, h);
      softBg.addColorStop(0, "rgba(248, 251, 255, 1)");
      softBg.addColorStop(0.48, "rgba(238, 247, 255, 1)");
      softBg.addColorStop(1, gate.canEnroll ? "rgba(239, 252, 247, 1)" : gate.failedCount > 1 ? "rgba(255, 241, 242, 1)" : "rgba(255, 247, 237, 1)");
      ctx.fillStyle = softBg;
      ctx.fillRect(0, 0, w, h);

      const centerGlow = ctx.createRadialGradient(centerX, centerY, 4, centerX, centerY, Math.min(w, h) * 0.5);
      centerGlow.addColorStop(0, `rgba(${modeAccent}, 0.18)`);
      centerGlow.addColorStop(0.28, "rgba(20, 184, 166, 0.11)");
      centerGlow.addColorStop(0.56, "rgba(22, 93, 255, 0.06)");
      centerGlow.addColorStop(1, "rgba(255, 255, 255, 0)");
      ctx.fillStyle = centerGlow;
      ctx.fillRect(0, 0, w, h);

      const networkPoints = Array.from({ length: modePointCount }, (_, index) => {
        const x = (((index * 73 + seed * 17) % 1000) / 1000) * w;
        const yBase = (((index * 47 + seed * 29) % 1000) / 1000);
        const y = h * (0.34 + yBase * 0.64) + Math.sin(clock * 0.62 + index) * 4;
        const size = 1.6 + ((index + seed) % 5) * 0.55;
        return { x, y, size, tone: index % 4 };
      });

      ctx.save();
      ctx.globalAlpha = 0.52;
      for (let index = 0; index < networkPoints.length; index += 1) {
        const point = networkPoints[index];
        const next = networkPoints[(index * 7 + 11) % networkPoints.length];
        const distance = Math.hypot(point.x - next.x, point.y - next.y);
        if (distance < Math.min(w, h) * 0.36) {
          ctx.strokeStyle = point.tone === 0 ? `rgba(${modeAccent}, 0.08)` : "rgba(56, 116, 190, 0.07)";
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(point.x, point.y);
          ctx.lineTo(next.x, next.y);
          ctx.stroke();
        }
      }

      networkPoints.forEach((point, index) => {
        const scan = Math.max(0, 1 - Math.abs(point.x - playhead) / Math.max(90, w * 0.15));
        ctx.fillStyle =
          point.tone === 0
            ? `rgba(${modeAccent}, ${0.1 + scan * 0.14})`
            : `rgba(20, 184, 166, ${0.08 + scan * 0.14})`;
        ctx.beginPath();
        ctx.arc(point.x, point.y, point.size + scan * 1.2 + Math.sin(clock + index) * 0.15, 0, Math.PI * 2);
        ctx.fill();
      });
      ctx.restore();

      ctx.save();
      const ringRadius = Math.min(w, h) * 0.33 * pulse;
      const ringStart = Math.PI * 0.62;
      const ringEnd = Math.PI * 2.35;
      const tickCount = 104;
      for (let index = 0; index < tickCount; index += 1) {
        const t = index / (tickCount - 1);
        const angle = ringStart + (ringEnd - ringStart) * t;
        const active = t <= record.quality.overall / 100;
        const inner = ringRadius + (active ? 0 : 1);
        const outer = ringRadius + (active ? 14 : 9);
        ctx.strokeStyle = active
          ? index % 3 === 0
            ? `rgba(${modeAccent}, 0.26)`
            : "rgba(20, 184, 166, 0.2)"
          : "rgba(122, 140, 170, 0.12)";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(centerX + Math.cos(angle) * inner * 1.04, centerY + Math.sin(angle) * inner);
        ctx.lineTo(centerX + Math.cos(angle) * outer * 1.04, centerY + Math.sin(angle) * outer);
        ctx.stroke();
      }

      ctx.strokeStyle = `rgba(${modeAccent}, 0.18)`;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(centerX, centerY, ringRadius * 0.82, ringStart + clock * 0.05, ringEnd + clock * 0.05);
      ctx.stroke();
      ctx.strokeStyle = "rgba(20, 184, 166, 0.12)";
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(centerX, centerY, ringRadius * 1.02, ringStart - clock * 0.04, ringEnd - clock * 0.04);
      ctx.stroke();

      const core = ctx.createRadialGradient(centerX, centerY, 8, centerX, centerY, ringRadius * 0.74);
      core.addColorStop(0, "rgba(255, 255, 255, 0.52)");
      core.addColorStop(0.5, `rgba(${modeAccent}, 0.08)`);
      core.addColorStop(1, "rgba(22, 93, 255, 0)");
      ctx.fillStyle = core;
      ctx.beginPath();
      ctx.arc(centerX, centerY, ringRadius * 0.72, 0, Math.PI * 2);
      ctx.fill();

      ctx.globalCompositeOperation = "source-over";
      const slowSweep = ctx.createLinearGradient(centerX - ringRadius, centerY, centerX + ringRadius, centerY);
      slowSweep.addColorStop(0, "rgba(255, 255, 255, 0)");
      slowSweep.addColorStop(0.5, `rgba(${modeAccent}, 0.1)`);
      slowSweep.addColorStop(1, "rgba(20, 184, 166, 0.08)");
      ctx.strokeStyle = slowSweep;
      ctx.lineWidth = 12 + energyPulse * 3;
      ctx.beginPath();
      ctx.arc(centerX, centerY, ringRadius * 0.66, -Math.PI * 0.22 + clock * 0.18, Math.PI * 0.44 + clock * 0.18);
      ctx.stroke();
      ctx.restore();

      ctx.save();
      ctx.globalAlpha = 0.2;
      ctx.strokeStyle = gate.canEnroll ? "rgba(20, 184, 166, 0.22)" : "rgba(255, 125, 0, 0.18)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      for (let x = 0; x <= w; x += 8) {
        const t = x / w;
        const y =
          h * 0.86 +
          Math.sin(t * Math.PI * 3.2 + clock * 0.58) * 16 * scoreFactor * modeWave +
          Math.sin(t * Math.PI * 8.4) * 5 * modeWave;
        if (x === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
      ctx.restore();

      raf = window.requestAnimationFrame(draw);
    };
    raf = window.requestAnimationFrame(draw);
    return () => window.cancelAnimationFrame(raf);
  }, [activeDimensions, record.id, record.quality.overall, record.quality.stability, gate.canEnroll, gate.minConsistency]);

  return (
    <section className={`voiceprint-enrollment-stage ${gate.canEnroll ? "pass" : gate.failedCount > 1 ? "pending" : "review"}`}>
      <div className="voiceprint-hero-grid">
        <main className="voiceprint-player-panel">
          <canvas ref={canvasRef} className="voiceprint-player-canvas" aria-label={`${record.employee} 声纹粒子播放视图`} />
          <div className="voiceprint-player-overlay">
            <span className={`voiceprint-live-chip ${gate.canEnroll ? "pass" : gate.failedCount > 1 ? "pending" : "review"}`}>
              <Radio size={13} />
              {templateRiskLabel}
            </span>
            <strong>{record.quality.overall}</strong>
            <p>{stageCenterCopy}</p>
            <div>
              <b>{passedGateCount}/{gate.checks.length} Gate</b>
              <b>{activeDimensionLabel}/{templateDimensionLabel}</b>
              <b>能量 {qualityEnergyLabel}</b>
              <b>{enrollStateLabel}</b>
            </div>
          </div>
          <div className="voiceprint-dimension-console" aria-label="声纹 embedding 维度投影">
            <div className="voiceprint-dimension-head">
              <span>Embedding Profile</span>
              <strong>{activeDimensions}/{templateDimensions} 维</strong>
              <em>{embeddingProfile.compressionRatio}% 有效贡献</em>
            </div>
            <svg className="voiceprint-projection-map" viewBox="0 0 100 100" role="img" aria-label="A/B/C 样本到模板中心的二维投影">
              <defs>
                <radialGradient id={`voiceprint-core-${record.id}`} cx="50%" cy="50%" r="50%">
                  <stop offset="0%" stopColor="#165dff" stopOpacity="0.22" />
                  <stop offset="70%" stopColor="#14b8a6" stopOpacity="0.08" />
                  <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
                </radialGradient>
              </defs>
              <rect x="0" y="0" width="100" height="100" rx="12" fill="rgba(255,255,255,0.58)" />
              <ellipse cx="50" cy="50" rx="27" ry="20" fill={`url(#voiceprint-core-${record.id})`} stroke="rgba(22,93,255,0.18)" strokeWidth="1.2" />
              <path d="M16 50H84M50 18V82" stroke="rgba(100,116,139,0.16)" strokeWidth="0.8" strokeDasharray="3 4" />
              {embeddingProfile.projection.filter((point) => point.tone !== "center").map((point) => (
                <line key={`${point.label}-line`} x1="50" y1="50" x2={point.x} y2={point.y} stroke={point.tone === "risk" ? "rgba(245,63,63,0.46)" : "rgba(22,93,255,0.36)"} strokeWidth="1.2" />
              ))}
              {embeddingProfile.projection.map((point) => (
                <g key={point.label}>
                  <circle
                    cx={point.x}
                    cy={point.y}
                    r={point.tone === "center" ? 5.8 : 4.2}
                    fill={point.tone === "center" ? "#165dff" : point.tone === "risk" ? "#f53f3f" : "#14b8a6"}
                    opacity={point.tone === "center" ? 0.92 : 0.86}
                  />
                  <text x={point.x + 5.6} y={point.y - 3.5} fill="#1d2129" fontSize="7" fontWeight="800">{point.label}</text>
                  <text x={point.x + 5.6} y={point.y + 5.5} fill="#64748b" fontSize="5.8">{point.score}</text>
                </g>
              ))}
            </svg>
            <div className="voiceprint-dimension-bands">
              {embeddingProfile.bands.map((band) => (
                <button key={band.key} type="button" className={`voiceprint-dimension-band ${band.tone}`} onClick={() => setSelectedFlow(band.key === "noise" ? "quality" : "fusion")}>
                  <span>
                    <b>{band.label}</b>
                    <em>{band.range}</em>
                  </span>
                  <strong>{band.active}/{band.dims}</strong>
                  <i>
                    <b style={{ width: `${Math.round((band.active / band.dims) * 100)}%` }} />
                  </i>
                  <small>权重 {Math.round(band.weight * 100)}% · 漂移 {band.drift}</small>
                </button>
              ))}
            </div>
          </div>
          <div className="voiceprint-player-toolbar">
            <span className="voiceprint-play-indicator" aria-hidden="true">
              <Play size={16} fill="currentColor" />
            </span>
            <div>
              <span>{record.wav}</span>
              <strong>{record.window}</strong>
            </div>
            <em>质量播放中</em>
          </div>
          <div className="voiceprint-wave-strip" aria-label="声纹波形质量条">
            {Array.from({ length: 54 }, (_, index) => {
              const harmonic = Math.abs(Math.sin(index * 0.48 + record.quality.overall * 0.03));
              const modulation = Math.abs(Math.cos(index * 0.19 + record.quality.stability * 0.02));
              const height = 18 + harmonic * 48 + modulation * 24;
              const hot = index > 25 && index < 34;
              return <span key={index} className={hot ? "hot" : ""} style={{ height: `${Math.min(86, height)}%` }} />;
            })}
          </div>
        </main>

        <aside className="voiceprint-quality-panel">
          <div className="voiceprint-quality-head">
            <div>
              <span>质量检测</span>
              <strong>{gate.verdict}</strong>
              <p>{selectedFlowCopy.subtitle}</p>
            </div>
            <b>融合模板</b>
          </div>
          <div className="voiceprint-gate-stack voiceprint-gate-stack-hero">
            {gate.checks.map((check) => (
              <button key={check.key} type="button" className={check.passed ? "pass" : "fail"} onClick={() => setSelectedFlow("quality")}>
                <span>{check.label}</span>
                <strong>{check.value}</strong>
                <em>{check.threshold}</em>
                <i>
                  <b style={{ width: `${Math.max(8, Math.min(100, check.score))}%` }} />
                </i>
              </button>
            ))}
          </div>
          <div className="voiceprint-mini-consistency voiceprint-consistency-hero">
            {[
              ["A-B", record.consistency.ab],
              ["A-C", record.consistency.ac],
              ["B-C", record.consistency.bc]
            ].map(([label, value]) => {
              const pct = Math.round(Number(value) * 100);
              return (
                <div key={label}>
                  <span>{label}</span>
                  <i>
                    <b style={{ width: `${pct}%` }} />
                  </i>
                  <strong>{pct}%</strong>
                </div>
              );
            })}
          </div>
          <button
            className="voiceprint-submit-primary"
            type="button"
            data-action-key="voiceprint-enroll-submit"
            data-voiceprint-id={record.id}
            disabled={!gate.canEnroll || submitted || submitting}
            onClick={submit}
          >
            <ShieldCheck size={14} />
            {submitLabel}
          </button>
        </aside>
      </div>
    </section>
  );
}

export function VoiceprintDataPage<TAsset extends { id: string; assetKey: string }>({
  records,
  dataAssets,
  setActiveModule,
  setSelectedAssetId,
  openListeningFromDataAsset,
  openAssetsFromDataAsset
}: {
  records: VoiceprintRecord[];
  dataAssets: TAsset[];
  setActiveModule: (module: "canvas") => void;
  setSelectedAssetId: (id: string) => void;
  openListeningFromDataAsset: (asset: TAsset) => void;
  openAssetsFromDataAsset: (asset: TAsset) => void;
}) {
  const [selectedRecordId, setSelectedRecordId] = useState(records[0]?.id ?? "");
  const selectedRecord = records.find((record) => record.id === selectedRecordId) ?? records[0];
  const [selectedFlow, setSelectedFlow] = useState<VoiceprintFlowKey>("quality");
  const [submittedVoiceprints, setSubmittedVoiceprints] = useState<Set<string>>(() => new Set());
  const [submittingVoiceprintId, setSubmittingVoiceprintId] = useState<string | null>(null);
  const [enrollmentReceipts, setEnrollmentReceipts] = useState<Record<string, BackendActionReceipt>>({});
  const [enrollmentFeedback, setEnrollmentFeedback] = useState<EnrollmentFeedback>({
    status: "idle",
    title: "声纹入库未提交",
    detail: "确认入库后会写入后端入库台账，并进入人工复核或质量修复队列。"
  });
  const linkedAsset = dataAssets.find((asset) => asset.id === selectedRecord.assetId) ?? dataAssets[0];
  const gate = getVoiceprintGate(selectedRecord);
  const selectedEmbeddingProfile = getVoiceprintEmbeddingProfile(selectedRecord);
  const selectedReceipt = enrollmentReceipts[selectedRecord.id];
  const submitted = submittedVoiceprints.has(selectedRecord.id) || Boolean(selectedReceipt);
  const shortTrace = (traceId?: string) => (traceId ? traceId.slice(0, 12) : "no-trace");
  const openLinkedListening = () => {
    setSelectedAssetId(linkedAsset.id);
    openListeningFromDataAsset(linkedAsset);
  };
  const openLinkedAsset = () => {
    setSelectedAssetId(linkedAsset.id);
    openAssetsFromDataAsset(linkedAsset);
  };
  const submitVoiceprint = async () => {
    if (!gate.canEnroll) return;
    const minConsistency = Math.min(selectedRecord.consistency.ab, selectedRecord.consistency.ac, selectedRecord.consistency.bc);
    const recordingId = selectedRecord.wav.replace(/\.wav$/i, "");
    const embeddingProfile = getVoiceprintEmbeddingProfile(selectedRecord);
    const payload = {
      enrollment_id: `${selectedRecord.id.toLowerCase()}_enrollment_v1`,
      voiceprint_id: selectedRecord.id,
      employee_ref: selectedRecord.employee,
      speaker_id: selectedRecord.speakerId,
      audio_session_id: selectedRecord.session,
      recording_id: recordingId,
      wav_file: selectedRecord.wav,
      device: selectedRecord.device,
      store: selectedRecord.store,
      evidence_window: selectedRecord.window,
      source_asset: selectedRecord.sourceAsset,
      voice_asset_key: selectedRecord.voiceAsset,
      asset_key: linkedAsset.assetKey,
      quality: selectedRecord.quality,
      consistency: selectedRecord.consistency,
      min_consistency: Number(minConsistency.toFixed(3)),
      samples: selectedRecord.samples.map(([sampleId, sampleType, sampleWindow, score]) => ({
        sample_id: sampleId,
        type: sampleType,
        window: sampleWindow,
        score,
        wav_file: selectedRecord.wav
      })),
      embedding_ref: {
        collection: "voiceprint_embeddings",
        vector_dim: embeddingProfile.totalDims,
        active_dims: embeddingProfile.activeDims,
        status: "reference_only"
      },
      lineage: selectedRecord.lineage,
      risks: selectedRecord.risks,
      decision: "submit_enrollment",
      source: "voiceprint_data_page"
    };
    setSubmittingVoiceprintId(selectedRecord.id);
    setEnrollmentFeedback({
      status: "pending",
      title: "声纹入库申请提交中",
      detail: `${selectedRecord.id} 正在写入后端台账，等待 BFF 返回 trace。`
    });
    try {
      const response = await submitVoiceprintEnrollment(payload);
      setEnrollmentReceipts((current) => ({ ...current, [selectedRecord.id]: response.data }));
      setSubmittedVoiceprints((current) => {
        const next = new Set(current);
        next.add(selectedRecord.id);
        return next;
      });
      setSelectedFlow("submit");
      const statusCopy = response.data.status === "enrolled" ? "已入库" : response.data.status === "blocked" ? "已阻断" : "待人工复核";
      setEnrollmentFeedback({
        status: response.data.status === "blocked" ? "error" : "success",
        title: `声纹入库申请已记录：${statusCopy}`,
        detail: `${response.data.id} · Trace ${shortTrace(response.data.trace_id || response.meta?.trace_id)}`
      });
    } catch (error) {
      setEnrollmentFeedback({
        status: "error",
        title: "声纹入库申请失败",
        detail: error instanceof Error ? error.message : "后端未返回可识别错误，请重试。"
      });
    } finally {
      setSubmittingVoiceprintId(null);
    }
  };

  return (
    <div className="data-reference-page voiceprint-data-page">
      <section className="data-reference-head voiceprint-head">
        <div>
          <h2>人物/声纹资产</h2>
          <p>声纹模板必须回链到 wav、VAD 片段、说话人轨和人员实体；质量评分决定是否可入库。</p>
          <div className="data-ingest-hint">
            <ShieldCheck size={13} />
            <span>参考声纹录入工作流：身份确认 → 样本采集 → 质量检测 → 模板融合 → 人工确认。</span>
          </div>
        </div>
        <div>
          <button type="button" className="data-connect-button" onClick={() => setActiveModule("canvas")}>
            <Plus size={15} />
            接入声纹样本
          </button>
          <button type="button" className="data-contract-button" onClick={openLinkedAsset}>
            <Link2 size={15} />
            查看 wav 血缘
          </button>
          <button>
            <ListFilter size={15} />
            筛选
          </button>
          <button>
            <Download size={15} />
            导出
          </button>
        </div>
      </section>

      {enrollmentFeedback.status !== "idle" && (
        <div className={`operation-toast data-operation-toast voiceprint-enrollment-toast is-${enrollmentFeedback.status}`} role="status" aria-live="polite">
          <ShieldCheck size={15} />
          <strong>{enrollmentFeedback.title}</strong>
          <span>{enrollmentFeedback.detail}</span>
        </div>
      )}

      <VoiceprintEnrollmentStage
        record={selectedRecord}
        gate={gate}
        selectedFlow={selectedFlow}
        setSelectedFlow={setSelectedFlow}
        submitted={submitted}
        submitting={submittingVoiceprintId === selectedRecord.id}
        receipt={selectedReceipt}
        submit={submitVoiceprint}
      />

      <section className="voiceprint-quality-queue">
        <aside className="voiceprint-object-strip">
          <div className="voiceprint-panel-title">
            <span>声纹对象质量队列</span>
            <strong>只展示可决策的样本状态</strong>
          </div>
          <div className="voiceprint-object-rowset">
            {records.map((record) => {
              const tone = record.status === "可入库" ? "pass" : record.status === "需复核" ? "review" : "pending";
              return (
                <button
                  key={record.id}
                  type="button"
                  className={record.id === selectedRecord.id ? `voiceprint-row active ${tone}` : `voiceprint-row ${tone}`}
                  onClick={() => setSelectedRecordId(record.id)}
                >
                  <span>{record.employee}</span>
                  <strong>{record.status}</strong>
                  <em>{record.wav}</em>
                  <b>{record.quality.overall}</b>
                </button>
              );
            })}
          </div>
        </aside>

        <main className="voiceprint-trace-panel">
          <div className="voiceprint-panel-title">
            <span>当前样本证据链</span>
            <strong>{linkedAsset.id} · {linkedAsset.assetKey}</strong>
          </div>
          <div className="voiceprint-trace-grid">
            {[
              ["wav", selectedRecord.wav],
              ["会话", selectedRecord.session],
              ["设备", selectedRecord.device],
              ["窗口", selectedRecord.window],
              ["Source", selectedRecord.sourceAsset],
              ["Voice Asset", selectedRecord.voiceAsset]
            ].map(([label, value]) => (
              <div key={label}>
                <span>{label}</span>
                <strong>{value}</strong>
              </div>
            ))}
          </div>
          <div className="voiceprint-dimension-table" aria-label="声纹维度明细">
            <div>
              <span>维度组</span>
              <span>范围</span>
              <span>有效维度</span>
              <span>漂移</span>
            </div>
            {selectedEmbeddingProfile.bands.map((band) => (
              <button key={band.key} type="button" className={band.tone} onClick={() => setSelectedFlow(band.key === "noise" ? "quality" : "fusion")}>
                <strong>{band.label}</strong>
                <em>{band.range}</em>
                <b>{band.active}/{band.dims}</b>
                <small>{band.drift}</small>
              </button>
            ))}
          </div>
          <div className="voiceprint-lineage-compact">
            {selectedRecord.lineage.map((item, index) => (
              <span key={item}>
                <b>{index + 1}</b>
                {item}
              </span>
            ))}
          </div>
          <div className="voiceprint-actions voiceprint-actions-inline">
            <button type="button" onClick={openLinkedListening}>
              <Headphones size={14} />
              回到调听
            </button>
            <button type="button" onClick={openLinkedAsset}>
              <BookOpen size={14} />
              查看血缘
            </button>
            <button type="button" onClick={() => setActiveModule("canvas")}>
              <GitBranch size={14} />
              配置入库流程
            </button>
          </div>
        </main>
      </section>
    </div>
  );
}
