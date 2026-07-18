import type { ModuleDeepLink } from "../../../../shared/contracts/navigation";
import { listeningDeviceBadges, reviewQueueMockData } from "../../fixtures/evidenceFixtures";
import type { ReviewSample } from "../../fixtures/reviewSamples";
import { getReviewQueueMock } from "../../fixtures/reviewSamples";
import type { EvidencePageConfig, ListeningDeviceKey, ListeningScope, MarkState, Mode, PanelTab } from "../../types";
import { AnnotationToolbar, BadgeBar, IslandBar } from "./annotationControls";
import { AnnotationMinimap } from "./minimap/AnnotationMinimap";
import { EvidencePanel } from "./panel/EvidencePanel";
import { ReceptionLinkPanel } from "./ReceptionLinkPanel";
import { AnnotationSpine, AnnotationSplitView } from "./trackDisplays";
import { WaveformPanel } from "./waveform/WaveformPanel";
import { SlidersHorizontal } from "lucide-react";
import { useState } from "react";

export function EvidenceMode(props: {
  panelTab: PanelTab;
  setPanelTab: (tab: PanelTab) => void;
  setMode: (mode: Mode) => void;
  setListeningScope: (scope: ListeningScope) => void;
  selectedWindow: string;
  setSelectedWindow: (value: string) => void;
  markState: MarkState;
  setMarkState: (state: MarkState) => void;
  selectedLabel: string;
  agentState: "pending" | "accepted" | "rejected";
  setAgentState: (state: "pending" | "accepted" | "rejected") => void;
  activeQueue: string;
  setActiveQueue: (value: string) => void;
  activeChip: string;
  setActiveChip: (value: string) => void;
  pageConfig: EvidencePageConfig;
  sample: ReviewSample;
  navigateToTarget: (target: ModuleDeepLink) => void;
  completedCount: number;
  onConfirmNext: () => void | Promise<void>;
  confirmPending: boolean;
}) {
  const [activeIsland, setActiveIsland] = useState("S129");
  const [activeDeviceKey, setActiveDeviceKey] = useState<ListeningDeviceKey>("A-1001");
  const [receptionLocatorOpen, setReceptionLocatorOpen] = useState(false);
  const [activeTrack, setActiveTrack] = useState<string>("entity");
  const [hiddenTracks, setHiddenTracks] = useState<Record<string, boolean>>({});
  const activeQueueMock = getReviewQueueMock(props.sample.queue);
  const activeDevice = listeningDeviceBadges.find((device) => device.key === activeDeviceKey) ?? listeningDeviceBadges[0];
  const showWaveformArea = props.pageConfig.modules.waveform || props.pageConfig.modules.tracks;
  const showReceptionLink = props.pageConfig.modules.minimap;
  const receptionLinkRow = showReceptionLink
    ? receptionLocatorOpen
      ? props.pageConfig.density === "compact"
        ? "512px"
        : "528px"
      : props.pageConfig.density === "compact"
        ? "86px"
        : "104px"
    : null;
  const rows = [
    "38px",
    props.pageConfig.modules.deviceBar ? "34px" : null,
    props.pageConfig.modules.minimap ? (props.pageConfig.density === "compact" ? "192px" : "236px") : null,
    receptionLinkRow,
    props.pageConfig.modules.islands ? (props.pageConfig.density === "compact" ? "48px" : "52px") : null,
    props.pageConfig.modules.waveform ? (props.pageConfig.density === "compact" ? "96px" : "112px") : null,
    props.pageConfig.modules.tracks ? (props.pageConfig.density === "compact" ? "260px" : "310px") : null,
    props.pageConfig.modules.transcript ? "minmax(0, 1fr)" : null,
    props.pageConfig.modules.spine ? "42px" : null
  ].filter(Boolean).join(" ");
  const gridColumns = [
    props.pageConfig.showQueue ? "172px" : null,
    "minmax(760px, 1fr)",
    props.pageConfig.showRightPanel ? "336px" : null
  ].filter(Boolean).join(" ");
  const selectDeviceFocus = (deviceKey: ListeningDeviceKey) => {
    const nextDevice = listeningDeviceBadges.find((device) => device.key === deviceKey) ?? listeningDeviceBadges[0];
    setActiveDeviceKey(nextDevice.key);
    props.setPanelTab(nextDevice.panel);
    props.setMarkState(nextDevice.mark);
  };

  return (
    <div className={`evidence-grid annotation-shell density-${props.pageConfig.density}`} style={{ gridTemplateColumns: gridColumns }}>
      {props.pageConfig.showQueue && (
        <aside className="queue-panel annotation-queue">
          <div className="panel-title">
            <span>复核队列</span>
            <SlidersHorizontal size={15} />
          </div>
          {reviewQueueMockData.map((item) => (
            <button
              key={item.label}
              data-testid={`review-queue-${item.label}`}
              className={props.activeQueue === item.label ? "queue-row active" : "queue-row"}
              onClick={() => props.setActiveQueue(item.label)}
            >
              <span>{item.label}</span>
              <strong>{item.count}</strong>
            </button>
          ))}
          <div className="queue-detail">
            <div className="risk-dot" />
            <strong>{props.sample.queueTitle}</strong>
            <span>{props.sample.queueMeta}</span>
            <em>{activeQueueMock.api}</em>
            <p>{props.sample.queueDetail}</p>
          </div>
        </aside>
      )}

      <section className="main-review annotation-main" style={{ gridTemplateRows: rows }}>
        <AnnotationToolbar sample={props.sample} />
        {props.pageConfig.modules.deviceBar && (
          <BadgeBar activeDeviceKey={activeDeviceKey} onSelectDevice={selectDeviceFocus} activeDevice={activeDevice} />
        )}
        {props.pageConfig.modules.minimap && (
          <AnnotationMinimap
            selectedWindow={props.selectedWindow}
            setSelectedWindow={props.setSelectedWindow}
            activeTrack={activeTrack}
            setActiveTrack={setActiveTrack}
            hiddenTracks={hiddenTracks}
            setHiddenTracks={setHiddenTracks}
            openListeningMode={() => {
              props.setListeningScope("conversation");
              props.setMode("simple");
            }}
          />
        )}
        {showReceptionLink && (
          <ReceptionLinkPanel
            sample={props.sample}
            setSelectedWindow={props.setSelectedWindow}
            setPanelTab={props.setPanelTab}
            onLocatorOpenChange={setReceptionLocatorOpen}
          />
        )}
        {props.pageConfig.modules.islands && (
          <IslandBar
            activeIsland={activeIsland}
            setActiveIsland={setActiveIsland}
            selectedWindow={props.selectedWindow}
            setSelectedWindow={props.setSelectedWindow}
          />
        )}
        {showWaveformArea && (
          <WaveformPanel
            audioSessionId={props.sample.sessionId}
            sessionStartedAt={props.sample.sessionStartedAt}
            showWaveform={props.pageConfig.modules.waveform}
            showTracks={props.pageConfig.modules.tracks}
            activeTrack={activeTrack}
            setActiveTrack={setActiveTrack}
            hiddenTracks={hiddenTracks}
            setHiddenTracks={setHiddenTracks}
          />
        )}
        {props.pageConfig.modules.transcript && <AnnotationSplitView sample={props.sample} activeDevice={activeDevice} />}
        {props.pageConfig.modules.spine && (
          <AnnotationSpine
            agentState={props.agentState}
            setAgentState={props.setAgentState}
            sample={props.sample}
            completedCount={props.completedCount}
            onConfirmNext={props.onConfirmNext}
            confirmPending={props.confirmPending}
          />
        )}
      </section>

      {props.pageConfig.showRightPanel && (
        <EvidencePanel
          panelTab={props.panelTab}
          setPanelTab={props.setPanelTab}
          markState={props.markState}
          setMarkState={props.setMarkState}
          agentState={props.agentState}
          setAgentState={props.setAgentState}
          sample={props.sample}
          activeDevice={activeDevice}
          navigateToTarget={props.navigateToTarget}
        />
      )}
    </div>
  );
}
