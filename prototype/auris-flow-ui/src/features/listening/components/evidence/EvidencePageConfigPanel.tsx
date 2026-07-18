import { defaultEvidencePageConfig, evidenceConfigPresets, evidenceModuleLabels } from "../../fixtures/evidenceFixtures";
import type { EvidenceModuleKey, EvidencePageConfig } from "../../types";
import { Eye, EyeOff, X } from "lucide-react";

export function EvidencePageConfigPanel({
  config,
  setConfig,
  close
}: {
  config: EvidencePageConfig;
  setConfig: (config: EvidencePageConfig) => void;
  close: () => void;
}) {
  const setModule = (key: EvidenceModuleKey, value: boolean) => {
    setConfig({
      ...config,
      modules: {
        ...config.modules,
        [key]: value
      }
    });
  };

  return (
    <div className="page-config-scrim" role="dialog" aria-label="证据审查页面配置">
      <aside className="page-config-panel">
        <div className="page-config-head">
          <div>
            <span>页面配置</span>
            <strong>按当前复核意图控制证据审查台的模块密度与可见性。</strong>
          </div>
          <button onClick={close} aria-label="关闭页面配置">
            <X size={16} />
          </button>
        </div>

        <section className="config-presets">
          {evidenceConfigPresets.map((preset) => (
            <button key={preset.key} onClick={() => setConfig(preset.config)}>
              <span>{preset.label}</span>
              <small>{preset.description}</small>
            </button>
          ))}
        </section>

        <section className="config-section">
          <div className="config-section-title">布局</div>
          <div className="segmented-row">
            {[
              ["comfortable", "标准"],
              ["compact", "紧凑"]
            ].map(([key, label]) => (
              <button
                key={key}
                className={config.density === key ? "active" : ""}
                onClick={() => setConfig({ ...config, density: key as EvidencePageConfig["density"] })}
              >
                {label}
              </button>
            ))}
          </div>
          <button className={config.showQueue ? "config-toggle on" : "config-toggle"} onClick={() => setConfig({ ...config, showQueue: !config.showQueue })}>
            <span>复核队列侧栏</span>
            <b>{config.showQueue ? "显示" : "隐藏"}</b>
          </button>
          <button
            className={config.showRightPanel ? "config-toggle on" : "config-toggle"}
            onClick={() => setConfig({ ...config, showRightPanel: !config.showRightPanel })}
          >
            <span>右侧证据面板</span>
            <b>{config.showRightPanel ? "显示" : "隐藏"}</b>
          </button>
        </section>

        <section className="config-section module-switch-list">
          <div className="config-section-title">模块</div>
          {(Object.keys(evidenceModuleLabels) as EvidenceModuleKey[]).map((key) => {
            const meta = evidenceModuleLabels[key];
            return (
              <button key={key} className={config.modules[key] ? "module-switch on" : "module-switch"} onClick={() => setModule(key, !config.modules[key])}>
                <span>
                  <b>{meta.label}</b>
                  <small>{meta.description}</small>
                </span>
                {config.modules[key] ? <Eye size={15} /> : <EyeOff size={15} />}
              </button>
            );
          })}
        </section>

        <div className="page-config-actions">
          <button onClick={() => setConfig(defaultEvidencePageConfig)}>恢复默认</button>
          <button onClick={close}>应用配置</button>
        </div>
      </aside>
    </div>
  );
}
