import type { InsightsController } from "../controller/useInsightsController";
import { InsightsDashboardPanel } from "./InsightsDashboardPanel";
import { InsightsHotwordPanel } from "./InsightsHotwordPanel";
import { InsightsScopePanel } from "./InsightsScopePanel";
import { InsightsSidePanel } from "./InsightsSidePanel";

export function InsightsWorkspaceView({ controller }: { controller: InsightsController }) {
  const { currentTab, currentTabLabel } = controller;
  return (
    <div
      className="insight-command-shell"
      data-audit-tab-content={currentTabLabel}
      data-audit-tab-key={currentTab}
      aria-label={`${currentTabLabel}主内容`}
    >
      <InsightsScopePanel controller={controller} />
      {currentTab === "quality" && <InsightsHotwordPanel controller={controller} />}
      <div className="insight-content-stack">
        <InsightsDashboardPanel controller={controller} />
        <InsightsSidePanel controller={controller} />
      </div>
    </div>
  );
}
