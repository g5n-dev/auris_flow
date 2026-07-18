import type { CanvasController } from "../../controller/useCanvasController";
import { Check, Sparkles } from "lucide-react";

export function DrawerOverview({ controller }: { controller: CanvasController }) {
  const { drawerTab, selectedNode, selectedNodeContext } = controller;
  return (
    <>
      {drawerTab === "overview" && (
                  <>
                    <section className="intent-detail-card">
                      <div className="intent-detail-head">
                        <Sparkles size={16} />
                        <strong>{selectedNodeContext.type}</strong>
                        <b>{selectedNode.confidence}%</b>
                      </div>
                      <p>{selectedNodeContext.relation}</p>
                      <div className="node-tag-list">
                        {selectedNode.tags.map((tag) => (
                          <span key={tag}>{tag}</span>
                        ))}
                      </div>
                    </section>
                    <section className="intent-checks">
                      <span>影响范围</span>
                      {[selectedNodeContext.impact, selectedNodeContext.version, `也被 ${selectedNodeContext.usedBy.join("、")} 使用`].map((item) => (
                        <button key={item}>
                          <Check size={14} />
                          {item}
                        </button>
                      ))}
                    </section>
                  </>
                )}
    </>
  );
}
