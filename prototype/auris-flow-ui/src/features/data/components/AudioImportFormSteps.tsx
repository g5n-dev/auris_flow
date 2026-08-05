import type { AudioImportDraft, AudioImportFieldMapping } from "../audioImportFlowModel";
import type { AudioImportFlow } from "../audioImportFlowController";
import { formatAudioImportPreviewValue } from "../audioImportPreviewSecurity";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";

function Field({
  label,
  required,
  children
}: {
  label: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label className="audio-import-field">
      <span>{label}{required && <b aria-hidden="true"> *</b>}</span>
      {children}
    </label>
  );
}

const copy = [
  ["关联外部平台", "绑定已有平台连接，服务端将在下一步验证。"],
  ["配置平台音频 URL API", "只保存凭证引用；认证、分页和下载均在服务端执行。"],
  ["测试连接并预览真实记录", "参数变化后必须重新测试并预览。"],
  ["映射平台字段", "前三项为生成音频会话的必填字段。"],
  ["配置增量游标和目标资产", "游标必须唯一、严格递增并可持久化。"]
] as const;

function Panel({
  flow,
  children
}: {
  flow: AudioImportFlow;
  children: React.ReactNode;
}) {
  const [title, detail] = copy[flow.step - 1];
  return (
    <section className="audio-import-step-panel" aria-labelledby={`audio-import-step-${flow.step}`}>
      <div className="audio-import-step-copy">
        <div>
          <h3 id={`audio-import-step-${flow.step}`}>{title}</h3>
          <p>{detail}</p>
        </div>
      </div>
      {children}
    </section>
  );
}

const endpointFields = [
  ["name", "配置名称", undefined, "audio-import-config-name"],
  ["baseUrl", "API 地址", "https://platform.example.com", "audio-import-base-url"],
  ["requestPath", "录音清单路径", "/v1/recordings", "audio-import-request-path"],
  ["credentialRef", "credential_ref", "secret://tenant/platform-audio-api", "audio-import-credential-ref"],
  ["pageSize", "每页记录数", undefined, "audio-import-page-size"],
  ["cursorParam", "增量游标参数名", undefined, "audio-import-cursor-param"],
  ["nextCursorPath", "下一持久游标字段路径", undefined, "audio-import-next-cursor-path"]
] as const;
const mappingFields = [
  ["externalRecordId", "外部录音 ID", true, "audio-import-map-external-record-id"],
  ["audioUrl", "音频 URL", true, "audio-import-map-audio-url"],
  ["startedAt", "通话时间", true, "audio-import-map-started-at"],
  ["agentRef", "员工 ID", false, "audio-import-map-agent-ref"],
  ["storeRef", "门店 ID", false, "audio-import-map-store-ref"],
  ["deviceRef", "设备 ID", false, "audio-import-map-device-ref"],
  ["durationMs", "时长（毫秒）", false, "audio-import-map-duration-ms"]
] as const;
const targetFields = [
  ["cursorField", "唯一递增游标字段", "audio-import-cursor-field"],
  ["initialWindowStart", "首次拉取开始时间", "audio-import-initial-window"],
  ["targetAssetKey", "目标音频资产", "audio-import-target-asset"]
] as const;

function setDraftField(
  flow: AudioImportFlow,
  key: keyof AudioImportDraft,
  value: string | number
) {
  flow.setDraft((current) => ({ ...current, [key]: value }));
}

const connectionBoundDraftFields = new Set<keyof AudioImportDraft>([
  "platformTenantKey",
  "baseUrl",
  "credentialRef"
]);

function setConnectionBoundDraftField(
  flow: AudioImportFlow,
  key: keyof AudioImportDraft,
  value: string | number
) {
  if (flow.selectedPlatformConnection && connectionBoundDraftFields.has(key)) {
    flow.selectPlatformConnection(flow.selectedPlatformConnection.id);
    return;
  }
  setDraftField(flow, key, value);
}

