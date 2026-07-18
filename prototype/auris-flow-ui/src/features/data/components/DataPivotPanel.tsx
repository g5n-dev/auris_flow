import { Sparkles, X } from "lucide-react";

import { heatRows } from "../fixtures";
import type { DataWorkspace } from "../useDataWorkspace";

export function DataPivotPanel({ workspace }: { workspace: DataWorkspace }) {
  const { isPivotCollapsed } = workspace;
  return (
    <>
      {!isPivotCollapsed && (
        <section className="data-pivot-panel">
          <div className="dimension-bar">
            <div>
              <strong>维度查询:</strong>
              {["空间 Space", "时间 Time", "人物 Person", "事件 Event"].map((item) => (
                <button key={item}>
                  <span>{item}</span>
                  <X size={12} />
                </button>
              ))}
              <button className="add-dimension">
                <Sparkles size={13} />
                添加维度
              </button>
            </div>
            <div className="dimension-actions">
              <div className="heat-legend">
                <span>强度:</span>
                <i />
              </div>
            </div>
          </div>
          <div className="heatmap-table">
            <div className="heatmap-head">
              {["空间 \\ 情绪", "积极", "中性", "消极", "混合/不清晰"].map((head) => (
                <span key={head}>{head}</span>
              ))}
            </div>
            {heatRows.map((row, rowIndex) => (
              <div className="heatmap-row" key={row[0]}>
                <strong>{row[0]}</strong>
                {row.slice(1).map((value, index) => (
                  <button key={`${row[0]}-${index}`} className={`heat-cell c${index} r${rowIndex}`}>
                    {value}
                  </button>
                ))}
              </div>
            ))}
          </div>
        </section>
      )}
    </>
  );
}
