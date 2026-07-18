import type { LabelsController } from "../controller/useLabelsController";
import { layerLevelConfigs } from "../../../shared/fixtures/labelLayers";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { labelHierarchyBlueprint } from "../fixtures/governanceCatalog";
import { Layers } from "lucide-react";

export function LegacyTaxonomyPanel({ controller }: { controller: LabelsController }) {
  const { activeIntent, handleIntentAction } = controller;
  return (
    <section className="module-panel wide label-taxonomy-panel">
            <PanelHeader title="四层标签体系" subtitle="标签域 → 标签组 → 标签 → 标签值/动作；L1-L9 是证据轨道，不等于治理层级" icon={<Layers size={16} />} />
            <div className="label-hierarchy-blueprint">
              {labelHierarchyBlueprint.map((item) => (
                <button key={item.level} type="button" onClick={() => handleIntentAction(`${item.level}：${item.contract}`)}>
                  <span>{item.level}</span>
                  <strong>{item.name}</strong>
                  <em>{item.contract}</em>
                  <small>{item.example}</small>
                </button>
              ))}
            </div>
            <div className="label-taxonomy">
              {layerLevelConfigs.map((level) => {
                const layerMatch = activeIntent.layers[level.key];
                return (
                  <button
                    key={level.key}
                    type="button"
                    className={`${level.color} ${layerMatch ? "active-intent" : ""}`}
                    onClick={() =>
                      handleIntentAction(
                        layerMatch
                          ? `${activeIntent.intent} 当前使用 ${level.label}：${layerMatch.tag}。`
                          : `${level.label} 未参与当前意图，可从规则页补充触发条件。`
                      )
                    }
                  >
                    <span>L{level.level}</span>
                    <strong>{level.label.replace(/^L\d+\s*/, "")}</strong>
                    <em>{layerMatch?.tag ?? level.category}</em>
                    <small>{layerMatch?.evidence ?? level.tags.slice(0, 3).join(" / ")}</small>
                  </button>
                );
              })}
            </div>
          </section>
  );
}
