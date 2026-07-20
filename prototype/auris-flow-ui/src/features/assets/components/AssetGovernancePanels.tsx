import { Activity, Gauge, ListFilter, RotateCcw, ShieldCheck } from "lucide-react";

import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { assetApiContracts, assetDagsterCompatibilityChecks } from "../catalog";
import { assetRunTimeline } from "../fixtures";
import type { AssetsWorkspace } from "../useAssetsWorkspace";

function AssetDetailUnavailable({ detail, testId }: { detail: string; testId: string }) {
  return (
    <div className="asset-detail-unavailable tenant-empty-state" data-testid={testId} role="status">
      <strong>BFF 明细未接入</strong>
      <span>{detail}；相关操作保持禁用，不展示本地 fixture。</span>
    </div>
  );
}

function AuthoritativeAssetChecks({ workspace }: { workspace: AssetsWorkspace }) {
  const { authoritativeAssetChecks, qualityRetryBlockedReason, qualityRetryReady } = workspace;
  if (authoritativeAssetChecks.status === "loading" || authoritativeAssetChecks.status === "idle") {
    return (
      <div className="asset-detail-unavailable tenant-empty-state" data-testid="asset-checks-loading" role="status">
        <strong>正在读取权威 checks</strong>
        <span>资产切换期间不会沿用上一资产或上一项目的质量事实。</span>
      </div>
    );
  }
  if (authoritativeAssetChecks.status === "error") {
    return (
      <div className="asset-detail-unavailable tenant-empty-state" data-testid="asset-checks-error" role="alert">
        <strong>权威 checks 读取失败</strong>
        <span>{authoritativeAssetChecks.reason}；质量重跑保持禁用。</span>
      </div>
    );
  }
  if (authoritativeAssetChecks.status === "empty" || !authoritativeAssetChecks.value?.checks.length) {
    return (
      <div className="asset-detail-unavailable tenant-empty-state" data-testid="asset-checks-empty" role="status">
        <strong>当前资产没有权威 checks</strong>
        <span>后端未返回可校验明细；质量重跑保持禁用。</span>
      </div>
    );
  }
  return (
    <div className="asset-compat-list" data-testid="asset-checks-authoritative">
      {authoritativeAssetChecks.value.checks.map((check) => {
        const failed = check.status === "failed" || check.status === "error" || check.failedPartitions.length > 0;
        return (
          <div
            key={check.id}
            className={`asset-compat-row ${failed ? "attention" : "ok"}`}
            data-check-id={check.id}
          >
            <code>{check.id}</code>
            <strong>{check.name}</strong>
            <span>{check.failedPartitions.length ? check.failedPartitions.join(" / ") : "无失败分区"}</span>
            <em>{check.status}</em>
          </div>
        );
      })}
      {!qualityRetryReady && (
        <div className="asset-detail-unavailable tenant-empty-state" data-testid="asset-quality-retry-disabled-reason" role="status">
          <strong>质量重跑已禁用</strong>
          <span>{qualityRetryBlockedReason}</span>
        </div>
      )}
    </div>
  );
}

export function AssetCompatibilityPanel({ workspace }: { workspace: AssetsWorkspace }) {
  const { compatFilter, setCompatFilter, visibleCompatibilityChecks } = workspace;
  if (!LABEL_DEMO_MODE) {
    return (
      <section className="module-panel wide asset-compat-panel">
        <PanelHeader title="执行兼容性" subtitle="需要后端质量检查与发布门禁回执" icon={<ShieldCheck size={16} />} />
        <AssetDetailUnavailable detail="尚未读取资产 checks / gate 回执" testId="asset-compatibility-detail-unavailable" />
      </section>
    );
  }
  return (
    <section className="module-panel wide asset-compat-panel">
      <PanelHeader title="执行兼容性 20 项" subtitle="发布前门禁：API、分区、运行、权限、回填、可观测性" icon={<ShieldCheck size={16} />} />
      <div className="asset-compat-toolbar">
        {(["全部", "需人工", "需确认"] as const).map((filter) => (
          <button key={filter} type="button" className={compatFilter === filter ? "active" : ""} onClick={() => setCompatFilter(filter)}>
            {filter}
          </button>
        ))}
        <span>{visibleCompatibilityChecks.length} / {assetDagsterCompatibilityChecks.length}</span>
      </div>
      <div className="asset-compat-list">
        {visibleCompatibilityChecks.map(([name, status, detail], index) => (
          <div key={name} className={`asset-compat-row ${status === "兼容" ? "ok" : "attention"}`}>
            <b>{String(index + 1).padStart(2, "0")}</b>
            <strong>{name}</strong>
            <span>{detail}</span>
            <em>{status}</em>
          </div>
        ))}
      </div>
    </section>
  );
}

