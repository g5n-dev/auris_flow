import type { CanvasController } from "../../controller/useCanvasController";

export function ExperimentConfigPanel({ controller }: { controller: CanvasController }) {
  const { controlledExperiment, experimentConfigDraft, experimentTaskVersions, experimentTreatmentPreview, setExperimentConfigDraft } = controller;
  const candidateArm = controlledExperiment?.arms.find((arm) => arm.arm_key === "candidate");
  const candidateTrafficPercent = controlledExperiment
    ? (candidateArm?.allocation_ppm ?? 0) / 10_000
    : experimentConfigDraft.candidateAllocationPpm / 10_000;
  const allocationUnit = controlledExperiment?.allocation_unit ?? experimentConfigDraft.allocationUnit;
  const minSampleSizePerArm = controlledExperiment?.min_sample_size_per_arm ?? experimentConfigDraft.minSampleSizePerArm;
  const confidenceLevel = controlledExperiment?.confidence_level ?? experimentConfigDraft.confidenceLevel;
  const configLocked = Boolean(controlledExperiment);
  const variantDimension = controlledExperiment?.variant_dimension ?? experimentConfigDraft.variantDimension;
  const controlTaskVersionId = controlledExperiment?.control_task_version_id ?? experimentConfigDraft.controlTaskVersionId;
  const candidateTaskVersionId = controlledExperiment?.candidate_task_version_id ?? experimentConfigDraft.candidateTaskVersionId;
  const versionId = (version: Record<string, unknown>) => String(version.task_version_id ?? version.id ?? "");
  const versionLabel = (version: Record<string, unknown>) => {
    const id = versionId(version);
    const versionName = String(version.version ?? id);
    const variant = String(version.canvas_variant ?? "未命名执行包");
    return `${versionName} · ${variant} · ${String(version.status ?? "unknown")}`;
  };
  const controlOptions = experimentTaskVersions.filter((version) =>
    version.status === "published" || versionId(version) === controlTaskVersionId
  );
  const candidateOptions = experimentTaskVersions.filter((version) =>
    ["experiment_ready", "validated"].includes(String(version.status ?? ""))
    || versionId(version) === candidateTaskVersionId
  );
  const dimensionLabels: Record<typeof variantDimension, string> = {
    workflow: "流程编排",
    model: "模型绑定",
    prompt: "Prompt 版本",
    label_policy: "标签策略",
    bundle: "组合版本包"
  };
  const updateCandidateTraffic = (rawValue: string) => {
    const percentage = Math.min(95, Math.max(5, Number(rawValue) || 5));
    setExperimentConfigDraft((current) => ({
      ...current,
      candidateAllocationPpm: Math.round(percentage * 10_000)
    }));
  };

  return (
    <div className={`experiment-config-panel ${configLocked ? "is-locked" : ""}`}>
      <div className="experiment-config-head">
        <span>实验设计</span>
        <b>{configLocked ? "已冻结" : "创建前可配置"}</b>
      </div>
      <div className="experiment-treatment-config">
        <label>
          <span>唯一实验变量</span>
          <select
            value={variantDimension}
            disabled={configLocked}
            aria-label="实验变量维度"
            onChange={(event) => setExperimentConfigDraft((current) => ({
              ...current,
              variantDimension: event.target.value as typeof current.variantDimension
            }))}
          >
            <option value="workflow">流程编排</option>
            <option value="model">模型绑定</option>
            <option value="prompt">Prompt 版本</option>
            <option value="label_policy">标签策略</option>
            <option value="bundle">组合版本包</option>
          </select>
        </label>
        <label>
          <span>A · 对照 TaskVersion</span>
          <select
            value={controlTaskVersionId}
            disabled={configLocked}
            aria-label="实验对照任务版本"
            onChange={(event) => setExperimentConfigDraft((current) => ({
              ...current,
              controlTaskVersionId: event.target.value
            }))}
          >
            {!controlOptions.length && <option value="">没有已发布版本</option>}
            {controlOptions.map((version) => <option key={versionId(version)} value={versionId(version)}>{versionLabel(version)}</option>)}
          </select>
        </label>
        <label>
          <span>B · 候选 TaskVersion</span>
          <select
            value={candidateTaskVersionId}
            disabled={configLocked}
            aria-label="实验候选任务版本"
            onChange={(event) => setExperimentConfigDraft((current) => ({
              ...current,
              candidateTaskVersionId: event.target.value
            }))}
          >
            {!candidateOptions.length && <option value="">没有冻结候选版本</option>}
            {candidateOptions.map((version) => <option key={versionId(version)} value={versionId(version)}>{versionLabel(version)}</option>)}
          </select>
        </label>
        <div className={`experiment-treatment-status ${experimentTreatmentPreview.compatible ? "is-valid" : "is-invalid"}`}>
          <span>{experimentTreatmentPreview.status}</span>
          <strong>
            声明：{dimensionLabels[variantDimension]} · 实际：{experimentTreatmentPreview.changedDimensions.map((dimension) => dimension === "label_schema" ? "标签结构" : dimension === "other" ? "其他执行参数" : dimensionLabels[dimension as keyof typeof dimensionLabels]).join(" + ") || "无"}
          </strong>
          <small>{experimentTreatmentPreview.diffSha256 ? `差异 SHA ${experimentTreatmentPreview.diffSha256.slice(0, 12)}` : "创建时由后端按规范化版本包再次校验并冻结 SHA"}</small>
        </div>
      </div>
      <label className="experiment-config-field traffic">
        <span>候选流量</span>
        <div>
          <input
            type="range"
            min="5"
            max="95"
            step="5"
            value={candidateTrafficPercent}
            disabled={configLocked}
            onChange={(event) => updateCandidateTraffic(event.target.value)}
            aria-label="候选流量百分比滑块"
          />
          <input
            type="number"
            min="5"
            max="95"
            step="5"
            value={candidateTrafficPercent}
            disabled={configLocked}
            onChange={(event) => updateCandidateTraffic(event.target.value)}
            aria-label="候选流量百分比"
          />
          <em>%</em>
        </div>
        <small>对照 {100 - candidateTrafficPercent}% / 候选 {candidateTrafficPercent}%</small>
      </label>
      <label className="experiment-config-field">
        <span>分流单元</span>
        <select
          value={allocationUnit}
          disabled={configLocked}
          aria-label="实验分流单元"
          onChange={(event) => setExperimentConfigDraft((current) => ({
            ...current,
            allocationUnit: event.target.value as typeof current.allocationUnit
          }))}
        >
          <option value="audio_session">音频会话</option>
          <option value="conversation">完整对话</option>
          <option value="store">组织单元</option>
          <option value="user">用户</option>
          <option value="device">设备</option>
          <option value="business_object">业务对象</option>
        </select>
        <small>同一 ID 始终进入同一实验臂</small>
      </label>
      <label className="experiment-config-field">
        <span>每臂最小样本</span>
        <input
          type="number"
          min="3"
          max="10000000"
          value={minSampleSizePerArm}
          disabled={configLocked}
          aria-label="每臂最小样本量"
          onChange={(event) => setExperimentConfigDraft((current) => ({
            ...current,
            minSampleSizePerArm: Math.min(10_000_000, Math.max(3, Number(event.target.value) || 3))
          }))}
        />
        <small>按唯一分流单元去重计算</small>
      </label>
      <div className="experiment-config-field">
        <span>置信水平</span>
        <div className="experiment-confidence-options" role="group" aria-label="实验置信水平">
          {([0.9, 0.95, 0.99] as const).map((level) => (
            <button
              key={level}
              type="button"
              disabled={configLocked}
              className={confidenceLevel === level ? "active" : ""}
              aria-pressed={confidenceLevel === level}
              onClick={() => setExperimentConfigDraft((current) => ({ ...current, confidenceLevel: level }))}
            >
              {Math.round(level * 100)}%
            </button>
          ))}
        </div>
        <small>用于差异区间和显著性计算</small>
      </div>
    </div>
  );
}
