export type HumanReviewTargetType =
  | "label_candidate"
  | "label_aggregate"
  | "prompt_version_candidate"
  | "taxonomy_suggestion"
  | "event_link"
  | "evidence_pack"
  | "conversation_boundary"
  | "voiceprint_sample"
  | "work_item";

export type HumanReviewChange = {
  target_type: HumanReviewTargetType;
  target_id: string;
  fields: Record<string, unknown>;
};

export type HumanReviewDecisionRequest = {
  decision: "accepted" | "modified" | "rejected";
  note?: string;
  changes?: HumanReviewChange[];
};

type BuildHumanReviewDecisionInput = {
  agentState: "pending" | "accepted" | "rejected";
  evidencePackId: string;
  lowConfidence: boolean;
  markState: "main" | "crosstalk" | "duplicate" | "none";
  note?: string;
  stagedChanges: HumanReviewChange[];
};

type DecisionClosureInput = {
  decisionId: string;
  expectedRootTraceId: string;
  receiptRootTraceId: string;
  reviewTaskId: string;
  evidencePackId: string;
  request: HumanReviewDecisionRequest;
  taskReadback: Record<string, unknown>;
  evidencePackReadback: Record<string, unknown>;
  affectedObjects: Array<{
    type: string;
    id: string;
    readback_url?: string;
    resource_version?: number;
  }>;
  affectedReadbacks: Record<
    string,
    {
      type: string;
      id: string;
      review_decision_id: string;
      root_trace_id: string;
      resource_version?: number;
      resource: Record<string, unknown>;
    }
  >;
};

const recordId = (value: Record<string, unknown>, ...keys: string[]) => {
  for (const key of keys) {
    const candidate = value[key];
    if (typeof candidate === "string" && candidate) return candidate;
  }
  return "";
};

const sameValue = (actual: unknown, expected: unknown): boolean => {
  if (Object.is(actual, expected)) return true;
  if (Array.isArray(actual) && Array.isArray(expected)) {
    return actual.length === expected.length && actual.every((value, index) => sameValue(value, expected[index]));
  }
  if (
    actual !== null &&
    expected !== null &&
    typeof actual === "object" &&
    typeof expected === "object" &&
    !Array.isArray(actual) &&
    !Array.isArray(expected)
  ) {
    const actualRecord = actual as Record<string, unknown>;
    return Object.entries(expected as Record<string, unknown>).every(([key, value]) =>
      sameValue(actualRecord[key], value)
    );
  }
  return false;
};

export function upsertHumanReviewChange(
  changes: HumanReviewChange[],
  change: HumanReviewChange
): HumanReviewChange[] {
  const targetId = change.target_id.trim();
  if (!targetId || Object.keys(change.fields).length === 0) return changes;
  const index = changes.findIndex(
    (candidate) =>
      candidate.target_type === change.target_type &&
      candidate.target_id === targetId
  );
  if (index < 0) {
    return [...changes, { ...change, target_id: targetId, fields: { ...change.fields } }];
  }
  return changes.map((candidate, candidateIndex) =>
    candidateIndex === index
      ? { ...candidate, fields: { ...candidate.fields, ...change.fields } }
      : candidate
  );
}

export function buildHumanReviewDecisionRequest(
  input: BuildHumanReviewDecisionInput
): HumanReviewDecisionRequest {
  let changes: HumanReviewChange[] = [];
  if (input.evidencePackId && (input.markState !== "none" || input.lowConfidence)) {
    const fields: Record<string, unknown> = {};
    if (input.lowConfidence) fields.low_confidence = true;
    if (input.markState !== "none") fields.recording_disposition = input.markState;
    changes = upsertHumanReviewChange(changes, {
      target_type: "evidence_pack",
      target_id: input.evidencePackId,
      fields
    });
  }
  for (const change of input.stagedChanges) {
    changes = upsertHumanReviewChange(changes, change);
  }
  const request: HumanReviewDecisionRequest = {
    decision: changes.length > 0
      ? "modified"
      : input.agentState === "rejected"
        ? "rejected"
        : "accepted"
  };
  const note = input.note?.trim();
  if (note) request.note = note;
  if (changes.length > 0) request.changes = changes;
  return request;
}

