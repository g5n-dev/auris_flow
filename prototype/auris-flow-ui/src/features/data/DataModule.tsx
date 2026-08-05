import { useState } from "react";

import type { DataAssetItem } from "../../shared/contracts/dataAssets";
import type { OperationNotice } from "../../shared/contracts/operations";
import { LABEL_DEMO_MODE } from "../../shared/runtime/demoMode";
import { resolveProjectSceneLock } from "../../shared/runtime/projectSceneLock";
import { dataAssets } from "./dataAssets";
import { DataContractStrip } from "./components/DataContractStrip";
import { DataHeader } from "./components/DataHeader";
import { DataHierarchyView } from "./components/DataHierarchyView";
import { AudioImportDrawer } from "./components/AudioImportDrawer";
import { DataPivotPanel } from "./components/DataPivotPanel";
import { DataRelationView } from "./components/DataRelationView";
import {
  DATA_PROJECTION_SCHEMA_BLOCKED_REASON,
  projectDataAggregationItems
} from "./dataProjection";
import { EventDataPage } from "./pages/EventDataPage";
import type { DataModuleProps } from "./types";
import { useDataWorkspace } from "./useDataWorkspace";
import { useAudioImportFlow } from "./audioImportFlowController";
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

const AUDIO_IMPORT_COLD_START_ASSET = "auris/audio/raw_recordings";

function openImportedAudioSession(props: DataModuleProps, audioSessionId: string) {
  props.setSelectedAssetId(audioSessionId);
  props.navigateToTarget({
    module: "listening",
    objectKind: "audioSession",
    objectId: audioSessionId,
    title: `新音频会话 ${audioSessionId}`,
    detail: "音频导入 / 写后回读",
    focusMode: "detail",
    origin: {
      label: "数据资产 / 音频导入批次",
      module: "data",
      objectLabel: audioSessionId
    }
  });
}

function OperationToast({ notice }: { notice: OperationNotice }) {
  return (
    <div className={`operation-toast data-operation-toast is-${notice.status}`} role="status" aria-live="polite">
      <strong>{notice.title}</strong>
      <span>{notice.detail}</span>
    </div>
  );
}

function EmptyAudioImportWorkspace({ props }: { props: DataModuleProps }) {
  const [notice, setNotice] = useState<OperationNotice>({
    status: "idle",
    title: "尚无音频会话",
    detail: "先创建平台音频导入配置；首批数据写入后，会话将从 BFF 聚合读模型出现。"
  });
  const { lock: sceneProfileLock, blockedReason } = resolveProjectSceneLock(
    props.workspaceSceneBinding,
    props.workspaceSceneState
  );
  const flow = useAudioImportFlow({
    targetAssetKey: AUDIO_IMPORT_COLD_START_ASSET,
    sceneProfileLock,
    setDataNotice: setNotice
  });
  return (
    <div className="data-reference-page" data-testid="data-audio-empty-import-ready">
      <section className="data-reference-head">
        <div>
          <h2>项目数据资产</h2>
          <p>当前 BFF 已返回有效空集 · 0 个音频会话</p>
          <div className="data-asset-inline-status">
            <span>目标资产 {AUDIO_IMPORT_COLD_START_ASSET}</span>
            <span>等待首批平台音频</span>
          </div>
        </div>
        <div>
          <button
            type="button"
            className="data-connect-button"
            data-testid="data-connector-import"
            disabled={!sceneProfileLock}
            title={!sceneProfileLock ? blockedReason : undefined}
            onClick={flow.openDrawer}
          >
            {flow.open ? "配置中" : "新建导入配置"}
          </button>
          {!sceneProfileLock && (
            <span data-testid="data-connector-blocked-reason">{blockedReason}</span>
          )}
        </div>
      </section>
      <OperationToast notice={notice} />
      <section className="module-panel wide tenant-empty-state">
        <strong>没有会话不是配置入口的前置条件</strong>
        <span>通过上方“新建导入配置”关联外部平台，完成测试、预览、发布和立即拉取。</span>
      </section>
      <AudioImportDrawer
        flow={flow}
        onOpenListeningSession={(audioSessionId) =>
          openImportedAudioSession(props, audioSessionId)}
      />
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
  const audioImportFlow = useAudioImportFlow({
    targetAssetKey: AUDIO_IMPORT_COLD_START_ASSET,
    sceneProfileLock: workspace.sceneProfileLock,
    setDataNotice: workspace.setDataNotice
  });
  const {
    activeTab,
    dataNotice,
    isContractCollapsed,
    isPivotCollapsed,
    isRelationView,
    openAssetsFromDataAsset,
    openListeningFromDataAsset,
    selectedAssetId,
    setSelectedAssetId
  } = workspace;
  return (
    <>
      {activeTab === "events"
        ? (
          <EventDataPage
            dataAssets={dataItems}
            selectedAssetId={selectedAssetId}
            setSelectedAssetId={setSelectedAssetId}
            openListeningFromDataAsset={openListeningFromDataAsset}
            openAssetsFromDataAsset={openAssetsFromDataAsset}
            openConnectorImport={audioImportFlow.openDrawer}
            isConnectorImporting={audioImportFlow.open}
          />
        )
        : (
          <div
            className={[
              "data-reference-page",
              isRelationView ? "relation-view-page" : "",
              isPivotCollapsed ? "pivot-collapsed" : "pivot-expanded",
              isContractCollapsed ? "contract-collapsed" : "contract-expanded"
            ].join(" ")}
          >
            <DataHeader
              workspace={workspace}
              openConnectorImport={audioImportFlow.openDrawer}
              connectorImportOpen={audioImportFlow.open}
            />
            <OperationToast notice={dataNotice} />
            {!truthMode && <DataContractStrip workspace={workspace} />}
            {!truthMode && <DataPivotPanel workspace={workspace} />}
            {isRelationView
              ? <DataRelationView workspace={workspace} />
              : (
                <DataHierarchyView
                  workspace={workspace}
                  openConnectorImport={audioImportFlow.openDrawer}
                />
              )}
          </div>
        )}
      <AudioImportDrawer
        flow={audioImportFlow}
        onOpenListeningSession={(audioSessionId) =>
          openImportedAudioSession(props, audioSessionId)}
      />
    </>
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
    if (!projection.blockedReason) {
      return <EmptyAudioImportWorkspace props={props} />;
    }
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
