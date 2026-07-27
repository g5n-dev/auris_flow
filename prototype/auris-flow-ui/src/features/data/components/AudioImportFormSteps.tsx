import type { AudioImportDraft, AudioImportFieldMapping } from "../audioImportFlowModel";
import type { AudioImportFlow } from "../audioImportFlowController";
import { formatAudioImportPreviewValue } from "../audioImportPreviewSecurity";

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
  ["name", "配置名称"],
  ["baseUrl", "API 地址", "https://platform.example.com"],
  ["requestPath", "录音清单路径", "/v1/recordings"],
  ["credentialRef", "credential_ref", "secret://tenant/platform-audio-api"],
  ["pageSize", "每页记录数"],
  ["cursorParam", "增量游标参数名"],
  ["nextCursorPath", "下一持久游标字段路径"]
] as const;
const mappingFields = [
  ["externalRecordId", "外部录音 ID", true],
  ["audioUrl", "音频 URL", true],
  ["startedAt", "通话时间", true],
  ["agentRef", "员工 ID", false],
  ["storeRef", "门店 ID", false],
  ["deviceRef", "设备 ID", false],
  ["durationMs", "时长（毫秒）", false]
] as const;
const targetFields = [
  ["cursorField", "唯一递增游标字段"],
  ["initialWindowStart", "首次拉取开始时间"],
  ["targetAssetKey", "目标音频资产"]
] as const;

function setDraftField(
  flow: AudioImportFlow,
  key: keyof AudioImportDraft,
  value: string | number
) {
  flow.setDraft((current) => ({ ...current, [key]: value }));
}

function PlatformStep({ flow }: { flow: AudioImportFlow }) {
  const { draft, platformConnections } = flow;
  return (
    <Panel flow={flow}>
      <div className="audio-import-form-grid">
        <Field label="平台连接" required>
          {platformConnections.length ? (
            <select
              data-testid="audio-import-platform-connection"
              value={draft.platformConnectionId}
              onChange={(event) => setDraftField(flow, "platformConnectionId", event.target.value)}
            >
              <option value="">请选择已有平台连接</option>
              {platformConnections.map((item) => (
                <option key={item.id} value={item.id}>{item.name} · {item.status}</option>
              ))}
            </select>
          ) : (
            <input
              data-testid="audio-import-platform-connection"
              value={draft.platformConnectionId}
              onChange={(event) => setDraftField(flow, "platformConnectionId", event.target.value)}
              placeholder="输入已有 platform_connection_id"
            />
          )}
        </Field>
        {([
          ["platformTenantKey", "平台租户标识", "例如 aurora-auto"],
          ["storeScope", "门店范围", "留空表示连接授权的全部门店"]
        ] as const).map(([key, label, placeholder]) => (
          <Field key={key} label={label} required={key === "platformTenantKey"}>
            <input
              value={draft[key]}
              placeholder={placeholder}
              onChange={(event) => setDraftField(flow, key, event.target.value)}
            />
          </Field>
        ))}
      </div>
      {flow.platformConnectionsDetail && (
        <p className="audio-import-inline-warning" role="status">
          平台连接目录不可读：{flow.platformConnectionsDetail}。可输入连接 ID 后真实测试。
        </p>
      )}
    </Panel>
  );
}

function EndpointStep({ flow }: { flow: AudioImportFlow }) {
  return (
    <Panel flow={flow}>
      <div className="audio-import-form-grid">
        {endpointFields.map(([key, label, placeholder]) => (
          <Field key={key} label={label} required>
            <input
              type={key === "pageSize" ? "number" : undefined}
              min={key === "pageSize" ? 1 : undefined}
              max={key === "pageSize" ? 250 : undefined}
              autoComplete={key === "credentialRef" ? "off" : undefined}
              placeholder={placeholder}
              value={flow.draft[key]}
              onChange={(event) => setDraftField(
                flow,
                key,
                key === "pageSize" ? Number(event.target.value) : event.target.value
              )}
            />
          </Field>
        ))}
        <Field label="分页方式" required><input value="持久增量游标分页" readOnly /></Field>
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
          type="button"
          data-testid="audio-import-test-connection"
          disabled={Boolean(flow.action)}
          onClick={flow.testConnection}
        >{flow.action === "test" ? "测试中" : "测试连接"}</button>
        <button
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
        {mappingFields.map(([key, label, required]) => (
          <Field key={key} label={label} required={required}>
            <input
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
        {targetFields.map(([key, label]) => (
          <Field key={key} label={label} required>
            <input
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
