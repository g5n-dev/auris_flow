import { eventLinks } from "../../../../../shared/fixtures/eventLinks";
import { actionFeedbackAttrs } from "../../../../../shared/runtime/feedbackAttributes";
import { LazyBranchBoundary } from "../../../../../shared/ui/LazyBranchBoundary";
import type { PanelTab } from "../../../types";
import { DataTrack, EvidenceList } from "../trackDisplays";
import { DiffEvidencePanel } from "./DiffEvidencePanel";
import type { EvidencePanelController } from "./hotwordCorrectionActions";
import { AlertTriangle, Check, FileText, Gauge, GitBranch, Link2, Radio, UserCheck, X } from "lucide-react";

export function EvidencePanelView({ controller }: { controller: EvidencePanelController }) {
  const { activeDevice, agentState, markState, panelTab, receptionCandidates, sample, setAgentState, setMarkState, setPanelTab } = controller;
  return (
    (
        <aside className="evidence-panel">
          <div className="panel-tabs">
            {[
              ["agent", "Agent建议"],
              ["docs", "业务单据"],
              ["diff", "字段差异"],
              ["crosstalk", "串音证据"]
            ].map(([key, label]) => (
              <button key={key} className={panelTab === key ? "active" : ""} onClick={() => setPanelTab(key as PanelTab)}>
                {label}
              </button>
            ))}
          </div>
          <div className={`device-panel-focus ${activeDevice.mark === "crosstalk" ? "warn" : ""}`}>
            <div className="device-panel-title">
              <span>当前设备焦点</span>
              <b>{activeDevice.role}</b>
            </div>
            <strong>{activeDevice.name}</strong>
            <p>{activeDevice.summary}</p>
            <div className="device-panel-meta">
              <span>{activeDevice.count}</span>
              <span>{activeDevice.src}</span>
              <span>电量 {activeDevice.battery}</span>
            </div>
          </div>

          {panelTab === "agent" && (
            <div className="panel-body">
              <div className="agent-score">
                <div>
                  <span>建议结论</span>
                  <strong>{sample.conclusion}</strong>
                </div>
                <Gauge size={42} />
                <b>{sample.confidence}%</b>
              </div>
              <p className="agent-reason">
                {sample.reason}
              </p>
              <EvidenceList sample={sample} activeDevice={activeDevice} />
              <div className="action-row">
                <button
                  className={agentState === "accepted" ? "good selected" : "good"}
                  onClick={() => setAgentState("accepted")}
                  title="接受 Agent 建议并在调听回执中显示处理状态。"
                  {...actionFeedbackAttrs("s,e")}
                >
                  <Check size={15} />
                  接受建议
                </button>
                <button
                  className={agentState === "rejected" ? "danger selected" : "danger"}
                  onClick={() => setAgentState("rejected")}
                  title="拒绝 Agent 建议并在调听回执中显示处理状态。"
                  {...actionFeedbackAttrs("s,e")}
                >
                  <X size={15} />
                  拒绝
                </button>
              </div>
              <button className="wide-action">
                <UserCheck size={15} />
                转人工仲裁
              </button>
            </div>
          )}

          {panelTab === "docs" && (
            <div className="panel-body">
              <div className="reception-side-summary">
                <span>当前接待单候选</span>
                {receptionCandidates.map((candidate) => (
                  <button key={candidate.id} type="button">
                    <strong>{candidate.orderNo}</strong>
                    <em>{candidate.customer} / {candidate.employee}</em>
                    <b>{candidate.match}%</b>
                  </button>
                ))}
              </div>
              <div className="event-chain-title">
                <span>单据事件链</span>
                <strong>音频片段 → ASR span → 业务事件 → 单据字段</strong>
              </div>
              <div className="doc-event-chain">
                {eventLinks.map((event) => (
                  <button key={event.id} className={`doc-event-card ${event.tone}`}>
                    <div className="event-card-head">
                      <span>{event.window}</span>
                      <b>{event.state}</b>
                    </div>
                    <strong>{event.type}</strong>
                    <p>{event.asr}</p>
                    <div className="event-hop">
                      <span>音频</span>
                      <em>{event.audio}</em>
                    </div>
                    <div className="event-hop">
                      <span>单据</span>
                      <em>{event.doc}</em>
                    </div>
                    <div className="event-field">
                      <code>{event.field}</code>
                      <small>{event.fieldValue}</small>
                    </div>
                  </button>
                ))}
              </div>
                {sample.docs.map((doc) => (
                <button className={`doc-card ${doc.tone}`} key={doc.id}>
                  <FileText size={17} />
                  <div>
                    <strong>{doc.type}</strong>
                    <span>{doc.id}</span>
                  </div>
                  <b>{doc.status}</b>
                  <small>{doc.match}</small>
                </button>
              ))}
            </div>
          )}

          {panelTab === "diff" && (
            <LazyBranchBoundary label="字段差异证据" minHeight={360} resetKey={panelTab} testId="listening-diff-panel">
              <DiffEvidencePanel controller={controller} />
            </LazyBranchBoundary>
          )}

          {panelTab === "crosstalk" && (
            <div className="panel-body crosstalk-body">
              <div className="crosstalk-card">
                <AlertTriangle size={18} />
                <div>
                  <strong>{sample.crosstalk.title}</strong>
                  <small>需要先判定主录音，再决定报价标签是否可写入资产。</small>
                </div>
                <span>{sample.crosstalk.detail}</span>
              </div>
              <div className="crosstalk-decision-grid">
                <button type="button" className={markState === "main" ? "selected" : ""} onClick={() => setMarkState("main")}>
                  <span>主源保留</span>
                  <strong>{sample.crosstalk.primary}</strong>
                  <em>报价链路完整，优先保留为证据主干</em>
                </button>
                <button type="button" className={markState === "crosstalk" ? "selected warn" : "warn"} onClick={() => setMarkState("crosstalk")}>
                  <span>候选串入</span>
                  <strong>{sample.crosstalk.candidate}</strong>
                  <em>同窗峰值同步，暂不写入主录音</em>
                </button>
              </div>
              <div className="crosstalk-metrics">
                {[
                  ["ASR 相似度", "0.87", "高"],
                  ["峰值同步", "3 峰", "同窗"],
                  ["空间关系", "相邻", "同门店"],
                  ["处置建议", "隔离", "待确认"]
                ].map(([label, value, state]) => (
                  <div key={label}>
                    <span>{label}</span>
                    <strong>{value}</strong>
                    <em>{state}</em>
                  </div>
                ))}
              </div>
              <div className="mini-compare crosstalk-compare">
                <div>
                  <span>{sample.crosstalk.primary}</span>
                  <DataTrack tone="green" chunks={[12, 8, 18, 6, 14, 9, 11]} />
                </div>
                <div>
                  <span>{sample.crosstalk.candidate}</span>
                  <DataTrack tone="amber" chunks={[8, 6, 16, 5, 12, 8, 9]} />
                </div>
              </div>
              <div className="crosstalk-next-step">
                <b>下一步</b>
                <span>{markState === "main" ? "将当前片段保留为主录音，串音候选只作为旁路证据。" : markState === "crosstalk" ? "把当前设备标记为串音候选，并阻断其报价标签写入。" : "请选择主录音或串音候选，底部动作会写入审查记录。"}</span>
              </div>
              <div className="crosstalk-audit-list">
                <span>证据引用</span>
                {[
                  ["12:28:01", "能量峰", "A/B 工牌与 Hall-Mic 出现同窗峰值"],
                  ["12:28:12", "ASR 片段", "候选源重复出现优惠上限话术"],
                  ["voice_segments", "资产", "串音标记会阻断该源写入报价标签"]
                ].map(([time, type, text]) => (
                  <button key={`${time}-${type}`} type="button">
                    <b>{time}</b>
                    <em>{type}</em>
                    <strong>{text}</strong>
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="mark-actions">
            <button className={markState === "main" ? "selected" : ""} onClick={() => setMarkState("main")}>
              <Radio size={15} />
              标记主录音
            </button>
            <button className={markState === "crosstalk" ? "selected" : ""} onClick={() => setMarkState("crosstalk")}>
              <GitBranch size={15} />
              标记串音
            </button>
            <button className={markState === "duplicate" ? "selected" : ""} onClick={() => setMarkState("duplicate")}>
              <Link2 size={15} />
              标记重复收录
            </button>
          </div>
        </aside>
      )
  );
}
