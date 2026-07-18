import type { SimpleLabelFilter, SimpleLabelHit, SpeakerChannel } from "../contracts/simpleConversation";
import type sharedListeningFixtureSchema from "./data/listening-shared-fixtures.json";
import { loadJsonFixture } from "../runtime/jsonFixture";

const sharedListeningFixture = await loadJsonFixture<typeof sharedListeningFixtureSchema>(
  new URL("./data/listening-shared-fixtures.json", import.meta.url),
  "共享调听 fixture"
);


export const simpleLabelDomains = sharedListeningFixture.listeningSamples.simpleLabelDomains;

export const simpleTurns = sharedListeningFixture.listeningSamples.simpleTurns;

export const simpleLabelHitOverrides: Record<string, SimpleLabelHit[]> = (sharedListeningFixture.listeningSamples.simpleLabelHitOverrides as unknown as Record<string, SimpleLabelHit[]>);

export function buildSimpleLabelHits(label: string, turnTagEdits: Record<string, string[]>): SimpleLabelHit[] {
  const aliases: Record<string, string[]> = {
    客户价格异议: ["价格异议"],
    低置信片段: ["低置信复核"],
    确认试驾时间: ["试驾时间"],
    报价金额冲突: ["报价金额", "优惠幅度", "业务单据"],
    试驾预约承接: ["试驾时间", "成交意向", "业务单据"],
    串音污染报价: ["串音疑似", "低置信片段", "报价金额"],
    价格异议已承接: ["客户价格异议", "价格异议", "优惠幅度"]
  };
  const labels = [label, ...(aliases[label] ?? [])];
  const combined = new Map<number, SimpleLabelHit>();
  (simpleLabelHitOverrides[label] ?? []).forEach((hit) => combined.set(hit.turnIndex, hit));
  simpleTurns.forEach((turn, turnIndex) => {
    const tags = turnTagEdits[turn.eventId] ?? turn.tags;
    if (!labels.some((item) => tags.includes(item)) || combined.has(turnIndex)) return;
    combined.set(turnIndex, {
      turnIndex,
      evidence: tags.filter((tag) => labels.includes(tag)).join(" / ") || label,
      reason: "当前片段已有该标签命中",
      relation: turn.eventId,
      confidence: turn.confidence,
      action: "复核后保存标签"
    });
  });
  return Array.from(combined.values()).sort((a, b) => a.turnIndex - b.turnIndex);
}
