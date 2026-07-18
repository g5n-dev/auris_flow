import { ShieldCheck, SlidersHorizontal } from "lucide-react";

import { StackedFacts, TimelineList } from "../../../shared/ui/FactDisplays";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import type { SettingsWorkspace } from "../useSettingsWorkspace";

export function SettingsGovernancePanels({ workspace }: { workspace: SettingsWorkspace }) {
  const {
    activeBundle,
    activeBundleEntries,
    activeDraft,
    guardLevel,
    selectedSetting,
    tabLabel
  } = workspace;
  return <div className="settings-pair">
        <section className="module-panel">
          <PanelHeader title="Policy Guard" subtitle={`${tabLabel} 的风险门禁`} icon={<ShieldCheck size={16} />} />
          <StackedFacts
            facts={[
              ["策略包", activeBundle.title],
              ["当前配置", selectedSetting.name],
              ["风险等级", selectedSetting.risk],
              ["影响配置", `${activeBundleEntries.length} 项`],
              ["门禁动作", guardLevel],
              ["跨租户访问", "拒绝"]
            ]}
          />
        </section>
        <section className="module-panel">
          <PanelHeader title="配置发布记录" subtitle="草稿、审批、发布、回滚" icon={<SlidersHorizontal size={16} />} />
          <TimelineList
	            items={[
	              ["v3.2.0", "已发布", `${tabLabel} 当前线上版本`],
	              [activeDraft ? "v3.3.0-rc1" : "未建草稿", activeDraft?.status ?? "空", activeDraft ? `${activeBundle.title} / ${selectedSetting.name} 等待下一步处理` : "选择策略或配置项后创建草稿"],
	              ["上次回滚", "06-24", "阈值过严导致人工队列暴涨"]
	            ]}
          />
        </section>
      </div>;
}
