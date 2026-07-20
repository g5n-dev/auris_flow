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
  const binding = hotwordBackfillRecovery.binding;
  if (!binding) {
    return (
      <div className="hotword-governance-lineage" data-testid="hotword-governance-lineage-unavailable" role="status">
        <div className="hotword-lineage-head">
          <div>
            <span>ASR 热词治理专用链路</span>
            <strong>权威绑定尚未恢复</strong>
          </div>
          <code>{hotwordBackfillRecovery.reason}</code>
        </div>
        <p>未读取到已发布词包、EvalRun、TaskVersion、根 Trace 与源物化的完整绑定，因此不展示本地血缘 fixture。</p>
      </div>
    );
  }
  const primaryBadcaseId = binding.sourceBadcaseIds[0] ?? null;
  const lineageNodes = [
    ["原 ASR 资产", binding.sourceMaterializationId, "immutable"],
    ["词级证据", primaryBadcaseId ? `${primaryBadcaseId} · source_badcase_id` : "当前词包版本未返回来源 Badcase", primaryBadcaseId ? "已绑定" : "未提供"],
    [primaryBadcaseId ? `${primaryBadcaseId} Badcase` : "Badcase 来源", primaryBadcaseId ?? "未提供", primaryBadcaseId ? "已治理" : "未提供"],
    ["词包候选版本", binding.hotwordPackVersionId, hotwordBackfillRecovery.status],
    ["EvalRun", binding.evalRunId, hotwordBackfillRecovery.status],
    ["新转写资产", binding.sourceAsset, "candidate"],
    ["受控回填", binding.taskVersionId, hotwordBackfillRecovery.status]
  ] as const;
  return (
    <div className="hotword-governance-lineage" data-testid="hotword-governance-lineage">
      <div className="hotword-lineage-head">
        <div>
          <span>ASR 热词治理专用链路</span>
          <strong>历史只读 → 候选修复 → 影子复测 → 受控回填</strong>
        </div>
        <code>root_trace_id = {binding.rootTraceId}</code>
      </div>
      <div className="hotword-lineage-flow">
        {lineageNodes.map(([label, value, status], index) => (
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
                  createBackfillDraft(
                    binding.sourceAsset,
                    "ASR 热词 EvalRun 通过后生成",
                    { rootTraceId: binding.rootTraceId, hotwordBinding: binding }
                  );
                  return;
                }
                if (label === "原 ASR 资产" || label === "新转写资产") {
                  setSelectedAssetKey(binding.sourceAsset);
                }
                setAssetNotice({
                  status: "success",
                  title: `已定位${label}`,
                  detail: `${value} · ${status} · root_trace_id ${binding.rootTraceId}`
                });
              }}
            >
              <span>{label}</span>
              <strong>{value}</strong>
              <em>{status}</em>
            </button>
            {index < lineageNodes.length - 1 && <i>→</i>}
          </Fragment>
        ))}
      </div>
      <p>人工确认结果与历史转写永不原地覆盖；只有发布后的词包版本可生成生产 TaskVersion 草稿。</p>
      {hotwordBackfillRecovery.status === "blocked" && (
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
              objectId: binding.taskVersionId,
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
