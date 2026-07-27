import { useEffect, useMemo, useRef, useState } from "react";

import {
  AUDIO_IMPORT_STEPS,
  canRetryImportBatch,
  hasImportBatchFailures,
  type AudioImportBatchStatus,
  type AudioImportCurrentStage
} from "../audioImportFlowModel";
import type { AudioImportFlow } from "../audioImportFlowController";
import { AudioImportFormStep } from "./AudioImportFormSteps";
import { AudioImportSessionPanel } from "./AudioImportSessionPanel";

const stages = ["等待执行", "读取清单", "下载音频", "校验入库", "生成会话"];
const stageIndex: Record<AudioImportCurrentStage, number> = {
  queued: 0,
  listing: 1,
  downloading: 2,
  verifying: 3,
  materializing: 4,
  completed: 5
};
const statusText: Record<AudioImportBatchStatus, string> = {
  queued: "等待执行",
  running: "导入中",
  materializing: "物化中",
  partial: "部分完成",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消"
};

function AudioImportRunPanel({ flow }: { flow: AudioImportFlow }) {
  const [showItems, setShowItems] = useState(false);
  const [selectedSessionId, setSelectedSessionId] = useState("");
  const batch = flow.batch;
  const sessionIds = useMemo(() => Array.from(new Set([
    ...(batch?.createdAudioSessionIds ?? []),
    ...flow.batchItems.map((item) => item.audioSessionId)
  ].filter(Boolean))), [batch, flow.batchItems]);
  const failedItems = flow.batchItems.filter(
    (item) => ["failed", "error"].includes(item.status.toLowerCase()) || item.errorCode
  );
  if (!batch) {
    return (
      <section className="audio-import-run-empty">
        <strong>尚未创建同步批次</strong>
        <span>发布后点击“立即拉取”，状态由 BFF 回读。</span>
      </section>
    );
  }
  const hasFailures = hasImportBatchFailures(batch, failedItems.length);
  const errorCode = batch.errorCode || (
    batch.status === "partial" ? "AUDIO_IMPORT_BATCH_PARTIAL" : "AUDIO_IMPORT_BATCH_FAILED"
  );
  const errorReason = batch.errorReason
    || "批次在生成逐条失败记录前终止；尚无逐条失败项，请检查配置后重试。";
  const metrics = [
    ["总数", batch.total],
    ["成功", batch.succeeded],
    ["重复跳过", batch.duplicates],
    ["失败", batch.failed]
  ];
  return (
    <>
      <section className={`audio-import-run-panel is-${batch.status}`}>
        <header>
          <div><span>同步批次</span><strong>{batch.id}</strong></div>
          <b>{statusText[batch.status]}</b>
        </header>
        <div className="audio-import-business-stages" aria-label="导入业务阶段">
          {stages.map((label, index) => (
            <div
              key={label}
              className={index < stageIndex[batch.currentStage]
                ? "completed"
                : index === stageIndex[batch.currentStage] ? "active" : ""}
            >
              <i aria-hidden="true">{index < stageIndex[batch.currentStage] ? "✓" : "○"}</i>
              <span>{label}</span>
            </div>
          ))}
        </div>
        <div className="audio-import-run-metrics">
          {metrics.map(([label, value]) => (
            <div key={label}><span>{label}</span><strong>{value}</strong></div>
          ))}
        </div>
        {(Boolean(batch.errorCode || batch.errorReason) || (hasFailures && !failedItems.length)) && (
          <div
            className="audio-import-inline-warning audio-import-batch-error"
            data-testid="audio-import-batch-error"
            role="alert"
          ><strong>{errorCode}</strong><span>{errorReason}</span></div>
        )}
        <div className="audio-import-run-actions">
          <button type="button" disabled={Boolean(flow.action)} onClick={() => void flow.refreshBatch()}>
            刷新批次
          </button>
          <button type="button" disabled={!hasFailures} onClick={() => setShowItems((value) => !value)}>
            {showItems ? "收起失败记录" : "查看失败记录"}
          </button>
          <button
            type="button"
            disabled={!canRetryImportBatch(batch, failedItems.length) || Boolean(flow.action)}
            onClick={() => void flow.retryFailedItems()}
          >{flow.action === "retry" ? "重试中" : "重试失败项"}</button>
          <button
            type="button"
            className="primary"
            disabled={!sessionIds.length}
            onClick={() => setSelectedSessionId(sessionIds[0] ?? "")}
          >查看新会话{sessionIds.length > 1 ? `（${sessionIds.length}）` : ""}</button>
        </div>
        {showItems && (
          <div className="audio-import-failed-items" data-testid="audio-import-failed-items">
            {failedItems.length ? failedItems.map((item) => (
              <div key={item.id}>
                <strong>{item.externalRecordId || item.id}</strong>
                <span>{item.errorCode || item.status}</span>
                <code>{item.rootTraceId || "no-trace"}</code>
              </div>
            )) : (
              <div data-testid="audio-import-batch-failure-placeholder">
                <strong>批次级失败</strong><span>{errorReason}</span>
                <code>{errorCode} · {batch.rootTraceId || "no-trace"}</code>
              </div>
            )}
          </div>
        )}
        <details data-testid="audio-import-technical-details">
          <summary>技术详情</summary>
          <dl>
            {([
              ["TaskVersion", flow.taskVersionId || "未发布"],
              ["TaskRun", batch.taskRunId || "未返回"],
              ["Root trace", batch.rootTraceId || "未返回"],
              ["执行引擎", "Dagster · auris_flow_audio_import_v1"]
            ] as const).map(([label, value]) => (
              <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
            ))}
          </dl>
        </details>
      </section>
      {selectedSessionId && (
        <AudioImportSessionPanel
          audioSessionId={selectedSessionId}
          onClose={() => setSelectedSessionId("")}
        />
      )}
    </>
  );
}

