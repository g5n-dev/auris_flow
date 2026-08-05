import type { SimpleLabelFilter, SpeakerChannel } from "../../../../shared/contracts/simpleConversation";
import { buildSimpleLabelHits, simpleLabelDomains, simpleTurns } from "../../../../shared/fixtures/listeningSamples";
import type { ReviewSample } from "../../fixtures/reviewSamples";
import type { SimpleLabelReviewState, SimpleSpeakerAnnotation, SimpleSpeakerRole } from "../../fixtures/simpleReviewFixtures";
import { getSimpleLabelDetail, inferSimpleSpeaker, simpleAiRecommendedLabels, simpleLabelDetails, simpleLabelTabs, simpleSpeakerRoles, tagSuggestions } from "../../fixtures/simpleReviewFixtures";
import type { ListeningScope, Mode, SimpleAiRecommendState } from "../../types";
import { useGrantedAudioPlayback } from "../../hooks/useGrantedAudioPlayback";
import { LABEL_DEMO_MODE } from "../../../../shared/runtime/demoMode";
import { SlidingChipRail } from "../evidence/annotationControls";
import { SimpleAudioPlayer } from "./SimpleAudioPlayer";
import { SimpleDetailPane, SimpleSpeakerDock, SimpleTranscriptTurn } from "./SimpleReviewDetails";
import { Layers, Pause, Play, Sparkles } from "lucide-react";
import { useMemo, useState } from "react";

type SimpleModeProps = {
  setMode: (mode: Mode) => void;
  activeChip: string;
  setActiveChip: (value: string) => void;
  listeningScope: ListeningScope;
  setListeningScope: (scope: ListeningScope) => void;
  sample: ReviewSample;
};

function AuthoritativeSessionPlayback({
  sample,
  setMode
}: Pick<SimpleModeProps, "sample" | "setMode">) {
  const [playing, setPlaying] = useState(false);
  const {
    audioRef,
    playbackMessage,
    playbackPending,
    playbackStatus,
    togglePlayback,
    handlePlaybackError
  } = useGrantedAudioPlayback(sample.sessionId, setPlaying);
  const disabledReason =
    "生产模式未接入服务端 ASR/说话人/标签推荐编辑契约；本地 SimpleMode fixture 不会写入真实 AudioSession。";
  return (
    <div className="simple-page" data-testid="listening-authoritative-playback">
      <section className="module-panel wide tenant-empty-state">
        <strong>音频会话 {sample.sessionId}</strong>
        <span>{sample.file} · {sample.window}</span>
        <audio
          ref={audioRef}
          preload="none"
          data-testid="listening-recording"
          data-audio-session-id={sample.sessionId}
          aria-label={`${sample.sessionId} 录音`}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
          onError={handlePlaybackError}
        />
        <button
          type="button"
          className="primary"
          disabled={playbackPending}
          aria-busy={playbackPending}
          onClick={() => void togglePlayback()}
        >
          {playing ? <Pause size={14} /> : <Play size={14} />}
          {playbackPending ? "正在获取播放授权" : playing ? "暂停" : "播放真实音频"}
        </button>
        {playbackStatus !== "idle" && (
          <span role="status">{playbackMessage}</span>
        )}
        <span>{disabledReason}</span>
        <button type="button" disabled title={disabledReason}>
          AI 推荐与本地保存已禁用
        </button>
        <button type="button" onClick={() => setMode("evidence")}>返回证据审查</button>
      </section>
    </div>
  );
}

