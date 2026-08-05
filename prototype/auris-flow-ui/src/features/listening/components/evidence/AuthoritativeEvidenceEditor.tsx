import type { ReviewSample } from "../../fixtures/reviewSamples";
import type { HumanReviewChange } from "../../model/reviewDecisionModel";
import type { MarkState } from "../../types";
import { Check, Link2, Scissors, Tags } from "lucide-react";
import { useState, type CSSProperties } from "react";

type AuthoritativeEvidenceEditorProps = {
  sample: ReviewSample;
  markState: MarkState;
  setMarkState: (state: MarkState) => void;
  lowConfidence: boolean;
  setLowConfidence: (value: boolean) => void;
  onReviewChange: (change: HumanReviewChange) => void;
};

const finiteConfidence = (value: number) =>
  Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0;

const reviewStyles = {
  editor: {
    minHeight: 0,
    padding: 18,
    overflow: "auto",
    border: "1px solid var(--line)",
    borderRadius: 8,
    background: "var(--surface)"
  },
  header: {
    display: "flex",
    alignItems: "flex-start",
    justifyContent: "space-between",
    flexWrap: "wrap",
    gap: 16
  },
  copy: { display: "grid", flex: "2 1 420px", gap: 4 },
  muted: { margin: 0, color: "var(--muted)", fontSize: 12 },
  title: { fontSize: 18 },
  binding: {
    display: "grid",
    flex: "1 1 280px",
    gap: 4,
    padding: "10px 12px",
    border: "1px solid #bedaff",
    borderRadius: 6,
    background: "#f2f7ff",
    overflowWrap: "anywhere"
  },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,360px),1fr))",
    gap: 12,
    marginTop: 14
  },
  card: {
    display: "grid",
    alignContent: "start",
    gap: 12,
    padding: 14,
    border: "1px solid var(--line)",
    borderRadius: 6,
    background: "var(--surface-soft)"
  },
  wide: { gridColumn: "1 / -1" },
  cardHead: {
    display: "flex",
    alignItems: "flex-start",
    gap: 16
  },
  cardHeadCopy: { display: "grid", gap: 4 },
  icon: { flex: "none", marginTop: 2, color: "var(--blue)" },
  choices: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))",
    gap: 8
  },
  button: {
    minHeight: 36,
    padding: "0 12px",
    border: "1px solid var(--line)",
    borderRadius: 5,
    color: "var(--text)",
    background: "var(--surface)",
    fontWeight: 700
  },
  selected: {
    borderColor: "var(--blue)",
    color: "var(--blue)",
    background: "#f2f7ff"
  },
  disabled: { cursor: "not-allowed", opacity: 0.55 },
  target: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,240px),1fr))",
    gap: 12,
    alignItems: "end",
    paddingTop: 12,
    borderTop: "1px solid var(--line)"
  },
  targetId: {
    alignSelf: "center",
    overflowWrap: "anywhere",
    fontSize: 12
  },
  footer: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    flexWrap: "wrap",
    gap: 16,
    marginTop: 14,
    paddingTop: 12,
    borderTop: "1px solid var(--line)"
  }
} satisfies Record<string, CSSProperties>;

const actionStyle = (selected = false, disabled = false): CSSProperties => ({
  ...reviewStyles.button,
  ...(selected ? reviewStyles.selected : {}),
  ...(disabled ? reviewStyles.disabled : {})
});