function PlatformStep({ flow }: { flow: AudioImportFlow }) {
  const { draft, platformConnections } = flow;
  const selectedConnection = flow.selectedPlatformConnection;
  const directoryLoading = flow.action === "recover";
  const selectedConnectionMissing = Boolean(
    draft.platformConnectionId
    && !platformConnections.some((item) => item.id === draft.platformConnectionId)
  );
  return (
    <Panel flow={flow}>
      <div className="audio-import-form-grid">
        <Field label="平台连接" required>
          {LABEL_DEMO_MODE ? (
            <input
              id="audio-import-platform-connection"
              data-testid="audio-import-platform-connection"
              required
              aria-required="true"
              aria-invalid={flow.invalidFieldIds.includes("audio-import-platform-connection") || undefined}
              aria-describedby="audio-import-validation-detail"
              value={draft.platformConnectionId}
              onChange={(event) => setDraftField(flow, "platformConnectionId", event.target.value)}
              placeholder="DEMO：输入 platform_connection_id"
            />
          ) : (
            <select
              id="audio-import-platform-connection"
              data-testid="audio-import-platform-connection"
              required
              aria-required="true"
              aria-busy={directoryLoading || undefined}
              aria-invalid={flow.invalidFieldIds.includes("audio-import-platform-connection") || undefined}
              aria-describedby="audio-import-validation-detail"
              disabled={directoryLoading || platformConnections.length === 0}
              value={draft.platformConnectionId}
              onChange={(event) => flow.selectPlatformConnection(event.target.value)}
            >
              <option value="">
                {directoryLoading
                  ? "正在读取平台连接…"
                  : platformConnections.length
                    ? "请选择已有平台连接"
                    : "没有可用的平台连接"}
              </option>
              {selectedConnectionMissing && (
                <option value={draft.platformConnectionId}>
                  当前绑定 {draft.platformConnectionId} · 目录未回读
                </option>
              )}
              {platformConnections.map((item) => (
                <option
                  key={item.id}
                  value={item.id}
                  disabled={item.status !== "active"}
                >{item.name} · {item.status}</option>
              ))}
            </select>
          )}
        </Field>
        {([
          ["platformTenantKey", "平台租户标识", "例如 aurora-auto", "audio-import-platform-tenant"],
          ["storeScope", "门店范围", "留空表示连接授权的全部门店", "audio-import-store-scope"]
        ] as const).map(([key, label, placeholder, id]) => (
          <Field key={key} label={label} required={key === "platformTenantKey"}>
            <input
              id={id}
              required={key === "platformTenantKey"}
              aria-required={key === "platformTenantKey"}
              readOnly={Boolean(selectedConnection) && key === "platformTenantKey"}
              aria-readonly={Boolean(selectedConnection) && key === "platformTenantKey" || undefined}
              aria-invalid={flow.invalidFieldIds.includes(id) || undefined}
              aria-describedby="audio-import-validation-detail"
              value={draft[key]}
              placeholder={placeholder}
              onChange={(event) => setConnectionBoundDraftField(flow, key, event.target.value)}
            />
          </Field>
        ))}
      </div>
      {selectedConnection && (
        <p className="audio-import-inline-warning" role="status">
          平台租户、API origin 与 credential_ref 已由连接版本冻结；
          门店范围默认继承连接授权，可收窄但不能扩张。
          连通性测试路径为 {selectedConnection.testPath}。
        </p>
      )}
      {flow.platformConnectionsDetail && (
        <p className="audio-import-inline-warning" role="status">
          平台连接目录不可读：{flow.platformConnectionsDetail}。
          {LABEL_DEMO_MODE
            ? "DEMO 可手动输入连接 ID。"
            : "生产模式已停止配置，请先修复平台连接目录后重试。"}
        </p>
      )}
    </Panel>
  );
}

function EndpointStep({ flow }: { flow: AudioImportFlow }) {
  const selectedConnection = flow.selectedPlatformConnection;
  return (
    <Panel flow={flow}>
      <div className="audio-import-form-grid">
        {endpointFields.map(([key, label, placeholder, id]) => {
          const frozenByConnection = Boolean(
            selectedConnection && connectionBoundDraftFields.has(key)
          );
          return (
            <Field key={key} label={label} required>
              <input
                id={id}
                required
                aria-required="true"
                readOnly={frozenByConnection}
                aria-readonly={frozenByConnection || undefined}
                aria-invalid={flow.invalidFieldIds.includes(id) || undefined}
                aria-describedby="audio-import-validation-detail"
                type={key === "pageSize" ? "number" : undefined}
                min={key === "pageSize" ? 1 : undefined}
                max={key === "pageSize" ? 250 : undefined}
                autoComplete={key === "credentialRef" ? "off" : undefined}
                placeholder={placeholder}
                value={flow.draft[key]}
                onChange={(event) => setConnectionBoundDraftField(
                  flow,
                  key,
                  key === "pageSize" ? Number(event.target.value) : event.target.value
                )}
              />
            </Field>
          );
        })}
        <Field label="分页方式" required>
          <input
            id="audio-import-pagination-mode"
            value="持久增量游标分页"
            readOnly
            required
            aria-required="true"
          />
        </Field>
      </div>
    </Panel>
  );
}

