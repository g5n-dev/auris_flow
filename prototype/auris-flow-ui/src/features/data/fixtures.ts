import { Activity, Database, Tags, UserCheck } from "lucide-react";
import type { ComponentType } from "react";

import type { DataAggregateKey } from "./types";

export { dataDagsterContracts, dataTree, heatRows } from "./fixtures/runtimeData";

export const aggregateMeta: Record<DataAggregateKey, { label: string; hint: string; icon: ComponentType<{ size?: number }> }> = {
    space: { label: "空间", hint: "区域 / 门店 / 设备", icon: Database },
    time: { label: "时间", hint: "日期 / 小时 / 接待窗", icon: Activity },
    event: { label: "事件", hint: "接待 / 报价 / 单据", icon: Tags },
    person: { label: "人物", hint: "销售 / 客户组 / 声纹", icon: UserCheck }
  };
