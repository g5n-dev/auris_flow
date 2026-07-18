import type { CanvasController } from "../../controller/useCanvasController";
import { executionStateMeta } from "../../catalog";
import { canvasStageBounds } from "../../nodeTemplates";
import { ConnectorNode } from ".././ConnectorNode";
import { GitBranch, Link2, Sparkles } from "lucide-react";

export function NodeFlowCanvas({ controller }: { controller: CanvasController }) {
  const { activeIntent, activeIntentKey, addedNodes, appliedMappingCount, dragPropsFor, dragState, edges, executionState, linePath, mappingTotal, positionById, positionFor, processNodes, selectNode, selectedNodeId, sourceNodes, specialNodes, visibleNodeIds } = controller;
  return (
    <>
      <>
                    <svg
                      className="connector-lines"
                      viewBox={`0 0 ${canvasStageBounds.width} ${canvasStageBounds.height}`}
                      style={{ width: canvasStageBounds.width, height: canvasStageBounds.height }}
                      preserveAspectRatio="none"
                      aria-hidden="true"
                    >
                      {edges.filter((edge) => edge.active && visibleNodeIds.has(edge.from) && visibleNodeIds.has(edge.to)).map((edge) => (
                        <path key={`${edge.from}-${edge.to}`} className={edge.active ? "active" : ""} d={linePath(edge.from, edge.to)} />
                      ))}
                    </svg>

                    {sourceNodes.filter((node) => visibleNodeIds.has(node.id)).map((node) => (
                      <ConnectorNode
                        key={node.id}
                        node={node}
                        selected={selectedNodeId === node.id}
                        activeIntentKey={activeIntentKey}
                        position={positionFor(node)}
                        dragging={dragState?.id === node.id}
                        dragHandlers={dragPropsFor(node)}
                        onSelect={() => selectNode(node.id)}
                      />
                    ))}

                    {visibleNodeIds.has("ai") && (
                      <button
                        className={[
                          "mapping-agent-node",
                          "canvas-draggable",
                          selectedNodeId === "ai" ? "selected" : "",
                          dragState?.id === "ai" ? "dragging" : ""
                        ].join(" ")}
                        style={{ left: positionById("ai").x, top: positionById("ai").y }}
                        {...dragPropsFor(specialNodes[0])}
                        onClick={() => selectNode("ai")}
                      >
                        <span>
                          <Sparkles size={28} />
                        </span>
                        <strong>AI 映射助手</strong>
                        <em>{appliedMappingCount}/{mappingTotal} 已应用</em>
                      </button>
                    )}

                    {visibleNodeIds.has("entityMap") && (
                      <button
                        className={[
                          "entity-map-node",
                          "canvas-draggable",
                          selectedNodeId === "entityMap" ? "selected" : "",
                          dragState?.id === "entityMap" ? "dragging" : ""
                        ].join(" ")}
                        style={{ left: positionById("entityMap").x, top: positionById("entityMap").y }}
                        {...dragPropsFor(specialNodes[1])}
                        onClick={() => selectNode("entityMap")}
                      >
                        <span>
                          <Link2 size={23} />
                          平台主体映射
                        </span>
                        <b>{activeIntentKey === "review" ? "关联认证事件" : "已映射主体"}</b>
                        <strong>{activeIntentKey === "review" ? "24" : "3,142"}</strong>
                        <i />
                      </button>
                    )}

                    {visibleNodeIds.has("dagster") && (
                      <button
                        className={[
                          "dagster-node",
                          "canvas-draggable",
                          executionState,
                          selectedNodeId === "dagster" ? "selected" : "",
                          dragState?.id === "dagster" ? "dragging" : ""
                        ].join(" ")}
                        style={{ left: positionById("dagster").x, top: positionById("dagster").y }}
                        {...dragPropsFor(specialNodes[2])}
                        onClick={() => selectNode("dagster")}
                      >
                        <span>
                          <GitBranch size={18} />
                          执行计划
                        </span>
                        <strong>{activeIntent.taskId}</strong>
                        <em>{executionStateMeta[executionState].detail}</em>
                      </button>
                    )}

                    {visibleNodeIds.has("ai") && (
                      <div className="agent-bubble" style={{ left: 760, top: 116 }}>
                        <Sparkles size={15} />
                        <p>
                          <b>配置建议:</b> {activeIntent.checks[0]}，建议加入发布前校验。
                        </p>
                      </div>
                    )}

                    {processNodes.filter((node) => visibleNodeIds.has(node.id)).map((node) => (
                      <ConnectorNode
                        key={node.id}
                        node={node}
                        selected={selectedNodeId === node.id}
                        activeIntentKey={activeIntentKey}
                        position={positionFor(node)}
                        dragging={dragState?.id === node.id}
                        dragHandlers={dragPropsFor(node)}
                        onSelect={() => selectNode(node.id)}
                      />
                    ))}

                    {addedNodes.filter((node) => visibleNodeIds.has(node.id)).map((node) => (
                      <ConnectorNode
                        key={node.id}
                        node={node}
                        selected={selectedNodeId === node.id}
                        activeIntentKey={activeIntentKey}
                        position={positionFor(node)}
                        dragging={dragState?.id === node.id}
                        dragHandlers={dragPropsFor(node)}
                        onSelect={() => selectNode(node.id)}
                      />
                    ))}
                    </>
    </>
  );
}
