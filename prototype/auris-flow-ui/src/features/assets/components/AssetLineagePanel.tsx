import { GitBranch } from "lucide-react";

import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import type { AssetsWorkspace } from "../useAssetsWorkspace";
import { AssetLineage } from "./lineage/AssetLineage";
import { HotwordGovernanceLineage } from "./HotwordGovernanceLineage";
import { AuthoritativeAssetLineageView } from "./AuthoritativeAssetLineageView";

export function AssetLineagePanel({
  wide = false,
  workspace
}: {
  wide?: boolean;
  workspace: AssetsWorkspace;
}) {
  const { assetRows, createBackfillDraft, readScopeKey, selectedAsset, setSelectedAssetKey } = workspace;
  return (
    <section className={wide ? "module-panel wide lineage-panel asset-lineage-panel asset-lineage-expanded" : "module-panel lineage-panel asset-lineage-panel"}>
      <PanelHeader title="资产血缘" subtitle="上游来源、下游影响与当前选择联动" icon={<GitBranch size={16} />} />
      {LABEL_DEMO_MODE ? (
        <>
          <AssetLineage
            assetRows={assetRows}
            selectedAssetKey={selectedAsset.assetKey}
            onSelect={setSelectedAssetKey}
            onCreateBackfill={(assetKey) => createBackfillDraft(assetKey, "血缘影响分析生成")}
          />
          <HotwordGovernanceLineage workspace={workspace} />
        </>
      ) : (
        <>
          <AuthoritativeAssetLineageView assetKey={selectedAsset.assetKey} scopeKey={readScopeKey} />
          <HotwordGovernanceLineage workspace={workspace} />
        </>
      )}
    </section>
  );
}
