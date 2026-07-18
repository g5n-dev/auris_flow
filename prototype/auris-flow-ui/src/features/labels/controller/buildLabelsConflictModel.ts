import type { LabelsModuleProps } from "../types";
import type { LabelsCoreState } from "./useLabelsCoreState";
import type { LabelsReleaseState } from "./useLabelsReleaseState";
import type { LabelsCandidateModel } from "./buildLabelsCandidateModel";
import type { LabelsFocusModel } from "./useLabelsFocus";
import type { LabelsChangeModel } from "./buildLabelsChangeModel";
import type { LabelsGovernanceModel } from "./buildLabelsGovernanceModel";

type BuildLabelsConflictModelScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel & LabelsGovernanceModel;

export function buildLabelsConflictModel(activeIntent: BuildLabelsConflictModelScope["activeIntent"], navigateToTarget: BuildLabelsConflictModelScope["navigateToTarget"], selectedConflictKey: BuildLabelsConflictModelScope["selectedConflictKey"]) {
  const conflictCases =
      activeIntent.conflicts.length > 0
        ? activeIntent.conflicts.map((conflict, index) => ({
            ...conflict,
            key: `conflict-${index}`,
            owner: index === 0 ? activeIntent.owner : "Version Diff Agent",
            current: index === 0 ? "v1.8.4 线上结果保留" : "v1.8.4 已命中",
            candidate: index === 0 ? "v1.9.0-rc2 写候选，等待人审" : "v1.9.0-rc2 覆盖不足",
            evidence: activeIntent.evidence,
            asset: index === 0 ? "auris/label/event_tags" : "auris/eval/quality_metrics",
            blocker: activeIntent.blockers[index] ?? activeIntent.blockers[0] ?? "发布前抽检"
          }))
        : [
            {
              key: "conflict-none",
              label: `${activeIntent.intent} 低风险抽检`,
              source: "发布门禁",
              detail: "无强冲突，仍保留回填前抽检和灰度观察。",
              severity: "低" as const,
              owner: activeIntent.owner,
              current: "v1.8.4 无阻断",
              candidate: "v1.9.0-rc2 可进入影子评测",
              evidence: activeIntent.evidence,
              asset: "auris/label/event_tags",
              blocker: activeIntent.blockers[0] ?? "无发布阻断"
            }
          ];

  const activeConflict = conflictCases.find((item) => item.key === selectedConflictKey) ?? conflictCases[0];

  const conflictImpactRows = [
      ["证据片段", activeIntent.scope, "进入调听工作台核对音频、ASR 和单据"],
      ["标签版本", "v1.9.0-rc2 候选", "只写候选版本，不覆盖线上 v1.8.4"],
      ["下游资产", activeConflict.asset, "影响评测、业务洞察和复核队列"],
      ["发布门禁", activeConflict.blocker, "未仲裁前阻断自动发布"]
    ];

  const openLabelAsset = (assetKey = activeConflict.asset, title = "标签影响资产") => {
      navigateToTarget({
        module: "assets",
        tab: "lineage",
        objectKind: "asset",
        objectId: assetKey,
        focusMode: "lineage",
        title,
        detail: `${activeIntent.intent} / ${activeConflict.label}`,
        origin: { label: "标签治理 / 资产血缘", module: "labels", objectLabel: activeConflict.key }
      });
    };

  const openLabelIntentDetail = (title = activeIntent.intent) => {
      navigateToTarget({
        module: "labels",
        tab: "schema",
        objectKind: "labelIntent",
        objectId: activeIntent.key,
        title,
        detail: `${activeIntent.scene} / ${activeIntent.status}`,
        origin: { label: "标签治理 / 版本关联", module: "labels", objectLabel: activeConflict.key }
      });
    };

  return {
    conflictCases,
    activeConflict,
    conflictImpactRows,
    openLabelAsset,
    openLabelIntentDetail
  };
}

export type LabelsConflictModel = ReturnType<typeof buildLabelsConflictModel>;
