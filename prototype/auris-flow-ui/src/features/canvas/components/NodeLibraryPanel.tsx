import type { CanvasController } from "../controller/useCanvasController";
import { dagsterDefinitionForTemplate, defaultDraftForTemplate, nodeWriteModeForTemplate } from "../nodeTemplates";
import { Check, X } from "lucide-react";

export function NodeLibraryPanel({ controller }: { controller: CanvasController }) {
  const { activeIntent, addConfiguredNode, displayExecutionDefinition, draftOutputContract, markTaskDraftDirty, nodeDraft, nodeLibraryOpen, nodeTemplateCategories, selectNodeTemplate, selectedTemplate, selectedTemplateKey, setCanvasNotice, setNodeDraft, setNodeLibraryOpen, updateNodeDraft } = controller;
  return (
    <>
      {nodeLibraryOpen && (
                  <div className="node-library-panel" role="dialog" aria-label="添加节点">
                    <div className="node-library-head">
                      <div>
                        <span>节点库</span>
                        <strong>添加到当前任务草稿</strong>
                      </div>
                      <button onClick={() => setNodeLibraryOpen(false)} aria-label="关闭节点库">
                        <X size={15} />
                      </button>
                    </div>
                    <p>按“从平台抽取数据 → 智能处理 → 把结果推回平台”配置当前任务版本；不会创建或修改全局数据源。</p>
                    <div className="node-library-content">
                      <div className="node-template-groups">
                        {nodeTemplateCategories.map(({ category, templates }) => (
                          <section key={category} className="node-template-group">
                            <span>{category}</span>
                            {templates.map((template) => {
                              const TemplateIcon = template.node.icon;
                              return (
                                <button
                                  key={template.key}
                                  className={selectedTemplateKey === template.key ? "selected" : ""}
                                  onClick={() => selectNodeTemplate(template)}
                                >
                                  <TemplateIcon size={16} />
                                  <strong>{template.title}</strong>
                                  <em>{template.description}</em>
                                </button>
                              );
                            })}
                          </section>
                        ))}
                      </div>
                      <section className="node-config-panel">
                        <div className="node-config-title">
                          <span>节点配置</span>
                          <strong>{selectedTemplate.adapterKind ?? selectedTemplate.category} · 当前任务版本草稿</strong>
                        </div>
                        <div className="node-adapter-profile">
                          <div>
                            <span>HTTP</span>
                            <b>
                              {nodeDraft.httpMethod || selectedTemplate.method || selectedTemplate.node.metaA} {nodeDraft.endpoint || selectedTemplate.endpoint || selectedTemplate.node.metaB}
                            </b>
                          </div>
                          <div>
                            <span>认证</span>
                            <b>{selectedTemplate.authMode ?? "继承当前任务认证"}</b>
                          </div>
                          <div>
                            <span>底层定义</span>
                            <b>{displayExecutionDefinition(dagsterDefinitionForTemplate(selectedTemplate))}</b>
                          </div>
                          <div>
                            <span>IO Manager</span>
                            <b>{selectedTemplate.defaultIoManager ?? nodeDraft.ioManager}</b>
                          </div>
                          <div className="wide">
                            <span>依赖</span>
                            <b>{selectedTemplate.depsHint ?? nodeDraft.input}</b>
                          </div>
                        </div>
                        {selectedTemplate.outputSchema?.length ? (
                          <div className="node-schema-chips" aria-label="资产字段组">
                            {selectedTemplate.outputSchema.map((field) => (
                              <span key={field}>{field}</span>
                            ))}
                          </div>
                        ) : null}
                        <div className="node-data-edit-panel" aria-label="数据配置编辑">
                          <div className="node-data-edit-head">
                            <div>
                              <span>数据配置 / 演示数据</span>
                              <strong>这里修改数据源、查询条件和字段映射</strong>
                            </div>
                            <button
                              type="button"
                              onClick={() => {
                                markTaskDraftDirty();
                                setCanvasNotice({
                                  status: "success",
                                  title: "数据配置已更新到草稿",
                                  detail: "当前节点的数据源、查询条件和字段映射已更新；发布或运行前会先写入 BFF 任务版本。"
                                });
                              }}
                            >
                              <Check size={13} />
                              保存数据配置
                            </button>
                          </div>
                          <div className="node-data-edit-grid">
                            <label>
                              HTTP Method
                              <input value={nodeDraft.httpMethod} onChange={(event) => updateNodeDraft("httpMethod", event.target.value)} />
                            </label>
                            <label>
                              API Path
                              <input value={nodeDraft.endpoint} onChange={(event) => updateNodeDraft("endpoint", event.target.value)} />
                            </label>
                            <label>
                              Source ID
                              <input value={nodeDraft.sourceId} onChange={(event) => updateNodeDraft("sourceId", event.target.value)} />
                            </label>
                            <label>
                              资源类型
                              <input value={nodeDraft.resourceType} onChange={(event) => updateNodeDraft("resourceType", event.target.value)} />
                            </label>
                            <label className="wide">
                              Query / Filter
                              <input value={nodeDraft.queryParams} onChange={(event) => updateNodeDraft("queryParams", event.target.value)} />
                            </label>
                            <label>
                              分区规则
                              <input value={nodeDraft.partitionRule} onChange={(event) => updateNodeDraft("partitionRule", event.target.value)} />
                            </label>
                            <label>
                              聚合键
                              <input value={nodeDraft.aggregateKeys} onChange={(event) => updateNodeDraft("aggregateKeys", event.target.value)} />
                            </label>
                            <label className="wide">
                              字段映射
                              <textarea value={nodeDraft.fieldMapping} onChange={(event) => updateNodeDraft("fieldMapping", event.target.value)} rows={4} />
                            </label>
                            <label className="wide">
                              示例入参
                              <textarea value={nodeDraft.mockPayload} onChange={(event) => updateNodeDraft("mockPayload", event.target.value)} rows={5} />
                            </label>
                            <label className="wide">
                              写入策略
                              <input value={nodeDraft.writePolicy} onChange={(event) => updateNodeDraft("writePolicy", event.target.value)} />
                            </label>
                          </div>
                        </div>
                        <div className="node-asset-contract-panel" aria-label="输出资产契约">
                          <div className="node-asset-contract-head">
                            <span>输出资产契约</span>
                            <strong>{draftOutputContract.assetKey}</strong>
                            <p>{draftOutputContract.description}</p>
                          </div>
                          <div className="node-asset-flow">
                            <div>
                              <span>上游</span>
                              <strong>{draftOutputContract.upstream.length ? draftOutputContract.upstream.join(" + ") : "外部连接器"}</strong>
                            </div>
                            <b>→</b>
                            <div>
                              <span>{draftOutputContract.kind}</span>
                              <strong>{draftOutputContract.displayName}</strong>
                            </div>
                            <b>→</b>
                            <div>
                              <span>物化</span>
                              <strong>{draftOutputContract.materialization}</strong>
                            </div>
                          </div>
                          <div className="node-asset-kv">
                            <span>BFF/API</span>
                            <b>{draftOutputContract.api}</b>
                            <span>分区</span>
                            <b>{draftOutputContract.partition}</b>
                            <span>聚合键</span>
                            <b>{draftOutputContract.aggregateKeys.join(" / ")}</b>
                          </div>
                          <div className="node-asset-schema">
                            {draftOutputContract.schema.map(([group, fields]) => (
                              <div key={group}>
                                <span>{group}</span>
                                <strong>{fields}</strong>
                              </div>
                            ))}
                          </div>
                        </div>
                        {selectedTemplate.runtimeLinks?.length ? (
                          <div className="node-runtime-links" aria-label="处理和查看入口">
                            {selectedTemplate.runtimeLinks.map(([label, value, detail]) => (
                              <div key={label}>
                                <span>{label}</span>
                                <strong>{value}</strong>
                                <em>{detail}</em>
                              </div>
                            ))}
                          </div>
                        ) : null}
                        <div className="node-config-form">
                          <label>
                            节点名称
                            <input value={nodeDraft.name} onChange={(event) => updateNodeDraft("name", event.target.value)} />
                          </label>
                          <label>
                            资产引用 Key
                            <input value={nodeDraft.dataKey} onChange={(event) => updateNodeDraft("dataKey", event.target.value)} />
                          </label>
                          <label>
                            输入角色
                            <input value={nodeDraft.role} onChange={(event) => updateNodeDraft("role", event.target.value)} />
                          </label>
                          <label>
                            输入
                            <input value={nodeDraft.input} onChange={(event) => updateNodeDraft("input", event.target.value)} />
                          </label>
                          <label>
                            输出资产 / Asset Key
                            <input value={nodeDraft.output} onChange={(event) => updateNodeDraft("output", event.target.value)} />
                          </label>
                          <label>
                            底层 Op
                            <input value={nodeDraft.dagsterOp} onChange={(event) => updateNodeDraft("dagsterOp", event.target.value)} />
                          </label>
                          <label>
                            Asset Key
                            <input value={nodeDraft.dagsterAsset} onChange={(event) => updateNodeDraft("dagsterAsset", event.target.value)} />
                          </label>
                          <label>
                            IO Manager
                            <input value={nodeDraft.ioManager} onChange={(event) => updateNodeDraft("ioManager", event.target.value)} />
                          </label>
                        </div>
                        <div className="node-config-preview">
                          <span>生成后写入</span>
                          <b>{nodeWriteModeForTemplate(selectedTemplate)}</b>
                          <small>只影响当前任务版本，不修改全局数据源。</small>
                        </div>
                        <div className="node-config-actions">
                          <button onClick={() => setNodeDraft(defaultDraftForTemplate(selectedTemplate, activeIntent))}>重置</button>
                          <button onClick={addConfiguredNode}>添加到画布</button>
                        </div>
                      </section>
                    </div>
                  </div>
                )}
    </>
  );
}
