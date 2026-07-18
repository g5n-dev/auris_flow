import type { EvidencePanelController } from "./hotwordCorrectionActions";
import { Check, FileText, GitBranch, ShieldCheck, Sparkles, UserCheck } from "lucide-react";

export function DiffEvidencePanel({ controller }: { controller: EvidencePanelController }) {
  const { activeDiff, activeDiffDecision, conflictDiffs, diffCandidate, diffTone, hotwordCorrection, hotwordCorrectionBlockedReason, hotwordCorrectionNotice, hotwordCorrectionPending, hotwordCorrectionRecovery, panelTab, recordedHotwordCorrection, reusesExistingBadcase, sample, selectedDiffField, setHotwordCorrection, setPanelTab, setSelectedDiffField, submitHotwordCorrection, updateDiffDecision } = controller;
  return (
    panelTab === "diff" && (
            <div className="panel-body">
              <section className="field-diff-workbench">
                <div className="field-diff-summary">
                  <div>
                    <span>字段差异复核</span>
                    <strong>{conflictDiffs.length} 个需处理 · {sample.activeTime}</strong>
                  </div>
                  <b className={conflictDiffs.length ? "warn" : "ok"}>{conflictDiffs.length ? "待决策" : "一致"}</b>
                </div>
                <div className="field-diff-list" aria-label="字段差异列表">
                  {sample.mismatches.map((item) => (
                    <button
                      type="button"
                      className={["field-diff-item", diffTone(item.state), selectedDiffField === item.field ? "active" : ""].join(" ")}
                      key={item.field}
                      onClick={() => setSelectedDiffField(item.field)}
                    >
                      <span>{item.field}</span>
                      <b>{item.state}</b>
                      <em>{item.audio}</em>
                      <i>{item.doc}</i>
                    </button>
                  ))}
                </div>

                {activeDiff && (
                  <div className={`field-diff-detail ${diffTone(activeDiff.state)}`}>
                    <div className="field-diff-detail-head">
                      <div>
                        <span>当前字段</span>
                        <strong>{activeDiff.field}</strong>
                      </div>
                      <b>{activeDiff.state}</b>
                    </div>
                    <div className="field-source-compare">
                      <button
                        type="button"
                        className={activeDiffDecision === "asr" ? "selected" : ""}
                        onClick={() => updateDiffDecision("asr")}
                        title="采信口播识别结果并生成单据回填建议"
                      >
                        <span>ASR 口播</span>
                        <strong>{activeDiff.audio}</strong>
                        <small>{sample.file}</small>
                      </button>
                      <button
                        type="button"
                        className={activeDiffDecision === "doc" ? "selected" : ""}
                        onClick={() => updateDiffDecision("doc")}
                        title="采信业务单据字段并标记当前音频低置信"
                      >
                        <span>业务单据</span>
                        <strong>{activeDiff.doc}</strong>
                        <small>{diffCandidate?.orderNo ?? sample.docs[0]?.id}</small>
                      </button>
                    </div>
                    <p>
                      {activeDiff.state === "一致"
                        ? "ASR 与单据字段一致，可作为自动通过证据。"
                        : activeDiff.state.includes("缺")
                          ? "单据存在字段但当前音频未覆盖，需要补查相邻音频或转人工确认。"
                          : "ASR 口播、报价单字段和当前设备焦点不一致，需要决定采信来源并写入复核结论。"}
                    </p>
                    <div className="field-diff-links">
                      <button type="button" onClick={() => setPanelTab("docs")}>
                        <FileText size={13} />
                        看单据链
                      </button>
                      <button type="button" onClick={() => setPanelTab("agent")}>
                        <Sparkles size={13} />
                        看Agent解释
                      </button>
                      <button type="button" onClick={() => setPanelTab("crosstalk")}>
                        <GitBranch size={13} />
                        查串音影响
                      </button>
                    </div>
                    <div className="field-diff-actions">
                      <button type="button" className={activeDiffDecision === "asr" ? "selected" : ""} onClick={() => updateDiffDecision("asr")}>
                        <Check size={14} />
                        采信ASR
                      </button>
                      <button type="button" className={activeDiffDecision === "doc" ? "selected" : ""} onClick={() => updateDiffDecision("doc")}>
                        <ShieldCheck size={14} />
                        采信单据
                      </button>
                      <button type="button" className={activeDiffDecision === "human" ? "selected danger" : "danger"} onClick={() => updateDiffDecision("human")}>
                        <UserCheck size={14} />
                        转人工
                      </button>
                    </div>
                    {activeDiffDecision && (
                      <div className="field-diff-decision">
                        <span>已暂存决策</span>
                        <strong>
                          {activeDiffDecision === "asr" ? "采信 ASR 并生成单据回填" : activeDiffDecision === "doc" ? "采信单据并标记音频低置信" : "进入人工复核队列"}
                        </strong>
                      </div>
                    )}
                  </div>
                )}
                <section className="asr-hotword-correction" data-testid="asr-hotword-correction">
                  <div className="asr-hotword-correction-head">
                    <div>
                      <span>ASR Diff / 热词证据</span>
                      <strong>
                        {recordedHotwordCorrection
                          ? `已记录 ${recordedHotwordCorrection.correctionId}`
                          : hotwordCorrectionRecovery.status === "ready"
                              ? `已有 ${hotwordCorrectionRecovery.existingBadcaseId}`
                          : "恢复热词 Badcase"}
                      </strong>
                      <p>{reusesExistingBadcase ? "生成不可变修正；幂等不重复。" : "无受控证据则阻断，不覆盖转写。"}</p>
                    </div>
                    <b>capability=asr-hotword</b>
                  </div>
                  <div
                    className={`operation-toast is-${hotwordCorrectionRecovery.status === "ready" ? "success" : hotwordCorrectionRecovery.status === "loading" ? "pending" : "error"}`}
                    data-testid="asr-hotword-authority-binding"
                    role="status"
                    aria-live="polite"
                  >
                    <strong>
                      {hotwordCorrectionRecovery.status === "ready"
                        ? `已有 ${hotwordCorrectionRecovery.existingBadcaseId} · 复用后端 Badcase`
                        : hotwordCorrectionRecovery.status === "loading"
                          ? "正在恢复权威绑定"
                          : "权威绑定恢复已阻断"}
                    </strong>
                    <span>{hotwordCorrectionRecovery.reason}</span>
                  </div>
                  <label>
                    <span>识别文本</span>
                    <input
                      aria-label="识别文本"
                      disabled={hotwordCorrectionPending || Boolean(recordedHotwordCorrection)}
                      value={hotwordCorrection.recognizedText}
                      onChange={(event) => setHotwordCorrection((current) => ({ ...current, recognizedText: event.target.value }))}
                    />
                  </label>
                  <label>
                    <span>正确文本</span>
                    <input
                      aria-label="正确文本"
                      disabled={hotwordCorrectionPending || Boolean(recordedHotwordCorrection)}
                      value={hotwordCorrection.correctedText}
                      onChange={(event) => setHotwordCorrection((current) => ({ ...current, correctedText: event.target.value }))}
                    />
                  </label>
                  <label>
                    <span>错误类型</span>
                    <select
                      aria-label="错误类型"
                      disabled={hotwordCorrectionPending || Boolean(recordedHotwordCorrection)}
                      value={hotwordCorrection.errorType}
                      onChange={(event) => setHotwordCorrection((current) => ({ ...current, errorType: event.target.value as typeof current.errorType }))}
                    >
                      <option value="missing_term">missing_term</option>
                      <option value="misrecognition">misrecognition</option>
                      <option value="alias_gap">alias_gap</option>
                      <option value="weight_issue">weight_issue</option>
                      <option value="false_boost">false_boost</option>
                    </select>
                  </label>
                  <label>
                    <span>证据窗口</span>
                    <input
                      aria-label="证据窗口"
                      disabled={hotwordCorrectionPending || Boolean(recordedHotwordCorrection)}
                      value={hotwordCorrection.evidenceWindow}
                      onChange={(event) => setHotwordCorrection((current) => ({ ...current, evidenceWindow: event.target.value }))}
                    />
                  </label>
                  <div
                    className={`operation-toast is-${hotwordCorrectionNotice.status}`}
                    data-testid="asr-hotword-correction-status"
                    role="status"
                    aria-live="polite"
                  >
                    <strong>{hotwordCorrectionNotice.title}</strong>
                    <span>{hotwordCorrectionNotice.detail}</span>
                  </div>
                  <button
                    type="button"
                    className="primary"
                    data-testid="asr-hotword-badcase-submit"
                    disabled={Boolean(hotwordCorrectionBlockedReason) || hotwordCorrectionPending}
                    title={hotwordCorrectionBlockedReason || (recordedHotwordCorrection ? `打开 ${recordedHotwordCorrection.badcaseId}` : "记录修正，等待人审。")}
                    onClick={() => void submitHotwordCorrection()}
                  >
                    {hotwordCorrectionPending
                      ? "记录中..."
                      : recordedHotwordCorrection
                        ? `已记录 · 查看 ${recordedHotwordCorrection.badcaseId}`
                        : hotwordCorrectionRecovery.status === "ready"
                      ? `记录修正并关联 ${hotwordCorrectionRecovery.existingBadcaseId}`
                      : "等待权威 Badcase 绑定"}
                  </button>
                  {hotwordCorrectionBlockedReason && <small>blocked：{hotwordCorrectionBlockedReason}</small>}
                </section>
              </section>
            </div>
          )
  );
}