function DemoSimpleMode({
  setMode,
  activeChip,
  setActiveChip,
  listeningScope,
  setListeningScope,
  sample
}: SimpleModeProps) {
  const [activeTurnIndex, setActiveTurnIndex] = useState(1);
  const [selectedLabel, setSelectedLabel] = useState(sample.selectedLabel);
  const [labelFilter, setLabelFilter] = useState<SimpleLabelFilter>("全部");
  const [aiRecommendState, setAiRecommendState] = useState<SimpleAiRecommendState>("idle");
  const [entityLayer, setEntityLayer] = useState(true);
  const [judgeLayer, setJudgeLayer] = useState(true);
  const [playing, setPlaying] = useState(false);
  const [turnTagEdits, setTurnTagEdits] = useState<Record<string, string[]>>({});
  const [speakerEdits, setSpeakerEdits] = useState<Record<string, SimpleSpeakerAnnotation>>({});
  const [draftTag, setDraftTag] = useState("");
  const [tagEditState, setTagEditState] = useState<"未保存" | "已保存">("未保存");
  const [speakerEditState, setSpeakerEditState] = useState<"未保存" | "已保存">("已保存");
  const [labelReviewStates, setLabelReviewStates] = useState<Record<string, SimpleLabelReviewState>>({});
  const activeTurn = simpleTurns[activeTurnIndex];
  const playProgress = 17 + activeTurnIndex * 16;
  const selectedDetail = getSimpleLabelDetail(selectedLabel);
  const activeTags = turnTagEdits[activeTurn.eventId] ?? activeTurn.tags;
  const activeSpeaker = speakerEdits[activeTurn.eventId] ?? inferSimpleSpeaker(activeTurn);
  const activeSuggestions = tagSuggestions.filter((tag) => !activeTags.includes(tag)).slice(0, 6);
  const labelHits = useMemo(() => buildSimpleLabelHits(selectedLabel, turnTagEdits), [selectedLabel, turnTagEdits]);
  const activeLabelHitIndex = labelHits.findIndex((hit) => hit.turnIndex === activeTurnIndex);
  const showSimpleInlineAnnotations = false;
  const visibleLabelDomains = useMemo(
    () => simpleLabelDomains.filter((domain) => labelFilter === "全部" || domain.filter === labelFilter),
    [labelFilter]
  );

  const labelReviewKey = (label: string, turnIndex: number) => `${label}::${simpleTurns[turnIndex].eventId}`;
  const setLabelHitState = (turnIndex: number, state: SimpleLabelReviewState) => {
    setLabelReviewStates((current) => ({
      ...current,
      [labelReviewKey(selectedLabel, turnIndex)]: state
    }));
  };

  const syncTurnEditState = (turnIndex: number) => {
    const turn = simpleTurns[turnIndex];
    setTagEditState(turnTagEdits[turn.eventId] ? "未保存" : "已保存");
    setSpeakerEditState(speakerEdits[turn.eventId] ? "未保存" : "已保存");
  };

  const inferLabelForTurn = (turnIndex: number) => {
    const turn = simpleTurns[turnIndex];
    const turnTags = turnTagEdits[turn.eventId] ?? turn.tags;
    if (turnTags.includes("客户价格异议") || turnTags.includes("价格异议")) return "价格异议";
    if (turnTags.includes("试驾时间")) return "试驾时间";
    if (turnTags.includes("串音疑似") || turnTags.includes("低置信复核")) return "串音疑似";
    if (turnTags.includes("车型")) return "车型";
    return "报价金额";
  };

  const selectTurn = (turnIndex: number, labelOverride?: string) => {
    setActiveTurnIndex(turnIndex);
    syncTurnEditState(turnIndex);
    setSelectedLabel(labelOverride ?? inferLabelForTurn(turnIndex));
  };

  const selectLabelFromLibrary = (label: string) => {
    const hits = buildSimpleLabelHits(label, turnTagEdits);
    setSelectedLabel(label);
    if (hits.length > 0) {
      setActiveTurnIndex(hits[0].turnIndex);
      syncTurnEditState(hits[0].turnIndex);
    }
  };

  const changeLabelFilter = (nextFilter: SimpleLabelFilter) => {
    setLabelFilter(nextFilter);
    const nextDomains = simpleLabelDomains.filter((domain) => nextFilter === "全部" || domain.filter === nextFilter);
    const selectedStillVisible = nextDomains.some((domain) => domain.items.some((item) => item.name === selectedLabel));
    const firstVisibleLabel = nextDomains[0]?.items[0]?.name;
    if (!selectedStillVisible && firstVisibleLabel) selectLabelFromLibrary(firstVisibleLabel);
  };

  const runAiLabelRecommend = () => {
    setAiRecommendState("ready");
    setLabelFilter("推荐");
    selectLabelFromLibrary(simpleAiRecommendedLabels[0]);
  };

  const updateActiveSpeaker = (patch: Partial<SimpleSpeakerAnnotation>) => {
    const nextSpeaker = {
      ...activeSpeaker,
      ...patch
    };
    setSpeakerEdits((current) => ({
      ...current,
      [activeTurn.eventId]: nextSpeaker
    }));
    setSpeakerEditState("未保存");
  };

  const setActiveSpeakerRole = (roleKey: SimpleSpeakerRole["key"]) => {
    const role = simpleSpeakerRoles.find((item) => item.key === roleKey) ?? simpleSpeakerRoles[0];
    updateActiveSpeaker({
      key: role.key,
      speaker: role.speaker,
      role: role.role,
      short: role.short,
      channel: role.defaultChannel as SpeakerChannel,
      source: role.source,
      confidence: role.key === "unknown" ? 0.42 : role.key === "crosstalk" ? 0.64 : Math.max(activeSpeaker.confidence, 0.86)
    });
  };

  const setActiveSpeakerChannel = (channel: SpeakerChannel) => {
    updateActiveSpeaker({ channel });
  };

  const updateActiveTags = (nextTags: string[]) => {
    const normalized = Array.from(new Set(nextTags.map((tag) => tag.trim()).filter(Boolean)));
    setTurnTagEdits((current) => ({
      ...current,
      [activeTurn.eventId]: normalized
    }));
    setTagEditState("未保存");
  };

  const addActiveTag = (tag: string) => {
    updateActiveTags([...activeTags, tag]);
    setSelectedLabel(simpleLabelDetails[tag] ? tag : selectedLabel);
  };

  const addDraftTag = () => {
    if (!draftTag.trim()) return;
    addActiveTag(draftTag);
    setDraftTag("");
  };

  const removeActiveTag = (tag: string) => {
    updateActiveTags(activeTags.filter((item) => item !== tag));
    if (selectedLabel === tag) setSelectedLabel("报价金额");
  };

  const applyAgentTags = () => {
    const intentTags =
      activeTurn.intent.includes("异议")
        ? ["客户价格异议", "价格异议", "待回访"]
        : activeTurn.intent.includes("试驾")
          ? ["试驾时间", "成交意向", "单据待核"]
          : ["报价金额", "优惠幅度", "单据待核"];
    updateActiveTags([...activeTags, ...intentTags]);
  };

  const acceptSelectedLabel = () => {
    if (!activeTags.includes(selectedLabel)) {
      updateActiveTags([...activeTags, selectedLabel]);
    } else {
      setTagEditState("已保存");
    }
    setLabelHitState(activeTurnIndex, "accepted");
  };

  const acceptAllLabelHits = () => {
    if (!labelHits.length) return;
    setTurnTagEdits((current) => {
      const next = { ...current };
      labelHits.forEach((hit) => {
        const turn = simpleTurns[hit.turnIndex];
        const tags = next[turn.eventId] ?? turn.tags;
        next[turn.eventId] = Array.from(new Set([...tags, selectedLabel]));
      });
      return next;
    });
    setLabelReviewStates((current) => {
      const next = { ...current };
      labelHits.forEach((hit) => {
        next[labelReviewKey(selectedLabel, hit.turnIndex)] = "accepted";
      });
      return next;
    });
    setTagEditState("未保存");
  };

  const markSelectedLabelReview = () => {
    updateActiveTags([...activeTags, `${selectedLabel}复核`]);
    setLabelHitState(activeTurnIndex, "review");
  };

  return (
    <div className="simple-page" data-testid="listening-simple-demo" data-source="demo">
      <section className="simple-listen">
        <aside className="simple-label-lib">
          <div className="simple-lib-head">
            <div>
              <span>标签库</span>
              <strong>DEMO · 42 命中 / 132 标签</strong>
            </div>
            <button
              className={`simple-ai-button ${aiRecommendState === "idle" ? "" : `is-${aiRecommendState}`}`}
              onClick={runAiLabelRecommend}
              aria-pressed={aiRecommendState === "ready"}
              aria-busy={aiRecommendState === "running"}
              title="DEMO：仅在本地样例中预览推荐标签"
            >
              <Sparkles size={14} />
              <span>AI</span>
            </button>
          </div>
          <div className="simple-lib-tabs">
            {simpleLabelTabs.map((item) => (
              <button key={item} className={labelFilter === item ? "active" : ""} onClick={() => changeLabelFilter(item)}>
                {item}
              </button>
            ))}
          </div>
          <div className="simple-lib-tree">
            {aiRecommendState !== "idle" && (
              <div className={`simple-ai-insight ${aiRecommendState}`}>
                <Sparkles size={13} />
                <div>
                  <strong>{aiRecommendState === "running" ? "正在重算推荐标签" : "DEMO：AI 推荐预览"}</strong>
                  <span>
                    {aiRecommendState === "running"
                      ? "基于当前会话、命中证据和右侧复核状态匹配标签。"
                      : simpleAiRecommendedLabels.join(" / ")}
                  </span>
                </div>
              </div>
            )}
            {visibleLabelDomains.map((domain) => (
              <div className="simple-domain" key={domain.name}>
                <div className={`simple-domain-head ${domain.family}`}>
                  <Layers size={14} />
                  <span>{domain.name}</span>
                  <b>{domain.count}</b>
                </div>
                {domain.items.map((item) => (
                  <button
                    key={`${domain.name}-${item.name}`}
                    className={[
                      "simple-label-item",
                      domain.family,
                      item.status,
                      aiRecommendState === "ready" && simpleAiRecommendedLabels.includes(item.name) ? "ai-recommended" : "",
                      selectedLabel === item.name ? "active" : ""
                    ].join(" ")}
                    onClick={() => selectLabelFromLibrary(item.name)}
                  >
                    <span className="label-status-dot" />
                    <span>{item.name}</span>
                    <b>{item.count}</b>
                  </button>
                ))}
              </div>
            ))}
          </div>
        </aside>

        <section
          className={`simple-transcript-pane${showSimpleInlineAnnotations ? " has-inline-annotations" : ""}`}
        >
          <div className="simple-main-head">
            <div>
              <span>{listeningScope === "conversation" ? "完整对话 · 智能转录" : "当前片段 · 智能转录"}</span>
              <strong>{sample.simpleTitle}</strong>
            </div>
            <div className="simple-session-meta">
              {sample.simpleMeta.map((item) => (
                <span key={item}>{item}</span>
              ))}
            </div>
            <button
              onClick={() => {
                setListeningScope("segment");
                setMode("evidence");
              }}
            >
              回当前片段
            </button>
          </div>

          <SimpleAudioPlayer
            playing={playing}
            setPlaying={setPlaying}
            progress={playProgress}
            activeTurnIndex={activeTurnIndex}
            setActiveTurnIndex={setActiveTurnIndex}
            sample={sample}
          />

          {showSimpleInlineAnnotations && (
            <div className="simple-layer-bar">
              <span>显示层</span>
              <button className={entityLayer ? "on entity" : ""} onClick={() => setEntityLayer(!entityLayer)}>
                实体高亮
              </button>
              <button className={judgeLayer ? "on judge" : ""} onClick={() => setJudgeLayer(!judgeLayer)}>
                判定标签
              </button>
              <strong>{activeTurn.intent}</strong>
              <em>意图置信 {activeTurn.intentScore.toFixed(2)}</em>
            </div>
          )}

          <SlidingChipRail activeChip={activeChip} setActiveChip={setActiveChip} />

          <div className="simple-conversation-workspace">
            <SimpleSpeakerDock
              activeTurn={activeTurn}
              activeSpeaker={activeSpeaker}
              speakerRoles={simpleSpeakerRoles}
              setSpeakerRole={setActiveSpeakerRole}
              setSpeakerChannel={setActiveSpeakerChannel}
              saveSpeaker={() => setSpeakerEditState("已保存")}
              speakerEditState={speakerEditState}
            />
            <div className="simple-conv-scroll simple-chat-scroll">
              {simpleTurns.map((turn, index) => (
                (() => {
                  const turnTags = turnTagEdits[turn.eventId] ?? turn.tags;
                  return (
                    <SimpleTranscriptTurn
                      key={turn.eventId}
                      turn={turn}
                      tags={turnTags}
                      speaker={speakerEdits[turn.eventId] ?? inferSimpleSpeaker(turn)}
                      active={index === activeTurnIndex}
                      entityLayer={entityLayer}
                      judgeLayer={judgeLayer}
                      showInlineAnnotations={showSimpleInlineAnnotations}
                      onSelect={() => selectTurn(index)}
                      onTagSelect={setSelectedLabel}
                    />
                  );
                })()
              ))}
            </div>
          </div>
        </section>

        <SimpleDetailPane
          detail={selectedDetail}
          selectedLabel={selectedLabel}
          activeTurn={activeTurn}
          activeSpeaker={activeSpeaker}
          speakerRoles={simpleSpeakerRoles}
          setSpeakerRole={setActiveSpeakerRole}
          setSpeakerChannel={setActiveSpeakerChannel}
          saveSpeaker={() => setSpeakerEditState("已保存")}
          speakerEditState={speakerEditState}
          activeTags={activeTags}
          draftTag={draftTag}
          setDraftTag={setDraftTag}
          addDraftTag={addDraftTag}
          addTag={addActiveTag}
          removeTag={removeActiveTag}
          applyAgentTags={applyAgentTags}
          saveTags={() => setTagEditState("已保存")}
          tagEditState={tagEditState}
          suggestions={activeSuggestions}
          labelHits={labelHits}
          activeHitIndex={activeLabelHitIndex}
          labelReviewStates={labelReviewStates}
          onSelectHit={(turnIndex) => selectTurn(turnIndex, selectedLabel)}
          onAcceptSelectedLabel={acceptSelectedLabel}
          onAcceptAllLabelHits={acceptAllLabelHits}
          onMarkSelectedReview={markSelectedLabelReview}
          setMode={setMode}
          setSelectedLabel={setSelectedLabel}
        />
      </section>
    </div>
  );
}

export function SimpleMode(props: SimpleModeProps) {
  return LABEL_DEMO_MODE
    ? <DemoSimpleMode {...props} />
    : <AuthoritativeSessionPlayback sample={props.sample} setMode={props.setMode} />;
}