function VerificationStep({ flow }: { flow: AudioImportFlow }) {
  const { draft } = flow;
  const fields = flow.previewFields.slice(0, 6);
  return (
    <Panel flow={flow}>
      <div className="audio-import-verification-actions">
        <button
          id="audio-import-test-connection"
          type="button"
          data-testid="audio-import-test-connection"
          disabled={Boolean(flow.action)}
          onClick={flow.testConnection}
        >{flow.action === "test" ? "测试中" : "测试连接"}</button>
        <button
          id="audio-import-preview-records"
          type="button"
          data-testid="audio-import-preview-records"
          disabled={Boolean(flow.action) || !flow.connectionVerified}
          onClick={flow.previewRecordsFromSource}
        >{flow.action === "preview" ? "读取中" : "预览 3 条记录"}</button>
        <span className={flow.connectionVerified ? "is-ok" : ""}>
          连接 {flow.connectionVerified ? "已验证" : "待验证"}
        </span>
        <span className={flow.previewVerified ? "is-ok" : ""}>
          数据 {flow.previewVerified ? "已预览" : "待预览"}
        </span>
      </div>
      {!!flow.previewRecords.length && (
        <div className="audio-import-preview-table" data-testid="audio-import-record-preview">
          <div className="audio-import-preview-head">
            {fields.map((field) => <strong key={field}>{field}</strong>)}
          </div>
          {flow.previewRecords.map((item, index) => (
            <div className="audio-import-preview-row" key={String(item.id ?? item.recording_id ?? index)}>
              {fields.map((field) => {
                const value = formatAudioImportPreviewValue({
                  audioUrlFieldPath: draft.fieldMapping.audioUrl,
                  field,
                  record: item
                });
                return <span key={field} title={value}>{value}</span>;
              })}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

function MappingStep({ flow }: { flow: AudioImportFlow }) {
  return (
    <Panel flow={flow}>
      <datalist id="audio-import-preview-fields">
        {flow.previewFields.map((field) => <option key={field} value={field} />)}
      </datalist>
      <div className="audio-import-form-grid">
        {mappingFields.map(([key, label, required, id]) => (
          <Field key={key} label={label} required={required}>
            <input
              id={id}
              required={required}
              aria-required={required}
              aria-invalid={flow.invalidFieldIds.includes(id) || undefined}
              aria-describedby="audio-import-validation-detail"
              list="audio-import-preview-fields"
              value={flow.draft.fieldMapping[key]}
              onChange={(event) => flow.setDraft((current) => ({
                ...current,
                fieldMapping: {
                  ...current.fieldMapping,
                  [key]: event.target.value
                } as AudioImportFieldMapping
              }))}
            />
          </Field>
        ))}
      </div>
    </Panel>
  );
}

function TargetStep({ flow }: { flow: AudioImportFlow }) {
  return (
    <Panel flow={flow}>
      <div className="audio-import-form-grid">
        {targetFields.map(([key, label, id]) => (
          <Field key={key} label={label} required>
            <input
              id={id}
              required
              aria-required="true"
              aria-invalid={flow.invalidFieldIds.includes(id) || undefined}
              aria-describedby="audio-import-validation-detail"
              type={key === "initialWindowStart" ? "datetime-local" : undefined}
              value={flow.draft[key]}
              readOnly={key === "targetAssetKey"}
              onChange={(event) => setDraftField(flow, key, event.target.value)}
            />
          </Field>
        ))}
        <Field label="去重策略"><input value="外部录音 ID + 内容校验和" readOnly /></Field>
      </div>
    </Panel>
  );
}

export function AudioImportFormStep({ flow }: { flow: AudioImportFlow }) {
  return [
    <PlatformStep flow={flow} />,
    <EndpointStep flow={flow} />,
    <VerificationStep flow={flow} />,
    <MappingStep flow={flow} />,
    <TargetStep flow={flow} />
  ][flow.step - 1] ?? null;
}
