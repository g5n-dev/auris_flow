import type { InsightsModuleProps } from "../types";
import type { HotwordInsightsState } from "./useHotwordInsights";
import type { ModuleKey } from "../../../shared/contracts/navigation";
import { eventLinks } from "../../../shared/fixtures/eventLinks";
import { buildSimpleLabelHits, simpleLabelDomains, simpleTurns } from "../../../shared/fixtures/listeningSamples";
import { receptionOrderCandidates } from "../../../shared/fixtures/receptionOrders";
import { evaluationCapabilityRows } from "../catalog";
import type { InsightDataset, InsightFact } from "../types";
import { useMemo } from "react";

export function useInsightDataset(scope: InsightsModuleProps & HotwordInsightsState) {
  const { topbarContext } = scope;
  const labelCountLookup = useMemo(() => {
      const lookup = new Map<string, number | string>();
      simpleLabelDomains.forEach((domain) => domain.items.forEach((item) => lookup.set(item.name, item.count)));
      return lookup;
    }, []);

  const labelHitCount = (label: string) => {
      const explicitHits = buildSimpleLabelHits(label, {}).length;
      const libraryCount = labelCountLookup.get(label);
      return explicitHits || (typeof libraryCount === "number" ? libraryCount : 0);
    };

  const pct = (numerator: number, denominator: number) => (denominator > 0 ? Math.round((numerator / denominator) * 1000) / 10 : 0);

  const compactNumber = (value: number) => value >= 1000 ? value.toLocaleString("zh-CN") : String(value);

  const unique = <T,>(items: T[]) => Array.from(new Set(items));

  const clampScore = (value: number, min = 0, max = 100) => Math.max(min, Math.min(max, Math.round(value * 10) / 10));

  const formatPercent = (value: number) => `${clampScore(value)}%`;

  const dataset = useMemo<InsightDataset>(() => {
      const context = {
        tenant: topbarContext.tenant,
        project: topbarContext.project,
        store: topbarContext.store,
        date: topbarContext.date,
        model: topbarContext.model,
        label: topbarContext.label
      };
      const enrichTags = (turn: (typeof simpleTurns)[number], event?: (typeof eventLinks)[number]) => {
        const partText = turn.parts.map(([text]) => text).join("");
        const tags = [...turn.tags];
        if (/万|报价|落地|指导价/.test(partText) || turn.doc.includes("报价单")) tags.push("报价金额");
        if (turn.tags.some((tag) => tag.includes("优惠"))) tags.push("优惠幅度");
        if (turn.tags.some((tag) => tag.includes("价格异议")) || turn.intent.includes("价格压低")) tags.push("客户价格异议");
        if (turn.intent.includes("承接") || turn.tags.includes("试驾引导")) tags.push("价格异议已承接");
        if (turn.tags.includes("试驾时间") || turn.tags.includes("试驾引导")) tags.push("试驾时间");
        if (turn.tags.includes("成交意向")) tags.push("成交意向");
        if (turn.source.includes("串音") || turn.confidence < 0.7 || event?.state.includes("串音")) tags.push("串音疑似");
        if (turn.confidence < 0.7 || turn.tags.some((tag) => tag.includes("低置信"))) tags.push("低置信片段");
        if (turn.doc.includes("单")) tags.push("业务单据");
        if (event?.state.includes("金额冲突")) tags.push("报价金额冲突");
        return unique(tags);
      };
      const baseFacts: InsightFact[] = simpleTurns.map((turn, index) => {
        const event = eventLinks[index];
        const tags = enrichTags(turn, event);
        const route: ModuleKey = tags.includes("串音疑似") || tags.includes("低置信片段") ? "listening" : tags.includes("业务单据") ? "data" : "labels";
        return {
          id: `fact-turn-${index + 1}`,
          tenant: context.tenant,
          project: context.project,
          store: index === 1 ? "北京 SKP 店" : context.store,
          date: context.date,
          time: turn.time,
          person: turn.speaker === "销售A" ? "销售A / 陈先生" : turn.speaker === "客户" ? "客户 / 陈先生" : turn.speaker,
          customer: turn.speaker === "客户" ? "陈先生" : "陈先生接待",
          eventType: event?.type ?? turn.intent,
          tags,
          audio: turn.device,
          durationSec: Number.parseFloat(turn.dur) || 0,
          confidence: turn.confidence,
          status: event?.state ?? turn.nextAction,
          doc: turn.doc,
          source: turn.source,
          assetKey: tags.includes("报价金额冲突") ? "auris/label/event_tags" : tags.includes("低置信片段") ? "auris/model/asr_transcripts" : "auris/audio/voice_segments",
          partitionKey: `${context.date}|bj-center|${turn.device.split(" ")[0]}`,
          modelVersion: context.model,
          labelVersion: index === 3 ? "v1.9.0-rc2" : "v1.8.4",
          route,
          amountConflict: tags.includes("报价金额冲突") || event?.state.includes("金额冲突") === true,
          crosstalk: tags.includes("串音疑似"),
          lowConfidence: turn.confidence < 0.7,
          evidenceRefs: [turn.eventId, turn.doc, event?.id ?? "ASR-turn"].filter(Boolean)
        };
      });
      const relationFacts: InsightFact[] = receptionOrderCandidates.map((candidate, index) => {
        const isCross = candidate.status === "改绑候选";
        const isMissing = candidate.status === "补单草稿";
        return {
          id: `fact-reception-${index + 1}`,
          tenant: context.tenant,
          project: context.project,
          store: candidate.store,
          date: context.date,
          time: candidate.window.split(" - ")[0],
          person: candidate.employee,
          customer: candidate.customer,
          eventType: isCross ? "串音候选事件" : isMissing ? "低置信补单事件" : "接待关联事件",
          tags: unique([
            "业务单据",
            candidate.risk.includes("金额") ? "报价金额冲突" : "试驾预约承接",
            isCross ? "串音疑似" : "",
            isMissing ? "低置信片段" : "",
            "接待关联"
          ].filter(Boolean)),
          audio: candidate.sampleIds.join(" / "),
          durationSec: 96 + index * 38,
          confidence: candidate.match / 100,
          status: candidate.status,
          doc: candidate.docs[0],
          source: candidate.source,
          assetKey: candidate.asset,
          partitionKey: `${context.date}|${candidate.store}|${candidate.employee.split(" / ")[0]}`,
          modelVersion: context.model,
          labelVersion: context.label,
          route: isCross ? "listening" : isMissing ? "assets" : "data",
          amountConflict: candidate.risk.includes("金额"),
          crosstalk: isCross,
          lowConfidence: isMissing,
          evidenceRefs: [candidate.orderNo, candidate.writeRef, candidate.asset]
        };
      });
      const facts = [...baseFacts, ...relationFacts];
      const tagMap = new Map<string, { count: number; confidence: number; assetKey: string }>();
      facts.forEach((fact) => {
        fact.tags.forEach((tag) => {
          const current = tagMap.get(tag) ?? { count: 0, confidence: 0, assetKey: fact.assetKey };
          current.count += 1;
          current.confidence += fact.confidence;
          current.assetKey = current.assetKey || fact.assetKey;
          tagMap.set(tag, current);
        });
      });
      const tagCounts = Array.from(tagMap.entries())
        .map(([label, row]) => ({
          label,
          count: Math.max(row.count, labelHitCount(label)),
          confidence: Math.round((row.confidence / Math.max(row.count, 1)) * 100),
          assetKey: row.assetKey
        }))
        .sort((a, b) => b.count - a.count);
      const stores = unique(facts.map((fact) => fact.store));
      const storeRows = stores.map((store) => {
        const rows = facts.filter((fact) => fact.store === store);
        return {
          store,
          total: rows.length,
          risk: rows.filter((fact) => fact.amountConflict || fact.crosstalk || fact.lowConfidence).length,
          quote: rows.filter((fact) => fact.tags.includes("报价金额") || fact.tags.includes("报价金额冲突")).length,
          testDrive: rows.filter((fact) => fact.tags.includes("试驾时间") || fact.tags.includes("试驾预约承接")).length,
          confidence: Math.round(pct(rows.reduce((sum, fact) => sum + fact.confidence, 0), rows.length))
        };
      });
      const people = unique(facts.map((fact) => fact.person));
      const salesRows = people.map((person) => {
        const rows = facts.filter((fact) => fact.person === person);
        return {
          person,
          total: rows.length,
          quote: rows.filter((fact) => fact.tags.includes("报价金额") || fact.tags.includes("报价金额冲突")).length,
          resolved: rows.filter((fact) => fact.tags.includes("价格异议已承接") || fact.tags.includes("试驾预约承接")).length,
          risk: rows.filter((fact) => fact.amountConflict || fact.crosstalk || fact.lowConfidence).length,
          confidence: Math.round(pct(rows.reduce((sum, fact) => sum + fact.confidence, 0), rows.length))
        };
      });
      const qualityRows = evaluationCapabilityRows.map((row) => ({
        ability: row.ability,
        baseline: Number(row.baseline),
        candidate: Number(row.candidate),
        delta: Number(row.delta),
        blocker: row.blocker,
        samples: row.samples,
        route: row.blocker === "观察" ? "evaluation" as ModuleKey : "assets" as ModuleKey
      }));
      return {
        context,
        facts,
        tagCounts,
        storeRows,
        salesRows,
        qualityRows,
        trendDates: ["5/20", "5/21", "5/22", "5/23", "5/24", "5/25", "5/26"]
      };
    }, [labelCountLookup, topbarContext]);

  const quoteFacts = dataset.facts.filter((fact) => fact.tags.includes("报价金额") || fact.tags.includes("报价金额冲突"));

  const objectionFacts = dataset.facts.filter((fact) => fact.tags.includes("客户价格异议"));

  const resolvedFacts = dataset.facts.filter((fact) => fact.tags.includes("价格异议已承接") || fact.tags.includes("试驾预约承接"));

  const driveFacts = dataset.facts.filter((fact) => fact.tags.includes("试驾时间") || fact.tags.includes("试驾预约承接"));

  const riskFacts = dataset.facts.filter((fact) => fact.amountConflict || fact.crosstalk || fact.lowConfidence);

  const evidenceComplete = dataset.facts.filter((fact) => fact.assetKey && fact.doc && fact.evidenceRefs.length > 1);

  const quoteConsistency = 100 - pct(quoteFacts.filter((fact) => fact.amountConflict).length, Math.max(quoteFacts.length, 1));

  const objectionResolution = pct(resolvedFacts.length, Math.max(objectionFacts.length + resolvedFacts.length, 1));

  const testDriveIntent = pct(driveFacts.length, Math.max(dataset.facts.length, 1));

  const crosstalkRisk = pct(dataset.facts.filter((fact) => fact.crosstalk).length, Math.max(quoteFacts.length, 1));

  const tagAssetQuality = pct(evidenceComplete.length, Math.max(dataset.facts.length, 1));

  const modelScore = Math.round(dataset.qualityRows.reduce((sum, row) => sum + row.candidate, 0) / Math.max(dataset.qualityRows.length, 1) * 10) / 10;

  const validReceptionFacts = dataset.facts.filter((fact) => fact.durationSec >= 30 && fact.confidence >= 0.68 && !fact.lowConfidence);

  const effectiveReceptionRate = pct(validReceptionFacts.length, Math.max(dataset.facts.length, 1));

  const conversionProgress = pct(driveFacts.length + resolvedFacts.length, Math.max(dataset.facts.length, 1));

  const riskReverseScore = 100 - pct(riskFacts.length, Math.max(dataset.facts.length, 1));

  return {
    labelCountLookup,
    labelHitCount,
    pct,
    compactNumber,
    unique,
    clampScore,
    formatPercent,
    dataset,
    quoteFacts,
    objectionFacts,
    resolvedFacts,
    driveFacts,
    riskFacts,
    evidenceComplete,
    quoteConsistency,
    objectionResolution,
    testDriveIntent,
    crosstalkRisk,
    tagAssetQuality,
    modelScore,
    validReceptionFacts,
    effectiveReceptionRate,
    conversionProgress,
    riskReverseScore
  };
}

export type InsightDatasetState = ReturnType<typeof useInsightDataset>;
