import staticCatalog from "../../modules/staticCatalog";
import type { HotwordStatistics } from "../../api/client";
import type { EvaluationCapabilityRow } from "../../shared/contracts/evaluation";

const insightsCatalog = staticCatalog as {
  evaluationCatalog: {
    capabilityRows: EvaluationCapabilityRow[];
  };
  runtimeCatalog: {
    hotwordCatalog: {
      metrics: Array<{
        label: string;
        key: keyof HotwordStatistics["metrics"];
        suffix: string;
        detail: string;
      }>;
      providers: string[];
      models: string[];
      versions: Array<{ id: string; label: string }>;
      threshold: string;
    };
  };
};

export const evaluationCapabilityRows = insightsCatalog.evaluationCatalog.capabilityRows;
export const hotwordCatalog = insightsCatalog.runtimeCatalog.hotwordCatalog;
