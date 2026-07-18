import type { CanvasController } from "../../controller/useCanvasController";
import { Check, LockKeyhole, ShieldCheck } from "lucide-react";

export function DrawerAudioRuntime({ controller }: { controller: CanvasController }) {
  const { asrExecutionMode, asrHotwordVersionId, canvasAction, hotwordPackVersionOptions, hotwordVersionOptionsLoading, resolvedAsrTaskBindingRows, saveTaskDraft, selectedAudioRuntimeParams, selectedHotwordPackVersion, selectedNodeId, updateAsrExecutionMode, updateAsrHotwordVersion } = controller;
  return (
    <>
      {(["vad", "diar", "asr"].includes(selectedNodeId)) && (
                  <section className="service-binding-card">
                    <div className="dagster-binding-head">
                      <span>音频智能服务绑定</span>
                      <strong>{selectedNodeId === "vad" ? "VAD Output" : selectedNodeId === "diar" ? "Diar Output" : "ASR Output"}</strong>
                    </div>
                    <p>
                      服务端点、认证和云厂商适配在设置页注册；任务配置只绑定 provider、pipeline 参数和输出契约。VAD、Diar、ASR 可以来自同一服务调用，但按资产拆分物化。
                    </p>
                    <div className="service-contract-list">
                      {resolvedAsrTaskBindingRows.map(([label, value, detail]) => (
                        <div key={label} className="service-contract-row">
                          <span>{label}</span>
                          <strong>{value}</strong>
                          <em>{detail}</em>
                        </div>
                      ))}
                    </div>
                    <div className="audio-runtime-param-list">
                      <div className="audio-ops-head">
                        <span>当前节点请求参数</span>
                        <strong>run_config.{selectedNodeId}</strong>
                      </div>
                      {selectedAudioRuntimeParams.map(([label, value]) => (
                        <div key={label} className="service-contract-row">
                          <span>{label}</span>
                          <strong>{value}</strong>
                        </div>
                      ))}
                    </div>
                    {selectedNodeId === "asr" && (
                      <section className="hotword-version-binding" data-testid="hotword-version-binding">
                        <div className="dagster-binding-head">
                          <span>ASR 热词包版本绑定</span>
                          <strong>不可变版本引用</strong>
                        </div>
                        <label>
                          <span>execution_mode</span>
                          <select value={asrExecutionMode} onChange={(event) => updateAsrExecutionMode(event.target.value as "production" | "shadow")}>
                            <option value="production">production</option>
                            <option value="shadow">shadow</option>
                          </select>
                        </label>
                        <label>
                          <span>hotword_pack_version_id</span>
                          <select
                            value={asrHotwordVersionId}
                            disabled={hotwordVersionOptionsLoading || hotwordPackVersionOptions.length === 0}
                            onChange={(event) => updateAsrHotwordVersion(event.target.value)}
                          >
                            {hotwordVersionOptionsLoading && <option value="">loading hotword versions...</option>}
                            {!hotwordVersionOptionsLoading && hotwordPackVersionOptions.length === 0 && <option value="">blocked：无可用版本</option>}
                            {hotwordPackVersionOptions.map((version) => (
                              <option key={version.id} value={version.id} disabled={asrExecutionMode === "production" && version.status !== "published"}>
                                {version.id} · {version.label} · {version.status}
                              </option>
                            ))}
                          </select>
                        </label>
                        <div className="hotword-binding-guardrails">
                          <span><Check size={13} />生产运行仅允许已发布版本</span>
                          <span><ShieldCheck size={13} />候选版本只能用于 shadow</span>
                          <span><LockKeyhole size={13} />历史 hotwords_ref 只读兼容</span>
                        </div>
                        <button
                          type="button"
                          disabled={canvasAction === "save" || hotwordVersionOptionsLoading || !selectedHotwordPackVersion}
                          title={canvasAction === "save"
                            ? "热词绑定正在保存。"
                            : !selectedHotwordPackVersion
                              ? "blocked：未从后端恢复可用热词版本。"
                              : "保存到当前 TaskVersion 草稿，不切换生产。"}
                          onClick={() => void saveTaskDraft()}
                        >
                          {canvasAction === "save" ? "热词绑定保存中" : "保存热词版本绑定"}
                        </button>
                      </section>
                    )}
                  </section>
                )}
    </>
  );
}
