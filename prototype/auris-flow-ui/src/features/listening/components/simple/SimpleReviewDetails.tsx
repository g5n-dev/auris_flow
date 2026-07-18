import type { SimpleLabelHit, SpeakerChannel } from "../../../../shared/contracts/simpleConversation";
import { simpleTurns } from "../../../../shared/fixtures/listeningSamples";
import type { SimpleLabelDetail, SimpleLabelReviewState, SimpleSpeakerAnnotation, SimpleSpeakerRole } from "../../fixtures/simpleReviewFixtures";
import { simpleLabelDetails, simpleSpeakerRoles } from "../../fixtures/simpleReviewFixtures";
import type { Mode } from "../../types";
import { AlertTriangle, BrainCircuit, Check, ShieldCheck, Sparkles, Tags, Volume2, X } from "lucide-react";

export function SimpleTranscriptTurn({
  turn,
  tags,
  speaker,
  active,
  entityLayer,
  judgeLayer,
  showInlineAnnotations,
  onSelect,
  onTagSelect
}: {
  turn: (typeof simpleTurns)[number];
  tags: string[];
  speaker: SimpleSpeakerAnnotation;
  active: boolean;
  entityLayer: boolean;
  judgeLayer: boolean;
  showInlineAnnotations: boolean;
  onSelect: () => void;
  onTagSelect: (label: string) => void;
}) {
  const side = speaker.role === "customer" ? "right" : "left";
  const diarConfidence = Math.round((turn.diar?.confidence ?? speaker.confidence) * 100);
  return (
    <button className={active ? `simple-turn active ${speaker.role} ${side}` : `simple-turn ${speaker.role} ${side}`} onClick={onSelect}>
      <div className="simple-turn-time">
        {turn.time.slice(3)}
        <span>{turn.dur}</span>
      </div>
      <div className={`simple-chat-avatar ${speaker.role}`}>{speaker.short}</div>
      <div className="simple-bubble">
        <div className="simple-bubble-head">
          <span className={`simple-speaker ${speaker.role}`}>{speaker.speaker}</span>
          <span className={`simple-channel-pill ${speaker.channel.toLowerCase()}`}>
            {speaker.channel === "LR" ? "L/R" : speaker.channel}
          </span>
          <span className="simple-diar-pill">{turn.diar.cluster} · AI {diarConfidence}%</span>
          <strong>{turn.intent}</strong>
          <em>{turn.mood}</em>
          <b>{speaker.source}</b>
        </div>
        <p>
          {turn.parts.map(([text, kind], index) => {
            const highlighted =
              (entityLayer && ["entity", "money", "time", "event"].includes(kind)) || (judgeLayer && kind === "judge");
            return highlighted ? (
              <mark key={`${text}-${index}`} className={kind}>
                {text}
              </mark>
            ) : (
              <span key={`${text}-${index}`}>{text}</span>
            );
          })}
        </p>
        {showInlineAnnotations && (
          <div className="simple-turn-tags">
            <div className="simple-tag-row business">
              <b>业务标签</b>
              {tags.map((tag) => (
                <span
                  key={tag}
                  onClick={(event) => {
                    event.stopPropagation();
                    onTagSelect(tag === "价格异议" ? "价格异议" : tag === "低置信复核" ? "串音疑似" : tag === "试驾时间" ? "试驾时间" : tag);
                  }}
                >
                  {tag}
                </span>
              ))}
            </div>
            <div className="simple-tag-row acoustic">
              <b>声学标签</b>
              {turn.acoustic.map((item) => (
                <span className={`simple-acoustic-tag ${item.tone}`} key={`${turn.eventId}-${item.label}`}>
                  <Volume2 size={10} />
                  {item.label}
                  <em>{Math.round(item.confidence * 100)}%</em>
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </button>
  );
}

export function SimpleSpeakerDock({
  activeTurn,
  activeSpeaker,
  speakerRoles,
  setSpeakerRole,
  setSpeakerChannel,
  saveSpeaker,
  speakerEditState
}: {
  activeTurn: (typeof simpleTurns)[number];
  activeSpeaker: SimpleSpeakerAnnotation;
  speakerRoles: typeof simpleSpeakerRoles;
  setSpeakerRole: (role: SimpleSpeakerRole["key"]) => void;
  setSpeakerChannel: (channel: SpeakerChannel) => void;
  saveSpeaker: () => void;
  speakerEditState: "未保存" | "已保存";
}) {
  return (
    <aside className="simple-speaker-dock">
      <div className="simple-speaker-dock-head">
        <span>说话人标注</span>
        <strong>{activeTurn.time.slice(3)} · {activeTurn.dur}</strong>
        <b className={speakerEditState === "已保存" ? "saved" : ""}>{speakerEditState}</b>
      </div>

      <div className="simple-diar-card">
        <div>
          <Sparkles size={13} />
          <span>智能建议</span>
        </div>
        <strong>{activeTurn.diar.cluster} · {Math.round(activeTurn.diar.confidence * 100)}%</strong>
        <p>{activeTurn.diar.ai}</p>
      </div>

      <div className="simple-speaker-dock-section">
        <span>选择左侧 SPK 角色</span>
        <div className="simple-speaker-role-grid">
          {speakerRoles.map((role) => (
            <button
              key={role.key}
              className={activeSpeaker.key === role.key ? `active ${role.role}` : role.role}
              onClick={() => setSpeakerRole(role.key)}
            >
              <b>{role.short}</b>
              <span>{role.label}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="simple-speaker-dock-section">
        <span>声道归属</span>
        <div className="simple-channel-grid">
          {[
            ["L", "左声道"],
            ["R", "右声道"],
            ["LR", "双声道"]
          ].map(([key, label]) => (
            <button
              key={key}
              className={activeSpeaker.channel === key ? "active" : ""}
              onClick={() => setSpeakerChannel(key as SpeakerChannel)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="simple-speaker-dock-section">
        <span>音乐 / 噪音 / 串音</span>
        <div className="simple-acoustic-list">
          {activeTurn.acoustic.map((item) => (
            <div className={`simple-acoustic-row ${item.tone}`} key={`${activeTurn.eventId}-dock-${item.label}`}>
              <span>{item.label}</span>
              <b>{Math.round(item.confidence * 100)}%</b>
              <i style={{ width: `${Math.round(item.confidence * 100)}%` }} />
            </div>
          ))}
        </div>
      </div>

      <button className="simple-speaker-save" onClick={saveSpeaker}>
        <Check size={13} />
        保存说话人
      </button>
    </aside>
  );
}

export function SimpleDetailPane({
  detail,
  selectedLabel,
  activeTurn,
  activeSpeaker,
  speakerRoles,
  setSpeakerRole,
  setSpeakerChannel,
  saveSpeaker,
  speakerEditState,
  activeTags,
  draftTag,
  setDraftTag,
  addDraftTag,
  addTag,
  removeTag,
  applyAgentTags,
  saveTags,
  tagEditState,
  suggestions,
  labelHits,
  activeHitIndex,
  labelReviewStates,
  onSelectHit,
  onAcceptSelectedLabel,
  onAcceptAllLabelHits,
  onMarkSelectedReview,
  setMode,
  setSelectedLabel
}: {
  detail: SimpleLabelDetail;
  selectedLabel: string;
  activeTurn: (typeof simpleTurns)[number];
  activeSpeaker: SimpleSpeakerAnnotation;
  speakerRoles: typeof simpleSpeakerRoles;
  setSpeakerRole: (role: SimpleSpeakerRole["key"]) => void;
  setSpeakerChannel: (channel: SpeakerChannel) => void;
  saveSpeaker: () => void;
  speakerEditState: "未保存" | "已保存";
  activeTags: string[];
  draftTag: string;
  setDraftTag: (value: string) => void;
  addDraftTag: () => void;
  addTag: (tag: string) => void;
  removeTag: (tag: string) => void;
  applyAgentTags: () => void;
  saveTags: () => void;
  tagEditState: "未保存" | "已保存";
  suggestions: string[];
  labelHits: SimpleLabelHit[];
  activeHitIndex: number;
  labelReviewStates: Record<string, SimpleLabelReviewState>;
  onSelectHit: (turnIndex: number) => void;
  onAcceptSelectedLabel: () => void;
  onAcceptAllLabelHits: () => void;
  onMarkSelectedReview: () => void;
  setMode: (mode: Mode) => void;
  setSelectedLabel: (value: string) => void;
}) {
  const activeHit = activeHitIndex >= 0 ? labelHits[activeHitIndex] : labelHits[0];
  const reviewStateForHit = (hit: SimpleLabelHit | undefined) =>
    hit ? labelReviewStates[`${selectedLabel}::${simpleTurns[hit.turnIndex].eventId}`] ?? "pending" : "pending";
  const activeReviewState = reviewStateForHit(activeHit);
  const acceptedCount = labelHits.filter((hit) => reviewStateForHit(hit) === "accepted").length;
  const reviewCount = labelHits.filter((hit) => reviewStateForHit(hit) === "review").length;
  return (
    <aside className="simple-detail-pane">
      <div className="simple-detail-top">
        <div className={`simple-fam-pill ${detail.family.toLowerCase()}`}>{detail.family}</div>
        <span>{detail.layer}</span>
        <h2>{selectedLabel}</h2>
        <p>{detail.description}</p>
      </div>

      <div className="simple-meta-grid">
        <div>
          <span>状态</span>
          <strong>{detail.status}</strong>
        </div>
        <div>
          <span>置信</span>
          <strong>{detail.confidence}</strong>
        </div>
        <div>
          <span>负责人</span>
          <strong>{detail.owner}</strong>
        </div>
        <div>
          <span>当前意图</span>
          <strong>{activeTurn.intentScore.toFixed(2)}</strong>
        </div>
      </div>

      <div className="simple-detail-scroll">
        <section className="simple-detail-section simple-label-review-console">
          <div className="simple-review-head">
            <div>
              <span>筛选命中操作台</span>
              <strong>{selectedLabel} · {labelHits.length} 个命中</strong>
            </div>
            <b>{acceptedCount || reviewCount ? `已处理 ${acceptedCount + reviewCount}/${labelHits.length}` : activeHitIndex >= 0 ? `第 ${activeHitIndex + 1}/${labelHits.length}` : "未定位"}</b>
          </div>
          {activeHit ? (
            <>
              <div className={`simple-review-current ${activeReviewState}`}>
                <span>{simpleTurns[activeHit.turnIndex].time} · {simpleTurns[activeHit.turnIndex].speaker}</span>
                <strong>{activeHit.evidence}</strong>
                <p>{activeHit.reason}</p>
                <em>{activeHit.relation}</em>
                <b>{activeReviewState === "accepted" ? "已接受" : activeReviewState === "review" ? "待复核" : `${Math.round(activeHit.confidence * 100)}%`}</b>
              </div>
              <div className="simple-review-hit-list">
                {labelHits.map((hit, index) => {
                  const turn = simpleTurns[hit.turnIndex];
                  const state = reviewStateForHit(hit);
                  return (
                    <button
                      key={`${selectedLabel}-${turn.eventId}`}
                      className={[index === activeHitIndex ? "active" : "", state !== "pending" ? state : ""].join(" ")}
                      onClick={() => onSelectHit(hit.turnIndex)}
                    >
                      <span>{turn.time.slice(3)}</span>
                      <strong>{hit.evidence}</strong>
                      <em>{state === "accepted" ? "已写入当前片段标签" : state === "review" ? "已加入人工复核队列" : hit.action}</em>
                    </button>
                  );
                })}
              </div>
              <div className="simple-review-actions">
                <button className={activeReviewState === "accepted" ? "is-done" : ""} onClick={onAcceptSelectedLabel}>
                  <Check size={13} />
                  {activeReviewState === "accepted" ? "已接受" : "接受当前"}
                </button>
                <button className={activeReviewState === "review" ? "is-review" : ""} onClick={onMarkSelectedReview}>
                  <AlertTriangle size={13} />
                  {activeReviewState === "review" ? "待复核" : "转人工复核"}
                </button>
                <button className={acceptedCount === labelHits.length ? "is-done" : ""} onClick={onAcceptAllLabelHits}>
                  <Tags size={13} />
                  {acceptedCount === labelHits.length ? "已批量接受" : "批量接受"}
                </button>
              </div>
            </>
          ) : (
            <div className="simple-review-empty">
              <strong>当前会话未命中</strong>
              <span>可把该标签加入推荐池，等待 Agent 重新抽取。</span>
            </div>
          )}
        </section>

        <section className="simple-detail-section simple-speaker-editor">
          <div className="simple-section-title">当前片段说话人标注</div>
          <div className="speaker-edit-status">
            <span>{activeTurn.time} · {activeTurn.dur}</span>
            <b className={speakerEditState === "已保存" ? "saved" : ""}>{speakerEditState}</b>
          </div>
          <div className="speaker-active-card">
            <span className={`simple-speaker ${activeSpeaker.role}`}>{activeSpeaker.speaker}</span>
            <strong>{activeSpeaker.channel === "LR" ? "双声道" : `${activeSpeaker.channel} 声道`}</strong>
            <em>{activeSpeaker.source}</em>
          </div>
          <div className="speaker-role-select">
            {speakerRoles.map((role) => (
              <button
                key={role.key}
                className={activeSpeaker.key === role.key ? `active ${role.role}` : role.role}
                onClick={() => setSpeakerRole(role.key)}
              >
                <span>{role.short}</span>
                {role.label}
              </button>
            ))}
          </div>
          <div className="speaker-channel-row detail">
            {[
              ["L", "左声道"],
              ["R", "右声道"],
              ["LR", "双声道"]
            ].map(([key, label]) => (
              <button
                key={key}
                className={activeSpeaker.channel === key ? "active" : ""}
                onClick={() => setSpeakerChannel(key as SpeakerChannel)}
              >
                {label}
              </button>
            ))}
            <button className="save" onClick={saveSpeaker}>
              <Check size={13} />
              保存说话人
            </button>
          </div>
        </section>

        <section className="simple-detail-section simple-tag-editor">
          <div className="simple-section-title">当前片段标签编辑</div>
          <div className="tag-edit-status">
            <span>{activeTurn.eventId}</span>
            <b className={tagEditState === "已保存" ? "saved" : ""}>{tagEditState}</b>
          </div>
          <div className="editable-tags">
            {activeTags.map((tag) => (
              <button
                key={tag}
                onClick={() => {
                  removeTag(tag);
                }}
              >
                {tag}
                <X size={10} />
              </button>
            ))}
          </div>
          <div className="tag-input-row">
            <input
              value={draftTag}
              onChange={(event) => setDraftTag(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") addDraftTag();
              }}
              placeholder="输入新标签"
            />
            <button onClick={addDraftTag}>添加</button>
          </div>
          <div className="tag-suggestion-row">
            {suggestions.map((tag) => (
              <button key={tag} onClick={() => addTag(tag)}>
                {tag}
              </button>
            ))}
          </div>
          <div className="tag-editor-actions">
            <button onClick={applyAgentTags}>
              <Sparkles size={13} />
              套用建议
            </button>
            <button onClick={saveTags}>
              <Check size={13} />
              保存标签
            </button>
          </div>
        </section>

        <section className="simple-detail-section">
          <div className="simple-section-title">当前片段证据</div>
          <div className="simple-evidence-card">
            <span>{activeTurn.time}</span>
            <strong>{activeTurn.intent}</strong>
            <p>{activeTurn.source}</p>
            <b>{activeTurn.doc}</b>
          </div>
        </section>

        <section className="simple-detail-section">
          <div className="simple-section-title">结构化字段</div>
          <div className="simple-field-table">
            {detail.fields.map(([key, value]) => (
              <div key={key}>
                <span>{key}</span>
                <strong>{value}</strong>
              </div>
            ))}
          </div>
        </section>

        <section className="simple-detail-section">
          <div className="simple-section-title">命中样例</div>
          <div className="simple-example-list">
            {detail.examples.map((example) => (
              <span key={example}>{example}</span>
            ))}
          </div>
        </section>

        <section className="simple-detail-section">
          <div className="simple-section-title">依赖与下游</div>
          <div className="simple-chip-group">
            {detail.dependencies.map((item) => (
              <button key={item} onClick={() => setSelectedLabel(simpleLabelDetails[item] ? item : selectedLabel)}>
                {item}
              </button>
            ))}
          </div>
          <div className="simple-downstream">
            {detail.downstream.map((item) => (
              <span key={item}>{item}</span>
            ))}
          </div>
        </section>
      </div>

      <div className="simple-next-action">
        <BrainCircuit size={17} />
        <div>
          <strong>{activeTurn.nextAction}</strong>
          <span>{detail.action}</span>
        </div>
      </div>

      <div className="simple-detail-actions">
        <button>
          <Check size={15} />
          接受标签
        </button>
        <button>
          <AlertTriangle size={15} />
          标记复核
        </button>
        <button onClick={() => setMode("evidence")}>
          <ShieldCheck size={15} />
          证据审查
        </button>
      </div>
    </aside>
  );
}
