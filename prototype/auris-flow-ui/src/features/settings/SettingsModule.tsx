import { AudioServicePanels } from "./components/AudioServicePanels";
import { SettingsGovernancePanels } from "./components/SettingsGovernancePanels";
import { SettingsMainPanel } from "./components/SettingsMainPanel";
import type { SettingsModuleProps } from "./types";
import { useSettingsWorkspace } from "./useSettingsWorkspace";

export function SettingsModule(props: SettingsModuleProps) {
  const workspace = useSettingsWorkspace(props);
  const { settingsNotice } = workspace;
  return (
    <div className="settings-flow settings-intelligence-flow">
      <div className={`operation-toast settings-operation-toast is-${settingsNotice.status}`} role="status" aria-live="polite">
        <strong>{settingsNotice.title}</strong>
        <span>{settingsNotice.detail}</span>
      </div>
      <SettingsMainPanel workspace={workspace} />
      <SettingsGovernancePanels workspace={workspace} />
      <AudioServicePanels workspace={workspace} />
    </div>
  );
}
