import type { EvaluationCapabilityKey } from "../../../shared/contracts/evaluation";
import type evaluationFixtureSchema from "./data/evaluation-fixtures.json";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";

const evaluationFixture = await loadJsonFixture<typeof evaluationFixtureSchema>(
  new URL("./data/evaluation-fixtures.json", import.meta.url),
  "评测 fixture"
);


export const evaluationBadcaseRows: Array<{
  id: string;
  capability: EvaluationCapabilityKey;
  title: string;
  severity: string;
  source: string;
  reason: string;
  fix: string;
  assetKey: string;
  status: string;
}> = (evaluationFixture.legacyBadcases.evaluationBadcaseRows as unknown as { id: string; capability: EvaluationCapabilityKey; title: string; severity: string; source: string; reason: string; fix: string; assetKey: string; status: string; }[]);
