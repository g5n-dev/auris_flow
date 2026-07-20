import { AlertTriangle, ArrowRight, Check, Download, GitBranch, Plus, Search, X } from "lucide-react";
import type { WorkspaceProjectSceneBinding } from "../shared/contracts/moduleWorkspaceGateway";
import type { ModuleDeepLink, ModuleKey } from "../shared/contracts/navigation";
import type { ModuleInteractionModel } from "../shared/contracts/moduleInteractions";
import type { ModuleConfig } from "../shared/contracts/modules";
import type { OperationStatus } from "../shared/contracts/operations";
import type { TopbarContextState } from "../shared/contracts/workspace";
import { MockMutationRecord, ModuleCommandMode, ModuleWriteArchitecture } from "../shared/contracts/application";

export function ModuleCommandPanel({
  config,
  interaction,
  mode,
  query,
  setQuery,
  activeFilter,
  setActiveFilter,
  feedback,
  setFeedback,
  status,
  setStatus,
  exportReceipt,
  moduleKey,
  writeArchitecture,
  mutationRecords,
  createMutationRecord,
  retryMutationRecord,
  setActiveModule,
  navigateToTarget,
  scopeContext,
  sceneBinding,
  sceneState,
  close
}: {
  config: ModuleConfig;
  interaction: ModuleInteractionModel;
  mode: ModuleCommandMode;
  query: string;
  setQuery: (value: string) => void;
  activeFilter: string;
  setActiveFilter: (value: string) => void;
  feedback: string;
  setFeedback: (value: string) => void;
  status: OperationStatus;
  setStatus: (status: OperationStatus) => void;
  exportReceipt: string;
  moduleKey: Exclude<ModuleKey, "listening">;
  writeArchitecture: ModuleWriteArchitecture;
  mutationRecords: MockMutationRecord[];
  createMutationRecord: (item: ModuleInteractionModel["crud"][number]) => void;
  retryMutationRecord: (id: string) => void;
  setActiveModule: (module: ModuleKey) => void;
  navigateToTarget: (target: ModuleDeepLink) => void;
  scopeContext: TopbarContextState;
  sceneBinding: WorkspaceProjectSceneBinding | null;
  sceneState: "pending" | "bound" | "unbound" | "error";
  close: () => void;
}) {
  const normalizedQuery = query.trim().toLowerCase();
  const searchResults = interaction.search.filter((item) =>
    !normalizedQuery || `${item.label} ${item.meta}`.toLowerCase().includes(normalizedQuery)
  );
  const activeFilterMeta = interaction.filters.find((filter) => filter.label === activeFilter) ?? interaction.filters[0];
  const sceneBindingRequired = !["tenants", "projects", "settings"].includes(moduleKey);
  const writeBlockedByScene = sceneBindingRequired && (!sceneBinding || sceneState !== "bound");
  const writeBlockedReason = sceneState === "pending"
    ? "正在读取当前项目的已发布 SceneProfile，加载完成后写入会自动恢复。"
    : sceneState === "error"
      ? "SceneProfile 绑定读取失败，写入暂不可用；请点击上方“重试场景绑定”。"
      : "当前项目尚未绑定已发布 SceneProfile；请前往项目管理完成发布与绑定。";
  const exportBlockedByScene = sceneBindingRequired && (!sceneBinding || sceneState !== "bound");
  const exportBlockedReason = sceneState === "pending"
    ? "正在读取当前项目的已发布 SceneProfile，加载完成后项目级导出会自动恢复。"
    : sceneState === "error"
      ? "SceneProfile 绑定读取失败，项目级导出暂不可用；请重试场景绑定。"
      : "当前项目尚未绑定已发布 SceneProfile；项目级导出已阻断，请先完成发布与绑定。";

  const commandTitle = mode === "search" ? "模块搜索" : mode === "filter" ? "模块筛选" : mode === "write" ? "创建 / 修改数据" : "导出回执";
  const latestMutation = mutationRecords[0];
  const writeStages: Array<[string, string]> = [
    ["1 草稿", "生成当前模块的变更对象"],
    ["2 校验", "校验租户/项目/版本上下文"],
    ["3 审批", "高风险动作进入人工门禁"],
    ["4 写入", "写入 BFF API 和审计记录"],
    ["5 回流", "同步下游资产、报告或队列"]
  ];
  const hasCreatedRunReceipt = mode === "export" && exportReceipt.includes("已创建导出运行");
  const recordButtonLabel =
    status === "error" ? "保留失败记录" : status === "success" || hasCreatedRunReceipt ? "固定回执" : "记录链路";
  const handleRecordCommand = () => {
    if (status === "error") {
      setFeedback("失败已保留在本地操作记录；请重试或检查 BFF / 权限 / 幂等键，未生成成功审计回执。");
      return;
    }
    if (status === "success" || hasCreatedRunReceipt) {
      setFeedback("当前操作回执已固定；包含后端 Trace 的动作可从审计链路回放。");
      return;
    }
    setFeedback("尚无可记录动作，请先完成搜索、筛选、写入或导出。");
  };

  return (
    <section className="module-command-panel" aria-label={`${config.title}全局操作`}>
      <div className="module-command-head">
        <div>
          <span>{commandTitle}</span>
          <strong>{config.title}</strong>
          <em>{activeFilterMeta ? `${activeFilterMeta.label} · ${activeFilterMeta.result}` : config.scope}</em>
        </div>
        <button type="button" aria-label="关闭模块操作面板" onClick={close}>
          <X size={15} />
        </button>
      </div>

      {mode === "search" && (
        <div className="module-command-search">
          <label>
            <span>输入关键词</span>
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={interaction.searchPlaceholder} autoFocus />
          </label>
          <div className="module-command-results">
            {searchResults.map((item) => (
              <button
                key={`${item.label}-${item.meta}`}
                type="button"
                onClick={() => {
                  setStatus("success");
                  setFeedback(`已定位「${item.label}」：${item.meta}`);
                  if (item.target) {
                    navigateToTarget(item.target);
                  } else {
                    setActiveModule(item.route);
                  }
                }}
              >
                <Search size={13} />
                <strong>{item.label}</strong>
                <span>{item.meta}</span>
              </button>
            ))}
            {searchResults.length === 0 && (
              <div className="module-command-empty">
                <Search size={14} />
                <span>没有匹配项，换一个实体、事件、标签或资产关键词。</span>
              </div>
            )}
          </div>
        </div>
      )}

      {mode === "filter" && (
        <div className="module-command-filter">
          <div className="module-filter-options">
            {interaction.filters.map((filter) => (
              <button
                key={filter.label}
                type="button"
                className={activeFilter === filter.label ? "active" : ""}
                onClick={() => {
                  setActiveFilter(filter.label);
                  setStatus("success");
                  setFeedback(`已应用筛选「${filter.label}」：${filter.detail}`);
                }}
              >
                <span>{filter.label}</span>
                <strong>{filter.result}</strong>
                <em>{filter.detail}</em>
              </button>
            ))}
          </div>
        </div>
      )}

      {mode === "write" && (
        <div className="module-command-write">
          <section className="module-write-architecture" aria-label="当前模块写入架构">
            <div className="module-write-title">
              <span>{moduleKey}</span>
              <strong>数据写入边界</strong>
              <em>{writeArchitecture.guardrail}</em>
            </div>
            <div className="module-write-facts">
              {[
                ["创建对象", writeArchitecture.createObject],
                ["修改对象", writeArchitecture.updateObject],
                ["写入 API", writeArchitecture.api],
                ["Payload", writeArchitecture.payload],
                ["下游同步", writeArchitecture.downstream]
              ].map(([label, value]) => (
                <div key={label}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                </div>
              ))}
            </div>
            <div className="module-write-gaps">
              <span>当前缺口</span>
              {writeArchitecture.missing.map((item) => (
                <p key={item}>
                  <AlertTriangle size={12} />
                  {item}
                </p>
              ))}
            </div>
          </section>

          <section className="module-write-actions" aria-label="可创建或修改的数据">
            {writeBlockedByScene && (
              <div className="module-write-gaps" data-testid="scene-profile-write-blocked">
                <span>当前不可用</span>
                <p>
                  <AlertTriangle size={12} />
                  {writeBlockedReason}
                </p>
              </div>
            )}
            <div className="module-write-stage-strip">
              {writeStages.map(([stage, detail], index) => (
                <button key={stage} type="button" className={latestMutation && index <= (latestMutation.status === "已提交" ? 4 : latestMutation.status === "待审批" ? 2 : latestMutation.status === "校验中" ? 1 : 0) ? "active" : ""}>
                  <b>{stage}</b>
                  <span>{detail}</span>
                </button>
              ))}
            </div>
            <div className="module-crud-strip" aria-label="当前模块可执行动作">
              {interaction.crud.map((item) => (
                <button
                  key={`${item.action}-${item.target}`}
                  type="button"
                  disabled={writeBlockedByScene}
                  title={writeBlockedByScene ? writeBlockedReason : undefined}
                  onClick={() => createMutationRecord(item)}
                >
                  <Plus size={13} />
                  <span>{item.action}</span>
                  <strong>{item.target}</strong>
                </button>
              ))}
            </div>
            <div className="module-write-ledger">
              <div className="module-write-ledger-head">
                <span>最近变更记录</span>
                <strong>{mutationRecords.length ? `${mutationRecords.length} 条` : "等待创建"}</strong>
              </div>
              {mutationRecords.length > 0 ? (
                mutationRecords.map((record) => (
                  <article key={record.id} className={`module-write-record status-${record.status}`}>
                    <div>
                      <span>{record.createdAt} · {record.status}</span>
                      <strong>{record.action} / {record.target}</strong>
                      <em>{record.entityKey}</em>
                    </div>
                    <p>{record.payload}</p>
                    <small>{record.api}</small>
                    {record.unavailableReason && (
                      <small data-testid="mutation-transition-disabled-reason">{record.unavailableReason}</small>
                    )}
                    <div className="module-write-record-actions">
                      <button
                        type="button"
                        disabled={record.status !== "失败"}
                        title={record.status === "失败" ? "使用同一用户意图与幂等键重试后端写入。" : record.unavailableReason}
                        onClick={() => retryMutationRecord(record.id)}
                      >
                        <Check size={12} />
                        {record.status === "失败" ? "重试写入" : record.status === "已提交" ? "已提交" : "等待后端"}
                      </button>
                      <button type="button" onClick={() => record.deepLink ? navigateToTarget(record.deepLink) : setActiveModule(record.route)}>
                        <ArrowRight size={12} />
                        {record.deepLink ? "查看具体详情" : "进入处理入口"}
                      </button>
                    </div>
                  </article>
                ))
              ) : (
                <div className="module-write-empty">
                  <GitBranch size={16} />
                  <span>点击上方动作后会生成草稿、API、Payload、护栏和下游同步记录。</span>
                </div>
              )}
            </div>
          </section>
        </div>
      )}

      {mode === "export" && (
        <div className="module-command-export">
          {exportBlockedByScene && (
            <div className="module-write-gaps" data-testid="scene-profile-export-blocked">
              <span>当前不可用</span>
              <p>
                <AlertTriangle size={12} />
                {exportBlockedReason}
              </p>
            </div>
          )}
          <div className="module-export-card">
            <Download size={16} />
            <div>
              <span>导出对象</span>
              <strong>{interaction.exportName}</strong>
              <em>{exportReceipt || "点击导出后生成可审计草稿"}</em>
            </div>
          </div>
          <div className="module-export-scope">
            {[
              ["租户", scopeContext.tenant],
              ["项目", scopeContext.project],
              ["时间范围", "当前模块时间分区"],
              ["筛选", activeFilterMeta ? `${activeFilterMeta.label} / ${activeFilterMeta.result}` : "当前视图"],
              ["版本", sceneBinding
                ? `SceneProfile ${sceneBinding.version.version} / ${sceneBinding.manifest_sha256.slice(0, 8)}`
                : `模型 ${scopeContext.model} / 标签 ${scopeContext.label}`]
            ].map(([label, value]) => (
              <span key={label}>
                <b>{label}</b>
                {value}
              </span>
            ))}
          </div>
        </div>
      )}

      <div className={`module-command-foot operation-toast is-${status}`}>
        <span>{feedback || "顶部操作会保留当前租户、项目、模型版本和标签版本上下文；时间以模块内分区为准。"}</span>
        <button type="button" disabled={status === "pending" && !hasCreatedRunReceipt} onClick={handleRecordCommand}>
          <Check size={13} />
          {recordButtonLabel}
        </button>
      </div>
    </section>
  );
}