export function AssetBackfillPanel({
  wide = false,
  workspace
}: {
  wide?: boolean;
  workspace: AssetsWorkspace;
}) {
  const {
    assetAction,
    backfillBlockedReason,
    backfillReady,
    createBackfillDraft,
    currentDraft,
    selectedAsset,
    setBackfillDraft,
    submitBackfillDraft
  } = workspace;
  return (
    <section className={wide ? "module-panel wide asset-backfill-panel asset-backfill-expanded" : "module-panel asset-backfill-panel"}>
      <PanelHeader title="数据回填" subtitle="影响范围、审批和下游重算" icon={<RotateCcw size={16} />} />
      <div className="asset-backfill-card">
        <div className="asset-backfill-summary">
          <span>当前建议</span>
          <strong>{selectedAsset.name}</strong>
          <p>{selectedAsset.backfill}</p>
        </div>
        <div className="asset-backfill-impact">
          <b>影响下游</b>
          {selectedAsset.downstream.length
            ? selectedAsset.downstream.map((item) => <em key={item}>{item}</em>)
            : <em>BFF 未提供下游投影</em>}
        </div>
        <div className="asset-backfill-actions">
          <button type="button" onClick={() => createBackfillDraft()}>
            {currentDraft ? "更新回填草稿" : "创建回填草稿"}
          </button>
          {currentDraft && (
            <button
              type="button"
              className="secondary"
              data-testid="asset-backfill-submit"
              disabled={assetAction === "backfill" || !backfillReady}
              title={!backfillReady ? backfillBlockedReason : undefined}
              onClick={() => void submitBackfillDraft()}
            >
              {assetAction === "backfill" ? "提交中" : backfillReady ? "提交审批" : "等待分区明细"}
            </button>
          )}
        </div>
        {currentDraft && (
          <div className="asset-backfill-draft">
            <span>{currentDraft.status}</span>
            <strong>{currentDraft.draftId}</strong>
            <p>{currentDraft.assetName} · {currentDraft.reason}</p>
            <code>{selectedAsset.assetKey}</code>
            <button type="button" onClick={() => setBackfillDraft(null)}>删除草稿</button>
          </div>
        )}
      </div>
      {!backfillReady && (
        <div className="asset-detail-unavailable tenant-empty-state" data-testid="asset-backfill-blocked-reason" role="status">
          <strong>回填提交已禁用</strong>
          <span>{backfillBlockedReason}；可以编辑 UI 草稿，但不会发送占位值。</span>
        </div>
      )}
      {wide && (
        <div className="asset-backfill-plan">
          {(LABEL_DEMO_MODE ? [
            ["回填范围", "2025-05-20 至 2025-05-26 / 极光中心店 / 北京 SKP 店"],
            ["分区选择", selectedAsset.partition],
            ["覆盖策略", "不覆盖人工确认结果，写入候选资产版本"],
            ["审批规则", "批量回填和重算下游需要项目管理员确认"],
            ["运行请求", `asset_key=${selectedAsset.assetKey} partition=${selectedAsset.partition}`]
          ] : [
            ["回填范围", "BFF 分区明细未读取；提交前必须选择权威分区"],
            ["分区选择", selectedAsset.partition],
            ["覆盖策略", "由受控回填接口校验，不在页面预设业务结果"],
            ["审批规则", "由 BFF 回执决定"],
            ["运行请求", `asset_key=${selectedAsset.assetKey} partition=${selectedAsset.partition}`]
          ]).map(([label, value]) => (
            <span key={label} className={label === "运行请求" ? "run-request" : ""}>
              <b>{label}</b>
              {value}
            </span>
          ))}
        </div>
      )}
    </section>
  );
}

export function AssetRuntimePanel({
  wide = false,
  workspace
}: {
  wide?: boolean;
  workspace: AssetsWorkspace;
}) {
  if (!LABEL_DEMO_MODE) {
    return (
      <section className={wide ? "module-panel wide asset-runtime-panel asset-runtime-expanded" : "module-panel asset-runtime-panel"}>
        <PanelHeader title="资产任务" subtitle="生成记录、运行状态和失败分区" icon={<Activity size={16} />} />
        <AssetDetailUnavailable detail="尚未读取 materializations / runs API" testId="asset-runtime-detail-unavailable" />
      </section>
    );
  }
  return (
    <section className={wide ? "module-panel wide asset-runtime-panel asset-runtime-expanded" : "module-panel asset-runtime-panel"}>
      <PanelHeader title="资产任务" subtitle="生成记录、运行状态和失败分区" icon={<Activity size={16} />} />
      <div className="asset-run-list">
        {assetRunTimeline.map(([time, asset, state, runId]) => (
          <button key={runId} type="button" className={state.includes("失败") ? "danger" : state.includes("等待") ? "warn" : ""}>
            <span>{time}</span>
            <strong>{asset}</strong>
            <em>{state}</em>
            <code>{runId}</code>
          </button>
        ))}
      </div>
    </section>
  );
}