function ReleaseStep({ flow }: { flow: AudioImportFlow }) {
  const draftSaved = flow.taskVersionCurrent && flow.taskVersionStatus === "draft";
  const published = flow.taskVersionCurrent && flow.taskVersionStatus === "published";
  const publishing = flow.taskVersionCurrent && flow.taskVersionStatus === "publishing";
  const versionState = published ? "已发布" : publishing ? "发布中" : draftSaved ? "草稿已保存" : "待保存草稿";
  return (
    <section className="audio-import-step-panel audio-import-release-panel" aria-labelledby="audio-import-step-6">
      <div className="audio-import-step-copy">
        <div>
          <h3 id="audio-import-step-6">发布配置并立即拉取</h3>
          <p>生产拉取仅引用已发布版本；成功以批次写后回读为准。</p>
        </div>
      </div>
      <div className="audio-import-release-summary">
        {([
          ["配置", flow.draft.name],
          ["平台连接", flow.draft.platformConnectionId],
          ["目标资产", flow.draft.targetAssetKey],
          ["任务版本", `${flow.taskVersionCurrent ? flow.taskVersionId : "待创建"} · ${versionState}`]
        ] as const).map(([label, value]) => (
          <div key={label}><span>{label}</span><strong>{value}</strong></div>
        ))}
      </div>
      <div className="audio-import-release-actions">
        <button
          type="button"
          data-testid="audio-import-save-draft"
          disabled={Boolean(flow.action) || draftSaved || published || publishing}
          onClick={() => void flow.saveDraft()}
        >{flow.action === "save-draft" ? "保存中" : draftSaved ? "草稿已保存" : "保存草稿"}</button>
        <button
          type="button"
          data-testid="audio-import-publish"
          disabled={Boolean(flow.action) || !draftSaved}
          onClick={() => void flow.publish()}
        >{flow.action === "publish" ? "发布中" : published ? "已发布" : publishing ? "等待发布回读" : "发布版本"}</button>
        <button
          type="button"
          className="primary"
          data-testid="audio-import-run-production"
          disabled={Boolean(flow.action) || !published}
          onClick={() => void flow.run()}
        >{flow.action === "run" ? "创建运行中" : "立即拉取"}</button>
      </div>
      {!published && (
        <p className="audio-import-inline-warning" role="status">
          {draftSaved ? "草稿已回读；发布后才能拉取。" : "请先保存当前配置草稿。"}
        </p>
      )}
      <AudioImportRunPanel flow={flow} />
    </section>
  );
}

export function AudioImportDrawer({ flow }: { flow: AudioImportFlow }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const previousRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!flow.open) return;
    previousRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    return () => {
      const previous = previousRef.current;
      if (previous?.isConnected) window.requestAnimationFrame(() => previous.focus());
    };
  }, [flow.open]);
  if (!flow.open) return null;
  const liveDetail = flow.action
    ? flow.detail
    : flow.blockers.length
      ? [flow.blockers[0], flow.detail].filter((item, index, items) =>
          Boolean(item) && items.indexOf(item) === index
        ).join(" · ")
      : flow.detail || "等待下一步操作";
  return (
    <div
      className="audio-import-overlay"
      role="presentation"
      onMouseDown={(event) => event.target === event.currentTarget && flow.close()}
    >
      <aside
        className="audio-import-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="audio-import-title"
        onKeyDown={(event) => event.key === "Escape" && flow.close()}
      >
        <header className="audio-import-drawer-head">
          <div>
            <span>数据资产 / 新建导入配置</span>
            <h2 id="audio-import-title">平台音频 URL 导入</h2>
            <p>关联平台、验证、发布，再创建生产同步批次。</p>
          </div>
          <button ref={closeRef} type="button" aria-label="关闭导入配置" onClick={flow.close}>×</button>
        </header>
        <nav className="audio-import-stepper" aria-label="导入配置步骤">
          {AUDIO_IMPORT_STEPS.map((item) => (
            <button
              key={item.id}
              type="button"
              className={flow.step === item.id ? "active" : flow.step > item.id ? "completed" : ""}
              aria-current={flow.step === item.id ? "step" : undefined}
              onClick={() => flow.setStep(item.id)}
            ><b>{flow.step > item.id ? "✓" : item.id}</b><span>{item.label}</span></button>
          ))}
        </nav>
        <div className="audio-import-drawer-body">
          {flow.step === 6 ? <ReleaseStep flow={flow} /> : <AudioImportFormStep flow={flow} />}
        </div>
        <div
          className={`audio-import-live-detail ${flow.blockers.length ? "has-blocker" : ""}`}
          role="status"
          aria-live="polite"
        >
          <strong>{flow.action ? "正在处理" : flow.blockers.length ? "本步待完成" : "配置状态"}</strong>
          <span>{liveDetail}</span>
        </div>
        <footer className="audio-import-drawer-foot">
          <button type="button" disabled={flow.step === 1 || Boolean(flow.action)} onClick={flow.previous}>
            上一步
          </button>
          <span>第 {flow.step} / {AUDIO_IMPORT_STEPS.length} 步</span>
          {flow.step < 6 ? (
            <button type="button" className="primary" disabled={Boolean(flow.action)} onClick={flow.next}>
              下一步
            </button>
          ) : <button type="button" onClick={flow.close}>完成并关闭</button>}
        </footer>
      </aside>
    </div>
  );
}
