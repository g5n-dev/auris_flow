import { Fragment } from "react";

import type { AssetsWorkspace } from "../useAssetsWorkspace";

export function HotwordGovernanceLineage({ workspace }: { workspace: AssetsWorkspace }) {
  const {
    createBackfillDraft,
    hotwordBackfillRecovery,
    navigateToTarget,
    setAssetNotice,
    setSelectedAssetKey
  } = workspace;
  return (
    <div className="hotword-governance-lineage" data-testid="hotword-governance-lineage">
      <div className="hotword-lineage-head">
        <div>
          <span>ASR 热词治理专用链路</span>
          <strong>历史只读 → 候选修复 → 影子复测 → 受控回填</strong>
        </div>
        <code>root_trace_id = {hotwordBackfillRecovery.binding?.rootTraceId ?? `${hotwordBackfillRecovery.status}：待后端恢复`}</code>
      </div>
      <div className="hotword-lineage-flow">
        {[
          ["原 ASR 资产", hotwordBackfillRecovery.binding?.sourceMaterializationId ?? "源物化待恢复", hotwordBackfillRecovery.binding ? "immutable" : hotwordBackfillRecovery.status],
          ["词级证据", "A-4107.evidence_storage_object_id（后端受控）", "按项目投影恢复"],
          ["A-4107 Badcase", "asr-hotword / misrecognition", "待人审"],
          ["词包候选版本", hotwordBackfillRecovery.binding?.hotwordPackVersionId ?? "version_id 待 API 恢复", hotwordBackfillRecovery.status],
          ["EvalRun", hotwordBackfillRecovery.binding?.evalRunId ?? "eval_run_id 待 API 恢复", hotwordBackfillRecovery.status],
          ["新转写资产", "auris/model/asr_transcripts@candidate", "shadow"],
          ["受控回填", hotwordBackfillRecovery.binding?.taskVersionId ?? hotwordBackfillRecovery.reason, hotwordBackfillRecovery.status]
        ].map(([label, value, status], index) => (
          <Fragment key={label}>
            <button
              type="button"
              data-testid={
                label === "受控回填"
                  ? "hotword-controlled-backfill"
                  : label === "原 ASR 资产"
                    ? "hotword-source-materialization"
                    : undefined
              }
              disabled={label === "受控回填" && hotwordBackfillRecovery.status !== "ready"}
              title={label === "受控回填" && hotwordBackfillRecovery.status !== "ready" ? `blocked：${hotwordBackfillRecovery.reason}` : undefined}
              onClick={() => {
                if (label === "受控回填") {
                  const binding = hotwordBackfillRecovery.binding;
                  if (!binding) {
                    setAssetNotice({
                      status: "error",
                      title: "ASR 热词受控回填已阻断",
                      detail: hotwordBackfillRecovery.reason
                    });
                    return;
                  }
                  createBackfillDraft(
                    "auris/model/asr_transcripts",
                    "ASR 热词 EvalRun 通过后生成",
                    { rootTraceId: binding.rootTraceId, hotwordBinding: binding }
                  );
                  return;
                }
                if (label === "原 ASR 资产" || label === "新转写资产") {
                  setSelectedAssetKey("auris/model/asr_transcripts");
                }
                setAssetNotice({
                  status: "success",
                  title: `已定位${label}`,
                  detail: `${value} · ${status} · root_trace_id ${hotwordBackfillRecovery.binding?.rootTraceId ?? "待后端恢复"}`
                });
              }}
            >
              <span>{label}</span>
              <strong>{value}</strong>
              <em>{status}</em>
            </button>
            {index < 6 && <i>→</i>}
          </Fragment>
        ))}
      </div>
      <p>人工确认结果与历史转写永不原地覆盖；只有发布后的词包版本可生成生产 TaskVersion 草稿。</p>
      {hotwordBackfillRecovery.status === "blocked" && hotwordBackfillRecovery.binding?.taskVersionId && (
        <div className="evaluation-action-row compact">
          <span>blocked：{hotwordBackfillRecovery.reason}</span>
          <button
            type="button"
            className="primary"
            data-testid="hotword-open-task-publish"
            onClick={() => navigateToTarget({
              module: "canvas",
              tab: "versions",
              objectKind: "taskVersion",
              objectId: hotwordBackfillRecovery.binding?.taskVersionId,
              focusMode: "gate",
              title: "发布 ASR 热词 TaskVersion 草稿",
              detail: hotwordBackfillRecovery.reason,
              origin: { label: "资产 / ASR 热词受控回填", module: "assets" }
            })}
          >
            进入任务发布流程
          </button>
        </div>
      )}
    </div>
  );
}
