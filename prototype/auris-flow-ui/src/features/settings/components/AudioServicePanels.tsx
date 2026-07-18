import { BrainCircuit, Database, GitBranch } from "lucide-react";

import { actionFeedbackAttrs } from "../../../shared/runtime/feedbackAttributes";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import {
  asrServiceProfile,
  audioServiceObservabilityRows,
  audioServiceOptimizationRows,
  audioServiceParamGroups
} from "../catalog";
import type { SettingsWorkspace } from "../useSettingsWorkspace";

export function AudioServicePanels({ workspace }: { workspace: SettingsWorkspace }) {
  const {
    activeTab,
    providerTestDisabledReason,
    saveAsrServiceDraft,
    settingsAction,
    submitPublishDisabledReason,
    testAsrServiceConnection,
    updateDraft
  } = workspace;
  return (
    <>
      {activeTab === "model" && (
        <>
          <section className="module-panel wide asr-service-panel">
            <PanelHeader title="音频智能服务接入" subtitle="统一注册 VAD / Diar / ASR；任务配置只引用服务版本和 provider 参数" icon={<BrainCircuit size={16} />} />
            <div className="asr-service-hero">
              <div>
                <span>{asrServiceProfile.name}</span>
                <strong>{asrServiceProfile.serviceId}</strong>
                <p>{asrServiceProfile.endpoint}</p>
              </div>
              <b>{asrServiceProfile.version}</b>
            </div>
            <div className="audio-provider-grid">
              {asrServiceProfile.providers.map(([name, serviceId, desc], index) => (
                <button key={name} type="button" className={index === 2 ? "active" : ""}>
                  <span>{name}</span>
                  <strong>{serviceId}</strong>
                  <em>{desc}</em>
                </button>
              ))}
            </div>
            <div className="asr-service-grid">
              {[
                ["认证", asrServiceProfile.auth],
                ["超时", asrServiceProfile.timeout],
                ["重试", asrServiceProfile.retry],
                ["Owner", asrServiceProfile.owner],
                ["IO Manager", asrServiceProfile.ioManager]
              ].map(([label, value]) => (
                <div key={label}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                </div>
              ))}
            </div>
            <div className="settings-smart-actions asr-service-actions">
              <button
                type="button"
                disabled={Boolean(providerTestDisabledReason)}
                data-action-key="settings-provider-test"
                {...actionFeedbackAttrs("p,s,e,d")}
                title={providerTestDisabledReason || "测试连通性"}
                aria-describedby={providerTestDisabledReason ? "settings-provider-disabled-reason" : undefined}
                onClick={testAsrServiceConnection}
              >
                {settingsAction === "asr-test" ? "测试中" : "测试连通性"}
              </button>
              <button
                type="button"
                {...actionFeedbackAttrs("s")}
                onClick={saveAsrServiceDraft}
              >
                保存服务草稿
              </button>
              <button
                type="button"
                disabled={Boolean(submitPublishDisabledReason)}
                {...actionFeedbackAttrs("s,e,d")}
                title={submitPublishDisabledReason || "提交服务发布"}
                aria-describedby={submitPublishDisabledReason ? "settings-provider-disabled-reason" : undefined}
                onClick={() => updateDraft("待发布")}
              >
                提交服务发布
              </button>
              {(providerTestDisabledReason || submitPublishDisabledReason) && (
                <div id="settings-provider-disabled-reason" className="disabled-reason settings-inline-disabled-reason">
                  {providerTestDisabledReason || submitPublishDisabledReason}
                </div>
              )}
            </div>
            <div className="asr-contract-columns">
              <section>
                <span>请求契约</span>
                {asrServiceProfile.request.map(([key, value]) => (
                  <div key={key} className="service-contract-row">
                    <span>{key}</span>
                    <strong>{value}</strong>
                  </div>
                ))}
              </section>
              <section>
                <span>响应契约</span>
                {asrServiceProfile.response.map(([key, value]) => (
                  <div key={key} className="service-contract-row">
                    <span>{key}</span>
                    <strong>{value}</strong>
                  </div>
                ))}
              </section>
            </div>
            <div className="audio-service-ops-grid">
              <section className="audio-param-panel">
                <div className="audio-ops-head">
                  <span>请求参数模板</span>
                  <strong>provider + pipeline + tuning</strong>
                </div>
                <div className="audio-param-grid">
                  {audioServiceParamGroups.map((group) => (
                    <div key={group.title} className="audio-param-card">
                      <strong>{group.title}</strong>
                      {group.rows.map(([key, value]) => (
                        <span key={key}>
                          <b>{key}</b>
                          <em>{value}</em>
                        </span>
                      ))}
                    </div>
                  ))}
                </div>
              </section>
              <section className="audio-observe-panel">
                <div className="audio-ops-head">
                  <span>Provider 可观测</span>
                  <strong>P95 / VAD / DER / WER / 成本</strong>
                </div>
                <div className="audio-provider-metrics">
                  {audioServiceObservabilityRows.map(([provider, latency, vadMiss, der, wer, cost, role]) => (
                    <button key={provider} type="button" className={role === "主路由" ? "active" : ""}>
                      <strong>{provider}</strong>
                      <span>{latency}</span>
                      <span>{vadMiss}</span>
                      <span>{der}</span>
                      <span>{wer}</span>
                      <span>{cost}</span>
                      <em>{role}</em>
                    </button>
                  ))}
                </div>
              </section>
              <section className="audio-optimization-panel">
                <div className="audio-ops-head">
                  <span>优化与回退</span>
                  <strong>能被 Agent 和人工共同调参</strong>
                </div>
                <div className="audio-optimization-list">
                  {audioServiceOptimizationRows.map(([name, policy, detail]) => (
                    <button key={name} type="button">
                      <span>{name}</span>
                      <strong>{policy}</strong>
                      <em>{detail}</em>
                    </button>
                  ))}
                </div>
              </section>
            </div>
          </section>
          <div className="settings-pair">
            <section className="module-panel asr-service-panel">
              <PanelHeader title="输出拆分" subtitle="转写和说话人分离不能混成一个字段" icon={<GitBranch size={16} />} />
              <div className="service-contract-list">
                {asrServiceProfile.assets.map(([asset, source, detail]) => (
                  <div key={asset} className="service-contract-row">
                    <span>{asset}</span>
                    <strong>{source}</strong>
                    <em>{detail}</em>
                  </div>
                ))}
              </div>
            </section>
            <section className="module-panel asr-service-panel">
              <PanelHeader title="执行映射" subtitle="同一音频服务调用，按资产拆分追踪和回填" icon={<Database size={16} />} />
              <div className="service-contract-list">
                {asrServiceProfile.dagster.map(([label, value, detail]) => (
                  <div key={label} className="service-contract-row">
                    <span>{label}</span>
                    <strong>{value}</strong>
                    <em>{detail}</em>
                  </div>
                ))}
              </div>
            </section>
          </div>
        </>
      )}
    </>
  );
}