export function AssetApiPanel({ workspace: _workspace }: { workspace: AssetsWorkspace }) {
  return (
    <section className="module-panel wide asset-api-panel">
      <PanelHeader title="BFF 接口契约" subtitle="业务 API 屏蔽底层执行复杂度，保留 key、partition、run_id、trace_id" icon={<ListFilter size={16} />} />
      <div className="asset-api-contract-board">
        <div className="asset-api-list" aria-label="BFF 接口清单">
          {assetApiContracts.map((contract) => (
            <button key={contract.endpoint} type="button" className={`asset-api-row ${contract.tone}`}>
              <b>{contract.method}</b>
              <code>{contract.endpoint}</code>
              <span>{contract.purpose}</span>
              <em>{contract.dagster}</em>
              <i>{contract.response}</i>
            </button>
          ))}
        </div>
        <aside className="asset-api-contract-aside" aria-label="接口契约约束">
          <div>
            <span>统一输入</span>
            <strong>asset_key / partition / cursor / trace_id</strong>
            <p>前端只传业务语义，BFF 负责映射资产 Key、分区和运行上下文。</p>
          </div>
          <div>
            <span>写操作保护</span>
            <strong>审批、幂等、可回放</strong>
            <p>POST 不直接覆盖资产；生成审批或运行请求，并保留 run_id 与 trace_id。</p>
          </div>
        </aside>
      </div>
    </section>
  );
}

export function AssetQualityPanel({ workspace }: { workspace: AssetsWorkspace }) {
  const {
    assetAction,
    exportAssetPackage,
    rerunAssetQuality,
    qualityRetryBlockedReason,
    qualityRetryReady,
    sceneProfileBlockedReason,
    sceneProfileLock,
    selectedAsset
  } = workspace;
  if (!LABEL_DEMO_MODE) {
    return (
      <section className="module-panel wide asset-quality-panel">
        <PanelHeader title="资产质量" subtitle="仅展示 data-assets/recent 已返回的投影字段" icon={<Gauge size={16} />} />
        <div className="asset-quality-grid">
          {[
            ["投影状态", selectedAsset.status, "status"],
            ["质量分", selectedAsset.quality === null ? "BFF 未提供" : `${selectedAsset.quality}`, "quality_score"],
            ["新鲜度", selectedAsset.freshness, "freshness"],
            ["最近生成", selectedAsset.materialization, "latest_materialization_id"]
          ].map(([label, value, desc]) => (
            <div key={label}>
              <span>{label}</span>
              <strong>{value}</strong>
              <em>{desc}</em>
            </div>
          ))}
        </div>
        <AuthoritativeAssetChecks workspace={workspace} />
        <div className="asset-backfill-actions">
          <button
            type="button"
            data-testid="asset-quality-retry"
            disabled={assetAction === "quality" || !qualityRetryReady}
            title={!qualityRetryReady ? qualityRetryBlockedReason : undefined}
            onClick={() => void rerunAssetQuality()}
          >
            {assetAction === "quality" ? "重跑中" : "重跑质量校验"}
          </button>
          <button
            type="button"
            data-testid="asset-package-export"
            disabled={assetAction === "export" || !sceneProfileLock}
            title={!sceneProfileLock ? sceneProfileBlockedReason : undefined}
            onClick={exportAssetPackage}
          >
            {assetAction === "export" ? "导出中" : "导出资产包"}
          </button>
        </div>
      </section>
    );
  }
  return (
    <section className="module-panel wide asset-quality-panel">
      <PanelHeader title="资产质量" subtitle="完整性、及时性、一致性、重复率、Schema 稳定性、人工确认率" icon={<Gauge size={16} />} />
      <div className="asset-quality-grid">
        {[
          ["完整性", "96%", "URL、分区、主键完整"],
          ["及时性", selectedAsset.freshness, "最近生成 SLA"],
          ["一致性", "91%", "单据金额与 ASR 承诺对齐"],
          ["失败校验", selectedAsset.status.includes("失败") || selectedAsset.status.includes("待") ? "需处理" : "通过", selectedAsset.backfill],
          ["人工确认率", "72%", "低置信和覆盖回填进入 Human Loop"],
          ["评测覆盖率", "88%", "已进入模型对比和发布门禁"]
        ].map(([label, value, desc]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
            <em>{desc}</em>
          </div>
        ))}
      </div>
      <AuthoritativeAssetChecks workspace={workspace} />
      <div className="asset-backfill-actions">
        <button
          type="button"
          data-testid="asset-quality-retry"
          disabled={assetAction === "quality" || !qualityRetryReady}
          title={!qualityRetryReady ? qualityRetryBlockedReason : undefined}
          onClick={() => void rerunAssetQuality()}
        >
          {assetAction === "quality" ? "重跑中" : "重跑质量校验"}
        </button>
        <button
          type="button"
          data-testid="asset-package-export"
          disabled={assetAction === "export" || !sceneProfileLock}
          title={!sceneProfileLock ? sceneProfileBlockedReason : undefined}
          onClick={exportAssetPackage}
        >
          {assetAction === "export" ? "导出中" : "导出资产包"}
        </button>
      </div>
    </section>
  );
}
