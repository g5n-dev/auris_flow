export type SimpleLabelFilter = "全部" | "实体" | "判定" | "组合" | "推荐";

export type SimpleLabelHit = {
  turnIndex: number;
  evidence: string;
  reason: string;
  relation: string;
  confidence: number;
  action: string;
};

export type SpeakerChannel = "L" | "R" | "LR";