export function validateHumanReviewDecisionClosure(input: DecisionClosureInput): string[] {
  const errors: string[] = [];
  if (input.receiptRootTraceId !== input.expectedRootTraceId) {
    errors.push(
      `人审回执 root_trace_id 不一致：期望 ${input.expectedRootTraceId}，实际 ${input.receiptRootTraceId || "unknown"}`
    );
  }
  const taskId = recordId(input.taskReadback, "id", "review_task_id");
  const taskStatus = recordId(input.taskReadback, "status");
  const taskDecisionId = recordId(input.taskReadback, "decision_id", "review_decision_id");
  const taskRootTraceId = recordId(input.taskReadback, "root_trace_id", "trace_id");
  if (taskId !== input.reviewTaskId) {
    errors.push(`HumanReviewTask 回读对象不一致：期望 ${input.reviewTaskId}，实际 ${taskId || "unknown"}`);
  }
  if (!taskStatus || ["pending", "queued", "running"].includes(taskStatus)) {
    errors.push(`HumanReviewTask ${input.reviewTaskId} 仍处于 ${taskStatus || "unknown"}`);
  }
  if (taskDecisionId !== input.decisionId) {
    errors.push(`HumanReviewTask ${input.reviewTaskId} decision 不一致`);
  }
  if (taskRootTraceId !== input.expectedRootTraceId) {
    errors.push(`HumanReviewTask ${input.reviewTaskId} root_trace_id 不一致`);
  }

  const evidencePackId = recordId(input.evidencePackReadback, "evidence_pack_id", "id");
  const evidenceRootTraceId = recordId(
    input.evidencePackReadback,
    "root_trace_id",
    "trace_id"
  );
  if (evidencePackId !== input.evidencePackId) {
    errors.push(`EvidencePack 回读对象不一致：期望 ${input.evidencePackId}，实际 ${evidencePackId || "unknown"}`);
  }
  if (recordId(input.evidencePackReadback, "review_decision_id", "decision_id") !== input.decisionId) {
    errors.push(`EvidencePack ${input.evidencePackId} decision 不一致`);
  }
  if (evidenceRootTraceId !== input.expectedRootTraceId) {
    errors.push(`EvidencePack ${input.evidencePackId} root_trace_id 不一致`);
  }

  const affectedKeys = new Set<string>();
  for (const affectedObject of input.affectedObjects) {
    const key = `${affectedObject.type}:${affectedObject.id}`;
    if (affectedKeys.has(key)) {
      errors.push(`${key} 在 receipt affected_objects 中重复`);
      continue;
    }
    affectedKeys.add(key);
    if (!affectedObject.readback_url) {
      errors.push(`${key} 缺少 receipt readback_url`);
    }
    const readback = input.affectedReadbacks[key];
    if (!readback) {
      errors.push(`${key} 缺少写后回读`);
      continue;
    }
    if (readback.type !== affectedObject.type || readback.id !== affectedObject.id) {
      errors.push(
        `${key} 回读对象不一致：实际 ${readback.type || "unknown"}:${readback.id || "unknown"}`
      );
    }
    if (readback.review_decision_id !== input.decisionId) {
      errors.push(`${key} review_decision_id 不一致`);
    }
    if (readback.root_trace_id !== input.expectedRootTraceId) {
      errors.push(`${key} root_trace_id 不一致`);
    }
    if (
      affectedObject.resource_version !== undefined &&
      (
        readback.resource_version === undefined ||
        (
          affectedObject.type === "platform_callback"
            ? readback.resource_version < affectedObject.resource_version
            : readback.resource_version !== affectedObject.resource_version
        )
      )
    ) {
      errors.push(`${key} resource_version 不一致`);
    }
    if (
      readback.resource === null ||
      typeof readback.resource !== "object" ||
      Array.isArray(readback.resource)
    ) {
      errors.push(`${key} 回读缺少权威 resource`);
    }
  }
  for (const requiredKey of [
    `human_review_task:${input.reviewTaskId}`,
    `evidence_pack:${input.evidencePackId}`,
    `human_review_decision:${input.decisionId}`
  ]) {
    if (!affectedKeys.has(requiredKey)) {
      errors.push(`${requiredKey} 未出现在 receipt affected_objects`);
    }
  }

  for (const change of input.request.changes ?? []) {
    const key = `${change.target_type}:${change.target_id}`;
    if (!affectedKeys.has(key)) {
      errors.push(`${change.target_type} ${change.target_id} 未出现在 receipt affected_objects`);
    }
    const affectedReadback = input.affectedReadbacks[key];
    const readback = affectedReadback?.resource;
    if (!affectedReadback || !readback) {
      errors.push(`${change.target_type} ${change.target_id} 缺少写后回读`);
      continue;
    }
    if (recordId(readback, "review_decision_id", "decision_id") !== input.decisionId) {
      errors.push(`${change.target_type} ${change.target_id} decision 不一致`);
    }
    if (recordId(readback, "root_trace_id", "trace_id") !== input.expectedRootTraceId) {
      errors.push(`${change.target_type} ${change.target_id} root_trace_id 不一致`);
    }
    const reviewOverrides =
      change.target_type === "evidence_pack" &&
      readback.review_overrides !== null &&
      typeof readback.review_overrides === "object" &&
      !Array.isArray(readback.review_overrides)
        ? readback.review_overrides as Record<string, unknown>
        : {};
    for (const [field, expected] of Object.entries(change.fields)) {
      const actual =
        change.target_type === "evidence_pack" && field in reviewOverrides
          ? reviewOverrides[field]
          : readback[field];
      if (!sameValue(actual, expected)) {
        errors.push(`${change.target_type} ${change.target_id} 字段 ${field} 回读不一致`);
      }
    }
  }
  return errors;
}
