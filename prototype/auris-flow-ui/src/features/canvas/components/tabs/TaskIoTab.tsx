import type { CanvasController } from "../../controller/useCanvasController";
import { loginRiskApiContracts } from "../../catalog";
import { Database, Download, Link2, Plus, ShieldCheck } from "lucide-react";

export function TaskIoTab({ controller }: { controller: CanvasController }) {
  const { activeTab, appliedMappingCount, contractNodeMap, generateMappingSuggestions, mappingTotal, openFieldMappingEditor, openInputSourceEditor, openOutputSinkTemplate, openOutputWritebackEditor, openScheduleSettings, renderTaskAssetCanvas, setDrawerTab, setNodeLibraryOpen, setSelectedNodeId, validateDagsterCompatibility } = controller;
  return (
    <>
      {activeTab === "io" && (
                      <div className="task-tab-grid io-binding-grid">
                        {renderTaskAssetCanvas()}
                        <section className="task-tab-card wide task-io-edit-card">
                          <div className="task-tab-card-head">
                            <span>数据配置入口</span>
                            <b>可修改</b>
                          </div>
                          <strong>输入源、字段映射、输出回写都从这里进入编辑</strong>
                          <p>当前页不只是展示契约；编辑会联动右侧节点配置、字段映射和 Output Sink 演示数据，保存后写入当前任务草稿。</p>
                          <div className="task-io-edit-actions">
                            <button type="button" onClick={() => openInputSourceEditor("platformAuth")}>
                              <Database size={14} />
                              编辑输入源
                              <em>认证 / 外部数据源 / 演示载荷</em>
                            </button>
                            <button type="button" onClick={openFieldMappingEditor}>
                              <Link2 size={14} />
                              编辑字段映射
                              <em>字段口径 / 聚合键 / 人工确认</em>
                            </button>
                            <button type="button" onClick={openOutputWritebackEditor}>
                              <Download size={14} />
                              编辑输出回写
                              <em>平台回调 / WAV URL / 证据包</em>
                            </button>
                          </div>
                        </section>
                        <section className="task-tab-card wide task-input-source-card">
                          <div className="task-tab-card-head">
                            <span>平台同步抽取</span>
                            <button onClick={() => setNodeLibraryOpen(true)}>
                              <Plus size={13} />
                              添加输入
                            </button>
                          </div>
                          {[
                            ["CRM/平台认证", "platform_session", "账户名密码 / Token / Cookie", "platformAuth"],
                            ["租户门店主数据", "tenant_list_api", "租户 / 门店 / 组织层级", "tenantApi"],
                            ["员工与工牌", "employee_list_api", "销售 / 坐席 / 声纹主体", "employeeApi"],
                            ["PBX/S3/平台音频 URL", "recording_url_api", "录音地址 / 时长 / 设备", "audioUrlApi"],
                            ["认证业务事件", "authenticated_event_api", "登录 / 报价 / 试驾 / 单据事件", "eventApi"]
                          ].map(([name, key, role, nodeId]) => (
                            <button
                              key={key}
                              className="task-binding-row"
                              onClick={() => {
                                setSelectedNodeId(nodeId);
                                setDrawerTab("overview");
                              }}
                            >
                              <strong>{name}</strong>
                              <span>{key}</span>
                              <em>{role}</em>
                            </button>
                          ))}
                        </section>
                        <section className="task-tab-card wide task-api-contract-card">
                          <div className="task-tab-card-head">
                            <span>流程接口契约 / BFF → 执行层</span>
                            <button onClick={validateDagsterCompatibility}>
                              <ShieldCheck size={13} />
                              校验兼容性
                            </button>
                          </div>
                          <p>业务接口先进入 BFF 资源接口，再映射到底层资源引用和处理资产；页面只暴露产品语言，底层保留 asset key、partition、run_id 和 trace_id。</p>
                          <div className="task-contract-list">
                            {loginRiskApiContracts.map((contract) => (
                              <button
                                key={contract.name}
                                type="button"
                                className="task-contract-row"
                                onClick={() => {
                                  setSelectedNodeId(contractNodeMap[contract.name] ?? "dagster");
                                  setDrawerTab("mapping");
                                }}
                              >
                                <span>{contract.method}</span>
                                <strong>{contract.name}</strong>
                                <em>{contract.path}</em>
                                <b>{contract.dagster}</b>
                                <small>{contract.auth}</small>
                                <i>{contract.request} → {contract.response}</i>
                              </button>
                            ))}
                          </div>
                        </section>
                        <section className="task-tab-card task-output-assets-card">
                          <span>输出资产</span>
                          <strong>证据包 / 标签结果 / 处理后音频 / 导出清单</strong>
                          <p>流程运行成功后写入资产目录，并保留当前运行、模型版本和流程版本血缘。</p>
                          <button onClick={openScheduleSettings}>查看执行计划</button>
                        </section>
                        <section className="task-tab-card task-agent-mapping-card">
                          <span>Agent 字段映射</span>
                          <strong>{appliedMappingCount}/{mappingTotal} 已应用</strong>
                          <p>Agent 根据平台字段、单据字段、音频元数据和标签体系生成映射建议，再由人工确认高风险字段。</p>
                          <button onClick={generateMappingSuggestions}>生成并查看建议</button>
                        </section>
                        <section className="task-tab-card wide task-output-target-card">
                          <div className="task-tab-card-head">
                            <span>导出与回写目标</span>
                            <button onClick={() => openOutputSinkTemplate("platform-callback-output")}>
                              <Plus size={13} />
                              添加回写
                            </button>
                          </div>
                          <strong>处理后 WAV / 证据包 / 标签结果 → OBS/S3 / API / CSV</strong>
                          <p>这是流程运行后的 Output Sink：先上传处理后的音频和证据资产，再把 URL、标签、复核结论和导出清单回写业务平台。</p>
                          {[
                            ["处理后 WAV 上传", "obs://auris-processed-audio/{tenant}/{task_run}/", "生成 processed_wav_url", "obs-wav-output"],
                            ["平台 URL 回调", "POST /api/v1/output-sinks/platform-callbacks", "回写 wav_url / 标签 / 证据包", "platform-callback-output"]
                          ].map(([name, key, role, templateKey]) => (
                            <button key={name} className="task-binding-row" onClick={() => openOutputSinkTemplate(templateKey)}>
                              <strong>{name}</strong>
                              <span>{key}</span>
                              <em>{role}</em>
                            </button>
                          ))}
                        </section>
                      </div>
                    )}
    </>
  );
}
