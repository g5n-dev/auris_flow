export type LabelTrackKey = "vad" | "speaker" | "asr" | "entity" | "intent" | "qa" | "doc" | "cross" | "agent";

export const layerLevelConfigs = [
  {
    key: "entity",
    level: 4,
    label: "L4 实体标签",
    category: "汽车销售实体",
    color: "entity",
    types: ["时间区段", "事件点"],
    tags: ["车型", "指导价", "优惠金额", "落地价", "试驾时间", "客户姓名"]
  },
  {
    key: "intent",
    level: 5,
    label: "L5 意图标签",
    category: "对话意图",
    color: "intent",
    types: ["时间区段"],
    tags: ["报价承诺", "价格异议", "试驾承接", "预约确认", "成交意向", "售后跟进"]
  },
  {
    key: "qa",
    level: 6,
    label: "L6 质检标签",
    category: "复核判定",
    color: "qa",
    types: ["时间区段", "事件点"],
    tags: ["金额冲突", "低置信", "串音待排除", "重复收录", "合规风险", "可回填"]
  },
  {
    key: "doc",
    level: 7,
    label: "L7 单据事件",
    category: "业务单据",
    color: "doc",
    types: ["事件点", "时间区段"],
    tags: ["报价单创建", "报价单更新", "试驾单草稿", "试驾预约回填", "订单待生成", "客户画像更新"]
  },
  {
    key: "cross",
    level: 8,
    label: "L8 串音证据",
    category: "音频证据",
    color: "cross",
    types: ["时间区段"],
    tags: ["同峰串音", "Hall-Mic 串入", "邻近工牌", "主录音确认", "设备重叠"]
  },
  {
    key: "agent",
    level: 9,
    label: "L9 Agent动作",
    category: "自动化动作",
    color: "agent",
    types: ["事件点"],
    tags: ["核报价单", "转串音矩阵", "生成试驾待办", "回填单据", "转人工仲裁"]
  }
] satisfies Array<{
  key: LabelTrackKey;
  level: number;
  label: string;
  category: string;
  color: string;
  types: string[];
  tags: string[];
}>;
