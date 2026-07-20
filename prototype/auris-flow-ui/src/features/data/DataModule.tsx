import type { DataAssetItem } from "../../shared/contracts/dataAssets";
import { LABEL_DEMO_MODE } from "../../shared/runtime/demoMode";
import { dataAssets } from "./dataAssets";
import { DataContractStrip } from "./components/DataContractStrip";
import { DataHeader } from "./components/DataHeader";
import { DataHierarchyView } from "./components/DataHierarchyView";
import { DataPivotPanel } from "./components/DataPivotPanel";
import { DataRelationView } from "./components/DataRelationView";
import {
  DATA_PROJECTION_SCHEMA_BLOCKED_REASON,
  projectDataAggregationItems
} from "./dataProjection";
import { EventDataPage } from "./pages/EventDataPage";
import type { DataModuleProps } from "./types";
import { useDataWorkspace } from "./useDataWorkspace";
import { voiceprintRecords } from "./voiceprintFixtures";

function TruthVoiceprintUnavailable() {
  return (
    <div className="data-reference-page voiceprint-data-page">
      <section className="module-panel wide tenant-empty-state" data-testid="data-voiceprint-unavailable" role="status">
        <strong>候选读模型未就绪</strong>
        <span>当前 BFF 尚未提供经过服务端质量校验、租户项目隔离和证据绑定的声纹候选；本地 voiceprint fixture 不作为生产事实。</span>
        <button type="button" data-testid="voiceprint-enrollment-disabled" disabled>
          声纹入库禁用
        </button>
      </section>
    </div>
  );
}

function TruthDataUnavailable({
  testId,
  title,
  detail,
  connectorBlockedReason
}: {
  testId: string;
  title: string;
  detail: string;
  connectorBlockedReason?: string;
}) {
  return (
    <div className="data-reference-page">
      <section className="module-panel wide tenant-empty-state" data-testid={testId} role="status">
        <strong>{title}</strong>
        <span>{detail}</span>
        {connectorBlockedReason && (
          <>
            <button type="button" data-testid="data-connector-import" disabled>连接器导入禁用</button>
            <span data-testid="data-connector-blocked-reason">{connectorBlockedReason}</span>
          </>
        )}
      </section>
    </div>
  );
}

function DataWorkspaceContent({
  props,
  dataItems,
  truthMode
}: {
  props: DataModuleProps;
  dataItems: DataAssetItem[];
  truthMode: boolean;
}) {
  const workspace = useDataWorkspace({ ...props, dataItems, truthMode });
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
  if (activeTab === "events") {
    return (
      <EventDataPage
        dataAssets={dataItems}
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
      {!truthMode && <DataContractStrip workspace={workspace} />}
      {!truthMode && <DataPivotPanel workspace={workspace} />}
      {isRelationView
        ? <DataRelationView workspace={workspace} />
        : <DataHierarchyView workspace={workspace} />}
    </div>
  );
}

export function DataModule(props: DataModuleProps) {
  const truthMode = !LABEL_DEMO_MODE;
  if (!truthMode) {
    if (props.activeTab === "people") {
      const VoiceprintDataView = props.VoiceprintDataView;
      return (
        <VoiceprintDataView
          records={voiceprintRecords}
          dataAssets={dataAssets}
          setActiveModule={props.setActiveModule}
          setSelectedAssetId={props.setSelectedAssetId}
          openListeningFromDataAsset={props.openListeningFromDataAsset}
          openAssetsFromDataAsset={props.openAssetsFromDataAsset}
        />
      );
    }
    return <DataWorkspaceContent props={props} dataItems={dataAssets} truthMode={false} />;
  }

  if (props.activeTab === "people") return <TruthVoiceprintUnavailable />;
  if (props.activeTab === "events") {
    return (
      <TruthDataUnavailable
        testId="data-events-unavailable"
        title="事件读模型未就绪"
        detail="认证事件及单据关联尚未由当前 BFF 投影提供；未展示本地事件 fixture。"
      />
    );
  }
  if (props.activeTab === "relations") {
    return (
      <TruthDataUnavailable
        testId="data-relations-unavailable"
        title="关联读模型未就绪"
        detail="跨对象关系与断链修复上下文尚未由当前 BFF 投影提供；未展示本地关系 fixture。"
      />
    );
  }

  const projection = props.projectionSource === "bff"
    ? projectDataAggregationItems(props.projectionItems)
    : { items: [], blockedReason: DATA_PROJECTION_SCHEMA_BLOCKED_REASON };
  if (!projection.items.length) {
    return (
      <TruthDataUnavailable
        testId="data-projection-invalid"
        title="音频会话读模型不可用"
        detail="BFF 返回为空或不符合 audio-sessions aggregation 叶子契约；未回落本地数据 fixture。"
        connectorBlockedReason={projection.blockedReason}
      />
    );
  }
  return <DataWorkspaceContent props={props} dataItems={projection.items} truthMode />;
}
