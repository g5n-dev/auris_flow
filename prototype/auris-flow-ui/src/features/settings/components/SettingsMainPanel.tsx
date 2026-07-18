import { Settings, Sparkles } from "lucide-react";

import {
  backendRunSucceeded,
  normalizeBackendRunStatus
} from "../../../shared/runtime/backendRunStatus";
import { backendReleaseRequester } from "../../../api/backendRuns";
import { actionFeedbackAttrs } from "../../../shared/runtime/feedbackAttributes";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import type { SettingsWorkspace } from "../useSettingsWorkspace";

export function SettingsMainPanel({ workspace }: { workspace: SettingsWorkspace }) {
  const {
    activeBundle,
    activeBundleEntries,
    activeDraft,
    activeTab,
    approveDisabledReason,
    approveSettingDraft,
    backendSettingDraftId,
    currentBundleEntries,
    currentUser,
    discardDisabledReason,
    discardSettingDraft,
    draftId,
    guardLevel,
    humanActionDisabledReason,
    rejectDisabledReason,
    rejectSettingDraft,
    rows,
    selectBundle,
    selectBundleEntry,
    selectedSetting,
    setSelectedSettingName,
    settingEditor,
    settingPolicyBundles,
    settingsAction,
    settingsReleaseGate,
    smartActionDisabledReason,
    submitPublishDisabledReason,
    tabLabel,
    updateDraft,
    updateSettingEditor,
    validateDisabledReason
  } = workspace;
  return <section className="module-panel wide">
        <PanelHeader title={tabLabel} subtitle="先选择策略意图，再落到底层配置；高风险变更只写草稿，发布前进入 Policy Guard" icon={<Settings size={16} />} sticky />
        <div className="settings-policy-layer" aria-label="策略抽象层">
          <div className="settings-policy-summary">
            <span>策略抽象层</span>
            <strong>{activeBundle.title}</strong>
            <em>{activeBundle.intent}</em>
          </div>
          <div className="settings-policy-bundles">
            {settingPolicyBundles.map((bundle) => (
              <button
                key={bundle.id}
                type="button"
                className={`settings-policy-bundle ${bundle.tone} ${activeBundle.id === bundle.id ? "active" : ""}`}
                aria-pressed={activeBundle.id === bundle.id}
                onClick={() => selectBundle(bundle)}
              >
                <span>{bundle.risk}</span>
                <strong>{bundle.title}</strong>
                <em>{bundle.objective}</em>
              </button>
            ))}
          </div>
          <div className="settings-policy-map">
            <div className="settings-policy-agent">
              <span><Sparkles size={14} /> Agent 编排</span>
              <strong>{activeBundle.outcome}</strong>
              <em>{activeBundle.gates.join(" / ")}</em>
            </div>
            <div className="settings-policy-targets">
              {activeBundleEntries.map((entry) => (
                <button
                  key={`${entry.tabId}-${entry.row.name}`}
                  type="button"
                  className={activeTab === entry.tabId && selectedSetting.name === entry.row.name ? "active" : ""}
                  onClick={() => selectBundleEntry(entry)}
                >
                  <span>{entry.tabLabel}</span>
                  <strong>{entry.row.name}</strong>
                  <em>{entry.row.value} · {entry.row.owner}</em>
                </button>
              ))}
            </div>
          </div>
        </div>
        <div className="settings-smart-layout">
          <div className="setting-list settings-select-list">
            <div className="settings-list-title">
              <span>底层配置项</span>
              <strong>{tabLabel}</strong>
              <em>当前策略命中 {currentBundleEntries.length} 项</em>
            </div>
            {rows.map((row) => (
              <button
                key={row.name}
                type="button"
                className={selectedSetting.name === row.name ? "setting-row active" : "setting-row"}
                onClick={() => setSelectedSettingName(row.name)}
              >
                <span>{row.name}</span>
                <strong>{row.value}</strong>
                <em>{row.desc}</em>
              </button>
            ))}
          </div>
          <aside className="settings-smart-view" aria-label={`${selectedSetting.name} 智能查看`}>
            <div className="settings-smart-head">
              <span>{activeBundle.title}</span>
              <strong>{selectedSetting.name}</strong>
              <em>{selectedSetting.risk}</em>
            </div>
            <p>{selectedSetting.desc}</p>
            <div className="settings-smart-facts">
              {[
                ["当前值", selectedSetting.value],
                ["负责人", selectedSetting.owner],
                ["影响范围", selectedSetting.scope],
                ["策略归属", activeBundle.owner],
                ["发布门禁", guardLevel]
              ].map(([label, value]) => (
                <span key={label}>
                  <b>{label}</b>
                  {value}
                </span>
              ))}
            </div>
            <div className="settings-smart-links">
              <span>{selectedSetting.api}</span>
              <span>{selectedSetting.asset}</span>
            </div>
            <div className="settings-smart-policy">
              <b>策略</b>
              <p>{selectedSetting.policy}</p>
            </div>
            <div className="settings-edit-form" aria-label="设置字段编辑">
              <label>
                <span>修改后的值</span>
                <input value={settingEditor.value} onChange={(event) => updateSettingEditor("value", event.target.value)} />
              </label>
              <label>
                <span>负责人</span>
                <input value={settingEditor.owner} onChange={(event) => updateSettingEditor("owner", event.target.value)} />
              </label>
              <label>
                <span>审批人</span>
                <input value={settingEditor.approver} onChange={(event) => updateSettingEditor("approver", event.target.value)} />
              </label>
              <label>
                <span>回滚版本</span>
                <input value={settingEditor.rollback} onChange={(event) => updateSettingEditor("rollback", event.target.value)} />
              </label>
              <label className="wide">
                <span>变更原因</span>
                <textarea value={settingEditor.reason} onChange={(event) => updateSettingEditor("reason", event.target.value)} rows={2} />
              </label>
              <label className="wide">
                <span>策略说明</span>
                <textarea value={settingEditor.policy} onChange={(event) => updateSettingEditor("policy", event.target.value)} rows={3} />
              </label>
            </div>
            <div className="settings-smart-actions">
              <button
                type="button"
                {...actionFeedbackAttrs("s,e")}
                onClick={() => updateDraft("草稿")}
              >
                编辑草稿
              </button>
              <button
                type="button"
                disabled={Boolean(validateDisabledReason)}
                {...actionFeedbackAttrs("p,s,e,d")}
                title={validateDisabledReason || "运行校验"}
                aria-describedby={validateDisabledReason ? "settings-smart-disabled-reason" : undefined}
                onClick={() => updateDraft("校验中")}
              >
                {settingsAction === "validate" ? "校验中" : "运行校验"}
              </button>
              <button
                type="button"
                disabled={Boolean(submitPublishDisabledReason)}
                {...actionFeedbackAttrs("s,e,d")}
                title={submitPublishDisabledReason || "提交发布"}
                aria-describedby={submitPublishDisabledReason ? "settings-smart-disabled-reason" : undefined}
                onClick={() => updateDraft("待发布")}
              >
                提交发布
              </button>
              {smartActionDisabledReason && (
                <div id="settings-smart-disabled-reason" className="disabled-reason settings-inline-disabled-reason">
                  {smartActionDisabledReason}
                </div>
              )}
            </div>
          </aside>
          <aside className="settings-draft-card" aria-label="配置草稿">
            <div>
              <span>变更草稿</span>
              <strong>{activeDraft ? activeDraft.status : "未创建"}</strong>
            </div>
            {activeDraft ? (
              <>
                <code>{backendSettingDraftId ?? draftId}</code>
                <p>{activeDraft.bundle} / {activeDraft.name}：{activeDraft.value}</p>
                <em>
                  {activeDraft.status === "已发布"
                    ? `已写入本地演示设置，回滚点 ${activeDraft.rollback} 已记录。`
                    : activeDraft.status === "已拒绝"
                      ? "已退回，线上配置未变。"
                      : activeDraft.status === "待发布"
                        ? `等待 ${activeDraft.approver} 审批后写入。`
                        : activeDraft.status === "校验中"
                          ? "正在检查影响资产、权限和审计覆盖。"
                          : "草稿只保存在当前项目版本，不影响线上。"}
                </em>
                <div className="settings-draft-meta">
                  <span>Owner: {activeDraft.owner}</span>
                  <span>审批: {activeDraft.approver}</span>
                  <span>回滚: {activeDraft.rollback}</span>
                </div>
                <small>{activeDraft.reason}</small>
                <div className="settings-human-actions">
                  <button
                    type="button"
                    disabled={Boolean(rejectDisabledReason)}
                    {...actionFeedbackAttrs("s,e,d")}
                    title={rejectDisabledReason || "提交人审"}
                    aria-describedby={rejectDisabledReason ? "settings-human-disabled-reason" : undefined}
                    onClick={() => updateDraft("待发布")}
                  >
                    提交人审
                  </button>
                  <button
                    type="button"
                    disabled={Boolean(approveDisabledReason)}
                    {...actionFeedbackAttrs("p,e,d")}
                    title={approveDisabledReason || "创建发布门禁"}
                    aria-describedby={approveDisabledReason ? "settings-human-disabled-reason" : undefined}
                    onClick={approveSettingDraft}
                  >
                    {settingsAction === "approve"
                      ? "处理中"
                      : settingsReleaseGate
                        ? normalizeBackendRunStatus(settingsReleaseGate.status) === "blocked"
                          ? backendReleaseRequester(settingsReleaseGate) === currentUser.userId
                            ? "刷新发布状态"
                            : "审批通过写入"
                          : backendRunSucceeded(settingsReleaseGate.status)
                            ? "已发布"
                            : "刷新发布状态"
                        : "创建发布门禁"}
                  </button>
                  <button
                    type="button"
                    disabled={Boolean(rejectDisabledReason)}
                    {...actionFeedbackAttrs("e,d")}
                    title={rejectDisabledReason || "退回草稿"}
                    aria-describedby={rejectDisabledReason ? "settings-human-disabled-reason" : undefined}
                    onClick={rejectSettingDraft}
                  >
                    退回
                  </button>
                  {humanActionDisabledReason && (
                    <div id="settings-human-disabled-reason" className="disabled-reason settings-inline-disabled-reason">
                      {humanActionDisabledReason}
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  disabled={Boolean(discardDisabledReason)}
                  {...actionFeedbackAttrs("s,d")}
                  title={discardDisabledReason || "清除草稿"}
                  aria-describedby={discardDisabledReason ? "settings-human-disabled-reason" : undefined}
                  onClick={discardSettingDraft}
                >
                  放弃草稿
                </button>
              </>
            ) : (
              <p>点击左侧任一配置项后，可在智能查看里创建草稿、跑校验或提交发布。</p>
            )}
          </aside>
        </div>
      </section>;
}