export function AuthoritativeEvidenceEditor({
  sample,
  markState,
  setMarkState,
  lowConfidence,
  setLowConfidence,
  onReviewChange
}: AuthoritativeEvidenceEditorProps) {
  const [boundaryStartMs, setBoundaryStartMs] = useState(
    sample.evidenceWindowStartMs ?? 0
  );
  const [boundaryEndMs, setBoundaryEndMs] = useState(
    sample.evidenceWindowEndMs ?? 0
  );
  const [eventLinkDrafts, setEventLinkDrafts] = useState(
    Object.fromEntries(
      (sample.authoritativeEventLinks ?? []).map((target) => [
        target.id,
        { ...target }
      ])
    )
  );
  const [labelDrafts, setLabelDrafts] = useState(
    Object.fromEntries(
      (sample.authoritativeLabelCandidates ?? []).map((target) => [
        target.id,
        { ...target }
      ])
    )
  );
  const [stagedKeys, setStagedKeys] = useState<string[]>([]);
  const stage = (key: string, change: HumanReviewChange) => {
    onReviewChange(change);
    setStagedKeys((current) =>
      current.includes(key) ? current : [...current, key]
    );
  };
  const boundaryId = sample.boundaryIds?.[0] ?? "";
  const boundaryValid =
    Boolean(boundaryId) &&
    Number.isInteger(boundaryStartMs) &&
    Number.isInteger(boundaryEndMs) &&
    boundaryStartMs >= 0 &&
    boundaryEndMs > boundaryStartMs;

  return (
    <section
      className="authoritative-review-editor"
      style={reviewStyles.editor}
      data-testid="listening-authoritative-editor"
      data-review-task-id={sample.reviewTaskId}
      data-root-trace-id={sample.rootTraceId}
    >
      <header className="authoritative-review-head" style={reviewStyles.header}>
        <div style={reviewStyles.copy}>
          <span style={reviewStyles.muted}>权威证据编辑</span>
          <strong style={reviewStyles.title}>{sample.title}</strong>
          <p style={{ ...reviewStyles.muted, fontSize: 13 }}>
            仅编辑当前 HumanReviewTask 声明的目标；所有修改先进入待提交决定，
            写后回读一致才进入下一通。
          </p>
        </div>
        <div
          className="authoritative-review-binding"
          style={reviewStyles.binding}
          aria-label="当前强绑定"
        >
          <span style={reviewStyles.muted}>任务 {sample.reviewTaskId}</span>
          <span style={reviewStyles.muted}>证据 {sample.dataAssetId}</span>
          <span style={reviewStyles.muted}>Trace {sample.rootTraceId}</span>
        </div>
      </header>

      <div className="authoritative-review-grid" style={reviewStyles.grid}>
        <section className="authoritative-review-card" style={reviewStyles.card}>
          <div className="authoritative-review-card-head" style={reviewStyles.cardHead}>
            <Check size={15} style={reviewStyles.icon} />
            <div style={reviewStyles.cardHeadCopy}>
              <strong>录音与置信判断</strong>
              <span style={reviewStyles.muted}>写入 EvidencePack review_overrides</span>
            </div>
          </div>
          <div
            className="authoritative-choice-row"
            style={reviewStyles.choices}
            role="group"
            aria-label="录音归类 recording_disposition"
          >
            {([
              ["main", "标记主录音"],
              ["crosstalk", "标记串音"],
              ["duplicate", "标记重复收录"]
            ] as const).map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={markState === value ? "active" : ""}
                style={actionStyle(markState === value)}
                aria-pressed={markState === value}
                onClick={() => setMarkState(value)}
              >
                {markState === value ? `${label}（待提交）` : label}
              </button>
            ))}
          </div>
          <button
            type="button"
            className={lowConfidence ? "authoritative-toggle active" : "authoritative-toggle"}
            style={{ ...actionStyle(lowConfidence), justifySelf: "start" }}
            aria-pressed={lowConfidence}
            aria-label="低置信覆盖 low_confidence"
            onClick={() => setLowConfidence(!lowConfidence)}
          >
            {lowConfidence ? "低置信修订待提交" : "标记低置信"}
          </button>
          <small style={reviewStyles.muted}>
            {markState === "none" && !lowConfidence
              ? "尚未修改"
              : "录音与低置信修订待提交，不会在本地宣称已保存。"}
          </small>
        </section>

        <section className="authoritative-review-card" style={reviewStyles.card}>
          <div className="authoritative-review-card-head" style={reviewStyles.cardHead}>
            <Scissors size={15} style={reviewStyles.icon} />
            <div style={reviewStyles.cardHeadCopy}>
              <strong>会话边界</strong>
              <span style={reviewStyles.muted}>
                {boundaryId || "当前任务未声明 conversation_boundary"}
              </span>
            </div>
          </div>
          <div className="entity-form-grid authoritative-boundary-fields">
            <label>
              <span>开始（毫秒）</span>
              <input
                type="number"
                min={0}
                step={100}
                value={boundaryStartMs}
                disabled={!boundaryId}
                onChange={(event) =>
                  setBoundaryStartMs(event.currentTarget.valueAsNumber)
                }
              />
            </label>
            <label>
              <span>结束（毫秒）</span>
              <input
                type="number"
                min={0}
                step={100}
                value={boundaryEndMs}
                disabled={!boundaryId}
                onChange={(event) =>
                  setBoundaryEndMs(event.currentTarget.valueAsNumber)
                }
              />
            </label>
          </div>
          <button
            type="button"
            disabled={!boundaryValid}
            style={actionStyle(false, !boundaryValid)}
            title={
              !boundaryId
                ? "当前 HumanReviewTask 未绑定可修订的会话边界。"
                : !boundaryValid
                  ? "结束时间必须大于开始时间。"
                  : "把权威时间窗修订加入当前决定。"
            }
            onClick={() =>
              stage(`conversation_boundary:${boundaryId}`, {
                target_type: "conversation_boundary",
                target_id: boundaryId,
                fields: {
                  start_ms: boundaryStartMs,
                  end_ms: boundaryEndMs,
                  decision: "manual_confirmed",
                  merged_slice_ids: [],
                  split_slice_ids: [],
                  extension_ids: []
                }
              })
            }
          >
            {stagedKeys.includes(`conversation_boundary:${boundaryId}`)
              ? "边界修订待提交"
              : "加入边界修订"}
          </button>
        </section>

        <section className="authoritative-review-card authoritative-review-wide"
          style={{ ...reviewStyles.card, ...reviewStyles.wide }}>
          <div className="authoritative-review-card-head" style={reviewStyles.cardHead}>
            <Link2 size={15} style={reviewStyles.icon} />
            <div style={reviewStyles.cardHeadCopy}>
              <strong>EventLink 修订</strong>
              <span style={reviewStyles.muted}>只显示当前任务明确绑定的事件关联</span>
            </div>
          </div>
          {(sample.authoritativeEventLinks ?? []).length === 0 ? (
            <p className="authoritative-empty" style={reviewStyles.muted}>
              当前 HumanReviewTask 未绑定可修订的 EventLink。
            </p>
          ) : (
            (sample.authoritativeEventLinks ?? []).map((target) => {
              const draft = eventLinkDrafts[target.id] ?? target;
              const key = `event_link:${target.id}`;
              return (
                <div
                  className="authoritative-target-row"
                  style={reviewStyles.target}
                  key={target.id}
                >
                  <strong style={reviewStyles.targetId}>{target.id}</strong>
                  <div className="entity-form-grid">
                    <label>
                      <span>业务单据</span>
                      <input
                        value={draft.documentRef}
                        onChange={(event) =>
                          setEventLinkDrafts((current) => ({
                            ...current,
                            [target.id]: {
                              ...draft,
                              documentRef: event.currentTarget.value
                            }
                          }))
                        }
                      />
                    </label>
                    <label>
                      <span>关系类型</span>
                      <input
                        value={draft.relationType}
                        onChange={(event) =>
                          setEventLinkDrafts((current) => ({
                            ...current,
                            [target.id]: {
                              ...draft,
                              relationType: event.currentTarget.value
                            }
                          }))
                        }
                      />
                    </label>
                    <label className="entity-form-wide">
                      <span>证据窗口</span>
                      <input
                        value={draft.evidenceWindow}
                        onChange={(event) =>
                          setEventLinkDrafts((current) => ({
                            ...current,
                            [target.id]: {
                              ...draft,
                              evidenceWindow: event.currentTarget.value
                            }
                          }))
                        }
                      />
                    </label>
                  </div>
                  <button
                    type="button"
                    disabled={!draft.evidenceWindow.trim()}
                    style={actionStyle(false, !draft.evidenceWindow.trim())}
                    onClick={() => {
                      const fields: Record<string, unknown> = {
                        confidence: finiteConfidence(draft.confidence),
                        evidence_window: draft.evidenceWindow.trim()
                      };
                      if (draft.sourceEventId.trim()) {
                        fields.source_event_id = draft.sourceEventId.trim();
                      }
                      if (draft.documentRef.trim()) {
                        fields.document_ref = draft.documentRef.trim();
                      }
                      if (draft.relationType.trim()) {
                        fields.relation_type = draft.relationType.trim();
                      }
                      stage(key, {
                        target_type: "event_link",
                        target_id: target.id,
                        fields
                      });
                    }}
                  >
                    {stagedKeys.includes(key)
                      ? "EventLink 修订待提交"
                      : "加入 EventLink 修订"}
                  </button>
                </div>
              );
            })
          )}
        </section>

        <section className="authoritative-review-card authoritative-review-wide"
          style={{ ...reviewStyles.card, ...reviewStyles.wide }}>
          <div className="authoritative-review-card-head" style={reviewStyles.cardHead}>
            <Tags size={15} style={reviewStyles.icon} />
            <div style={reviewStyles.cardHeadCopy}>
              <strong>标签候选修订</strong>
              <span style={reviewStyles.muted}>
                只修改 EvidencePack 回读的 label_candidate
              </span>
            </div>
          </div>
          {(sample.authoritativeLabelCandidates ?? []).length === 0 ? (
            <p className="authoritative-empty" style={reviewStyles.muted}>
              当前 HumanReviewTask 未绑定可修订的标签候选。
            </p>
          ) : (
            (sample.authoritativeLabelCandidates ?? []).map((target) => {
              const draft = labelDrafts[target.id] ?? target;
              const key = `label_candidate:${target.id}`;
              return (
                <div
                  className="authoritative-target-row"
                  style={reviewStyles.target}
                  key={target.id}
                >
                  <strong style={reviewStyles.targetId}>{target.id}</strong>
                  <div className="entity-form-grid">
                    <label>
                      <span>标签</span>
                      <input
                        value={draft.label}
                        onChange={(event) =>
                          setLabelDrafts((current) => ({
                            ...current,
                            [target.id]: {
                              ...draft,
                              label: event.currentTarget.value
                            }
                          }))
                        }
                      />
                    </label>
                    <label>
                      <span>修订值</span>
                      <input
                        value={draft.value}
                        onChange={(event) =>
                          setLabelDrafts((current) => ({
                            ...current,
                            [target.id]: {
                              ...draft,
                              value: event.currentTarget.value
                            }
                          }))
                        }
                      />
                    </label>
                  </div>
                  <button
                    type="button"
                    disabled={!draft.label.trim() || !draft.value.trim()}
                    style={actionStyle(
                      false,
                      !draft.label.trim() || !draft.value.trim()
                    )}
                    onClick={() =>
                      stage(key, {
                        target_type: "label_candidate",
                        target_id: target.id,
                        fields: {
                          label: draft.label.trim(),
                          value: draft.value.trim(),
                          confidence: finiteConfidence(draft.confidence)
                        }
                      })
                    }
                  >
                    {stagedKeys.includes(key)
                      ? "标签修订待提交"
                      : "加入标签修订"}
                  </button>
                </div>
              );
            })
          )}
        </section>
      </div>

      <footer className="authoritative-review-footer" style={reviewStyles.footer}
        aria-live="polite">
        <strong>
          {stagedKeys.length > 0
            ? `${stagedKeys.length} 项结构化修订待提交`
            : "尚无结构化修订"}
        </strong>
        <span style={reviewStyles.muted}>
          主录音、串音、重复和低置信也会由底部决定栏统一提交；这里不会直接写后端。
        </span>
      </footer>
    </section>
  );
}
