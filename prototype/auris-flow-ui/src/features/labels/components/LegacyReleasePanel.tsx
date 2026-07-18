import type { LabelsController } from "../controller/useLabelsController";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { AlertTriangle, ShieldCheck } from "lucide-react";

export function LegacyReleasePanel({ controller }: { controller: LabelsController }) {
  const { activeIntent, handleIntentAction, renderReleaseGateEditor, setActiveModule } = controller;
  return (
    <section className="module-panel label-release-card">
            <PanelHeader title="发布与资产影响" subtitle="发布前必须看到阻断、影响资产和回滚路径" icon={<ShieldCheck size={16} />} />
            <>
              {renderReleaseGateEditor()}
              <div className="label-impact-list">
                {["事件标签资产", "评测样本资产", "业务洞察趋势", "badcase 训练集"].map((asset, index) => (
                  <button key={asset} type="button" onClick={() => setActiveModule(index === 1 ? "evaluation" : "assets")}>
                    <span>{asset}</span>
                    <b>{index === 0 ? "12 下游" : index === 1 ? "64 样本" : index === 2 ? "3 指标" : "18 条"}</b>
                  </button>
                ))}
              </div>
              <div className="label-release-blockers">
                <span>当前意图阻断</span>
                {activeIntent.blockers.map((blocker) => (
                  <button key={blocker} type="button" onClick={() => handleIntentAction(`已定位发布阻断：${blocker}`)}>
                    <AlertTriangle size={13} />
                    {blocker}
                  </button>
                ))}
              </div>
            </>
          </section>
  );
}
