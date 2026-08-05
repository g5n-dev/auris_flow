import { apiRequest } from "./client";
import type {
  ApiCollection,
  ApiEnvelope,
  BackendAffectedObjectRef,
  HumanReviewTask
} from "./client";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

export type HumanReviewAffectedObjectReadback = {
  type: string;
  id: string;
  review_decision_id: string;
  root_trace_id: string;
  resource_version?: number;
  resource: Record<string, unknown>;
};

const affectedObjectReadbackPath = (affectedObject: BackendAffectedObjectRef) => {
  const path = affectedObject.readback_url;
  if (
    typeof path !== "string" ||
    !path.startsWith("/api/v1/human-review-decisions/") ||
    path.includes("?") ||
    path.includes("#")
  ) {
    throw new Error(
      `${affectedObject.type} ${affectedObject.id} 缺少安全的服务端 readback_url`
    );
  }
  return path.slice("/api".length);
};

export async function listPendingHumanReviewTasks(
  queue?: string,
  options: {
    limit?: number;
    cursor?: string;
  } = {}
): Promise<ApiEnvelope<ApiCollection<HumanReviewTask>>> {
  const query = new URLSearchParams();
  query.set("status", "pending");
  if (queue) query.set("queue", queue);
  query.set("limit", String(options.limit ?? 50));
  if (options.cursor) query.set("cursor", options.cursor);
  return apiRequest<ApiCollection<HumanReviewTask>>(
    `/v1/human-review-tasks?${query.toString()}`
  );
}

export async function getEvidencePack(
  evidencePackId: string
): Promise<ApiEnvelope<Record<string, unknown>>> {
  return apiRequest<Record<string, unknown>>(
    `/v1/evidence-packs/${encodeURIComponent(evidencePackId)}`
  );
}

export async function readHumanReviewAffectedObjects(
  affectedObjects: BackendAffectedObjectRef[]
): Promise<Record<string, HumanReviewAffectedObjectReadback>> {
  if (affectedObjects.length === 0) {
    throw new Error("后端复核回执缺少 affected_objects，已停止推进当前样本。");
  }
  const keys = affectedObjects.map(
    (affectedObject) => `${affectedObject.type}:${affectedObject.id}`
  );
  if (new Set(keys).size !== keys.length) {
    throw new Error("后端复核回执包含重复 affected_objects，已停止推进当前样本。");
  }
  const entries = await Promise.all(
    affectedObjects.map(async (affectedObject) => {
      const response = await apiRequest<HumanReviewAffectedObjectReadback>(
        affectedObjectReadbackPath(affectedObject)
      );
      if (!isRecord(response.data.resource)) {
        throw new Error(
          `${affectedObject.type} ${affectedObject.id} 回读缺少权威 resource`
        );
      }
      return [
        `${affectedObject.type}:${affectedObject.id}`,
        response.data
      ] as const;
    })
  );
  return Object.fromEntries(entries);
}
