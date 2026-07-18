import type { SimpleLabelFilter, SpeakerChannel } from "../../../shared/contracts/simpleConversation";
import { simpleLabelHitOverrides, simpleTurns } from "../../../shared/fixtures/listeningSamples";
import type listeningFixtureSchema from "./data/listening-fixtures.json";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";

const listeningFixture = await loadJsonFixture<typeof listeningFixtureSchema>(
  new URL("./data/listening-fixtures.json", import.meta.url),
  "调听 fixture"
);


export const simpleLabelTabs: SimpleLabelFilter[] = ["全部", "实体", "判定", "组合", "推荐"];

export const simpleAiRecommendedLabels = ["确认试驾时间", "价格异议缓解", "置换政策提示"];

export type SimpleLabelDetail = {
  family: string;
  layer: string;
  status: string;
  owner: string;
  description: string;
  confidence: string;
  examples: string[];
  fields: Array<[string, string]>;
  dependencies: string[];
  downstream: string[];
  action: string;
};

export type SimpleLabelReviewState = "pending" | "accepted" | "review";

export const simpleLabelDetails: Record<string, SimpleLabelDetail> = (listeningFixture.simpleReviewFixtures.simpleLabelDetails as unknown as Record<string, SimpleLabelDetail>);

export function getSimpleLabelDetail(label: string): SimpleLabelDetail {
  return (
    simpleLabelDetails[label] ?? {
      family: "TAG",
      layer: "筛选标签",
      status: "待复核",
      owner: "标签运营组",
      description: `围绕「${label}」筛选当前会话命中片段，支持逐条确认、批量接受和转入证据审查。`,
      confidence: "按命中",
      examples: simpleLabelHitOverrides[label]?.map((item) => item.evidence).slice(0, 3) ?? ["暂无样例"],
      fields: [
        ["filter_label", label],
        ["scope", "conversation"],
        ["review_mode", "human_loop"],
        ["write_policy", "segment_tag"]
      ],
      dependencies: ["当前片段", "标签版本", "人工复核"],
      downstream: ["标签资产", "质检样本", "模型评测"],
      action: "建议从命中列表逐条确认，再批量写入标签版本。"
    }
  );
}

export const tagSuggestions = listeningFixture.simpleReviewFixtures.tagSuggestions;

export const simpleSpeakerRoles = listeningFixture.simpleReviewFixtures.simpleSpeakerRoles;

export type SimpleSpeakerRole = (typeof simpleSpeakerRoles)[number];

export type SimpleSpeakerAnnotation = {
  key: SimpleSpeakerRole["key"];
  speaker: string;
  role: string;
  short: string;
  channel: SpeakerChannel;
  source: string;
  confidence: number;
};

export function inferSimpleSpeaker(turn: (typeof simpleTurns)[number]): SimpleSpeakerAnnotation {
  const role = turn.role === "customer" ? simpleSpeakerRoles[2] : simpleSpeakerRoles[1];
  return {
    key: role.key,
    speaker: turn.speaker,
    role: role.role,
    short: role.short,
    channel: role.defaultChannel as SpeakerChannel,
    source: turn.device,
    confidence: turn.confidence
  }
}
