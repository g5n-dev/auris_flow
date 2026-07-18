import { dataAssets } from "./dataAssets";
import { DataContractStrip } from "./components/DataContractStrip";
import { DataHeader } from "./components/DataHeader";
import { DataHierarchyView } from "./components/DataHierarchyView";
import { DataPivotPanel } from "./components/DataPivotPanel";
import { DataRelationView } from "./components/DataRelationView";
import { EventDataPage } from "./pages/EventDataPage";
import type { DataModuleProps } from "./types";
import { useDataWorkspace } from "./useDataWorkspace";
import { voiceprintRecords } from "./voiceprintFixtures";

export function DataModule(props: DataModuleProps) {
  const VoiceprintDataView = props.VoiceprintDataView;
  const workspace = useDataWorkspace(props);
  const {
    activeTab,
    dataAction,
    dataNotice,
    isContractCollapsed,
    isPivotCollapsed,
    isRelationView,
    openAssetsFromDataAsset,
    openConnectorImport,
    openListeningFromDataAsset,
    selectedAssetId,
    setActiveModule,
    setSelectedAssetId
  } = workspace;

  if (activeTab === "people") {
    return (
      <VoiceprintDataView
        records={voiceprintRecords}
        dataAssets={dataAssets}
        setActiveModule={setActiveModule}
        setSelectedAssetId={setSelectedAssetId}
        openListeningFromDataAsset={openListeningFromDataAsset}
        openAssetsFromDataAsset={openAssetsFromDataAsset}
      />
    );
  }
  if (activeTab === "events") {
    return (
      <EventDataPage
        dataAssets={dataAssets}
        selectedAssetId={selectedAssetId}
        setSelectedAssetId={setSelectedAssetId}
        openListeningFromDataAsset={openListeningFromDataAsset}
        openAssetsFromDataAsset={openAssetsFromDataAsset}
        openConnectorImport={openConnectorImport}
        isConnectorImporting={dataAction === "connector-import"}
      />
    );
  }
  return (
    <div
      className={[
        "data-reference-page",
        isRelationView ? "relation-view-page" : "",
        isPivotCollapsed ? "pivot-collapsed" : "pivot-expanded",
        isContractCollapsed ? "contract-collapsed" : "contract-expanded"
      ].join(" ")}
    >
      <DataHeader workspace={workspace} />
      <div className={`operation-toast data-operation-toast is-${dataNotice.status}`} role="status" aria-live="polite">
        <strong>{dataNotice.title}</strong>
        <span>{dataNotice.detail}</span>
      </div>
      <DataContractStrip workspace={workspace} />
      <DataPivotPanel workspace={workspace} />
      {isRelationView
        ? <DataRelationView workspace={workspace} />
        : <DataHierarchyView workspace={workspace} />}
    </div>
  );
}
