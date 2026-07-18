import {
  Database,
  FileText,
  GitBranch,
  Headphones,
  Tags,
  UserCheck
} from "lucide-react";
import type { ComponentType } from "react";

import type { DataAssetItem } from "../../shared/contracts/dataAssets";

export function deriveDataRelations(
  selectedAsset: DataAssetItem,
  selectedAssetCatalogName: string,
  openListeningFromDataAsset: (asset: DataAssetItem) => void,
  openAssetsFromDataAsset: (asset: DataAssetItem) => void
) {
  const relationStatusLabel = selectedAsset.status === "confirmed" ? "已对齐" : selectedAsset.status === "pending" ? "待确认" : "需修复";
  const relationNodeItems: Array<{
    key: string;
    label: string;
    value: string;
    meta: string;
    tone: string;
    icon: ComponentType<{ size?: number }>;
    action?: () => void;
  }> = [
    {
      key: "audio",
      label: "音频片段",
      value: selectedAsset.audio,
      meta: `${selectedAsset.duration} · ${selectedAsset.partitionKey}`,
      tone: "audio",
      icon: Headphones,
      action: () => openListeningFromDataAsset(selectedAsset)
    },
    {
      key: "person",
      label: "人物主体",
      value: selectedAsset.person,
      meta: "员工 / 客户组 / 声纹",
      tone: "person",
      icon: UserCheck
    },
    {
      key: "event",
      label: "业务事件",
      value: selectedAsset.event,
      meta: selectedAsset.tags.join(" / "),
      tone: selectedAsset.status === "risk" ? "risk" : "event",
      icon: Tags
    },
    {
      key: "docs",
      label: "事件单据",
      value: selectedAsset.docs.join("、") || "未关联单据",
      meta: `${selectedAsset.docs.length} 个业务凭证参与校验`,
      tone: selectedAsset.docs.length > 0 ? "doc" : "risk",
      icon: FileText
    },
    {
      key: "asset",
      label: "数据资产",
      value: selectedAssetCatalogName,
      meta: selectedAsset.assetKey,
      tone: "asset",
      icon: Database,
      action: () => openAssetsFromDataAsset(selectedAsset)
    },
    {
      key: "downstream",
      label: "下游消费",
      value: `${selectedAsset.downstreamAssets.length} 个下游`,
      meta: selectedAsset.downstreamAssets.join(" / "),
      tone: "downstream",
      icon: GitBranch
    }
  ];
  const relationEdgeRows: Array<[string, string, string, "ok" | "warn" | "risk"]> = [
    ["音频片段", "业务事件", selectedAsset.status === "risk" ? "低置信待确认" : "时间窗与 ASR 已对齐", selectedAsset.status === "risk" ? "risk" : "ok"],
    ["人物主体", "业务事件", selectedAsset.person.includes("未知") ? "身份未绑定" : "员工主体已绑定", selectedAsset.person.includes("未知") ? "risk" : "ok"],
    ["事件单据", "业务事件", selectedAsset.docs.length > 1 ? "多单据参与字段校验" : "单据字段参与校验", selectedAsset.docs.length ? "ok" : "warn"],
    ["业务事件", "数据资产", selectedAsset.assetCheck, selectedAsset.status === "confirmed" ? "ok" : selectedAsset.status === "pending" ? "warn" : "risk"],
    ["数据资产", "下游消费", selectedAsset.downstreamAssets.join("、"), selectedAsset.status === "risk" ? "warn" : "ok"]
  ];
  return {
    relationEdgeRows,
    relationNodeItems,
    relationStatusLabel
  };
}
