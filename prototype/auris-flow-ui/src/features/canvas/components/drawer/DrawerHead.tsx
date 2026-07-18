import type { CanvasController } from "../../controller/useCanvasController";
import type { CanvasDrawerTab } from "../../types";
import { GitBranch } from "lucide-react";

export function DrawerHead({ controller }: { controller: CanvasController }) {
  const { drawerTab, selectedNode, selectedNodeContext, setDrawerTab, setSelectedNodeId } = controller;
  return (
    <>
      <div className="drawer-head">
                <div>
                  <span>{selectedNode.name}</span>
                  <strong>{selectedNodeContext.type} · {selectedNode.role}</strong>
                </div>
                <button onClick={() => setSelectedNodeId("dagster")} aria-label="查看执行计划">
                  <GitBranch size={16} />
                </button>
              </div>
      <div className="drawer-tabs">
                {[
                  ["overview", "节点概览"],
                  ["mapping", "映射/字段"],
                  ["plan", "执行计划"],
                  ["logs", "运行记录"]
                ].map(([key, label]) => (
                  <button key={key} className={drawerTab === key ? "active" : ""} onClick={() => setDrawerTab(key as CanvasDrawerTab)}>
                    {label}
                  </button>
                ))}
              </div>
    </>
  );
}
