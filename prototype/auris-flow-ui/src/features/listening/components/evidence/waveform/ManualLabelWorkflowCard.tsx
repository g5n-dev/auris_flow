import { AlertTriangle, Check, Link2, RefreshCw } from "lucide-react";
import type { WaveformPanelController } from "./trackRegionModalActions";

const busyStatuses = new Set([
  "loading-scope",
  "saving-draft",
  "submitting",
  "previewing",
  "rebasing"
]);

export function ManualLabelWorkflowCard({ controller }: { controller: WaveformPanelController }) {
  const {
    confirmModalAnnotationRebase,
    loadManualLabelScope,
    manualLabelWorkflow,
    modalRegion,
    modalTrack,
    previewModalAnnotationRebase,
    setManualLabelSelection,
    setManualMappingBundleId,
    setManualRebaseConfirmed
  } = controller;
  const { draft, items, mappingBundleId, preview, releaseHead, selectedLabelId, status } = manualLabelWorkflow;
  const busy = busyStatuses.has(status);
  const stale = status === "stale" || status === "previewing" || status === "awaiting-confirmation" || status === "rebasing";

  return (
    <section className="session-boundary-card evidence" data-testid="manual-label-version-workflow">
      <span>标签版本与事实</span>
      <div>
        <p>
          {stale ? <AlertTriangle size={13} /> : <Check size={13} />}
          {manualLabelWorkflow.message}
        </p>
        <p>
          <Link2 size={13} />
          {releaseHead
            ? `production · ${releaseHead.label_version_id} · generation ${releaseHead.generation}`
            : "尚未取得 production Release Head；不会用页面候选值推断版本。"}
        </p>
        {draft && (
          <p>
            <Link2 size={13} />
            冻结草稿 {draft.annotation_id} · {draft.label_version_id} · SHA {draft.draft_sha256.slice(0, 12)}
          </p>
        )}

        {(status === "ready" || stale) && (
          <label>
            <span>{stale ? "Rebase 目标标签" : "权威标签项"}</span>
            <select
              value={selectedLabelId}
              onChange={(event) => setManualLabelSelection(event.target.value)}
              disabled={busy || status === "awaiting-confirmation"}
            >
              <option value="">请选择，不自动猜测</option>
              {items.map((item) => (
                <option key={item.label_version_item_id} value={item.label_id}>
                  {item.canonical_name} · {item.label_id} · {item.value_type}
                </option>
              ))}
            </select>
          </label>
        )}

        {stale && (
          <label>
            <span>已发布 Mapping Bundle</span>
            <input
              value={mappingBundleId}
              onChange={(event) => setManualMappingBundleId(event.target.value)}
              disabled={busy || status === "awaiting-confirmation"}
              placeholder="缺失时由治理负责人提供，服务端仍会校验"
            />
          </label>
        )}

        {preview && (
          <div className="label-v2-list" data-testid="manual-label-rebase-preview">
            <p>
              {preview.preview.old_label_version_id} / {preview.preview.old_label_id}
              {" → "}
              {preview.preview.new_label_version_id} / {preview.preview.new_label_id ?? "待选择"}
            </p>
            {preview.preview.mapping_paths.map((path) => (
              <p key={path.path_sha256}>
                {path.comparability_status} · {path.requires_recompute ? "需要重算" : "无需重算"} · {path.target_label_id ?? "无目标"}
              </p>
            ))}
            <p>预览 SHA {preview.preview_sha256.slice(0, 16)}</p>
            <label>
              <input
                type="checkbox"
                checked={manualLabelWorkflow.rebaseConfirmed}
                onChange={(event) => setManualRebaseConfirmed(event.target.checked)}
                disabled={!preview.can_confirm || busy}
              />
              我已核对目标标签、映射关系、可比性与重算要求
            </label>
          </div>
        )}

        <div className="track-region-quick-actions">
          {(status === "error" || status === "idle") && modalRegion && modalTrack && (
            <button type="button" onClick={() => void loadManualLabelScope(modalTrack, modalRegion)} disabled={busy}>
              <RefreshCw size={13} />
              重新读取权威范围
            </button>
          )}
          {status === "stale" && (
            <button
              type="button"
              onClick={() => void previewModalAnnotationRebase()}
              disabled={!selectedLabelId || !mappingBundleId.trim() || busy}
            >
              预览映射差异
            </button>
          )}
          {status === "awaiting-confirmation" && (
            <button
              type="button"
              onClick={() => void confirmModalAnnotationRebase()}
              disabled={!preview?.can_confirm || !manualLabelWorkflow.rebaseConfirmed || busy}
            >
              显式确认并创建新草稿
            </button>
          )}
        </div>
      </div>
    </section>
  );
}
