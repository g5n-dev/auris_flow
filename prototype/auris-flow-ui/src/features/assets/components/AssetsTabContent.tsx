import type { AssetsWorkspace } from "../useAssetsWorkspace";
import { AssetDetailPanel, AssetLineageSummaryPanel } from "./AssetDetailPanels";
import {
  AssetApiPanel,
  AssetBackfillPanel,
  AssetCompatibilityPanel,
  AssetQualityPanel,
  AssetRuntimePanel
} from "./AssetGovernancePanels";
import { AssetLineagePanel } from "./AssetLineagePanel";
import { AssetCatalogPanel, AssetChartPanel, AssetCommandPanel } from "./AssetOverviewPanels";

export function AssetsTabContent({ workspace }: { workspace: AssetsWorkspace }) {
  if (workspace.activeTab === "detail") {
    return (
      <>
        <AssetCommandPanel workspace={workspace} />
        <AssetDetailPanel workspace={workspace} />
        <AssetLineageSummaryPanel workspace={workspace} />
        <AssetApiPanel workspace={workspace} />
        <AssetRuntimePanel workspace={workspace} />
      </>
    );
  }

  if (workspace.activeTab === "lineage") {
    return (
      <>
        <AssetCommandPanel workspace={workspace} />
        <AssetLineagePanel wide workspace={workspace} />
        <AssetDetailPanel workspace={workspace} />
        <AssetBackfillPanel workspace={workspace} />
      </>
    );
  }

  if (workspace.activeTab === "backfill") {
    return (
      <>
        <AssetCommandPanel workspace={workspace} />
        <AssetBackfillPanel wide workspace={workspace} />
        <AssetLineageSummaryPanel workspace={workspace} />
        <AssetRuntimePanel workspace={workspace} />
      </>
    );
  }

  if (workspace.activeTab === "quality") {
    return (
      <>
        <AssetCommandPanel workspace={workspace} />
        <AssetChartPanel workspace={workspace} />
        <AssetQualityPanel workspace={workspace} />
        <AssetCompatibilityPanel workspace={workspace} />
        <AssetRuntimePanel workspace={workspace} />
        <AssetDetailPanel workspace={workspace} />
      </>
    );
  }

  if (workspace.activeTab === "tasks") {
    return (
      <>
        <AssetCommandPanel workspace={workspace} />
        <AssetRuntimePanel wide workspace={workspace} />
        <AssetApiPanel workspace={workspace} />
        <AssetCompatibilityPanel workspace={workspace} />
      </>
    );
  }

  return (
    <>
      <AssetCommandPanel workspace={workspace} />
      <AssetChartPanel workspace={workspace} />
      <AssetCatalogPanel workspace={workspace} />
      <AssetDetailPanel workspace={workspace} />
      <AssetLineageSummaryPanel workspace={workspace} />
      <AssetCompatibilityPanel workspace={workspace} />
      <AssetBackfillPanel workspace={workspace} />
      <AssetRuntimePanel workspace={workspace} />
      <AssetApiPanel workspace={workspace} />
    </>
  );
}
