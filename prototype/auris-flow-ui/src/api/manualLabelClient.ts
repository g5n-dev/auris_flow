import { apiRequest, stableIdempotencyKey } from "./client";
import type { ApiEnvelope } from "./client";

export type ManualLabelValueType =
  | "boolean"
  | "categorical"
  | "multi"
  | "numeric"
  | "temporal"
  | "hierarchical";

export type ReleaseBundleHead = {
  release_head_id: string;
  environment: "production";
  label_version_id: string;
  generation: number;
  status: "active";
  trace_id: string;
};

export type LabelVersionItem = {
  label_version_item_id: string;
  label_version_id: string;
  label_id: string;
  canonical_name: string;
  aliases: string[];
  value_type: ManualLabelValueType;
  risk_level: string;
  status: "active" | "retired" | "pending-configuration";
  definition_sha256: string | null;
  trace_id: string;
};

export type ManualLabelDraftReceipt = {
  annotation_id: string;
  audio_session_id: string;
  draft_sha256: string;
  event_or_segment_id: string;
  evidence_sha256: string;
  label_id: string;
  label_version_id: string;
  occurred_at: string;
  release_head_generation: number;
  status: "draft" | "submitted";
  fact_id?: string;
  trace_id: string;
};

export type ManualLabelRebasePreview = {
  can_confirm: boolean;
  preview: {
    bundle_sha256: string;
    current_release_head_generation: number;
    mapping_bundle_id: string;
    mapping_paths: Array<{
      comparability_status: string;
      path_sha256: string;
      relation_path: Array<Record<string, unknown>>;
      requires_recompute: boolean;
      target_label_id: string | null;
    }>;
    new_label_id: string | null;
    new_label_version_id: string;
    old_draft_sha256: string;
    old_label_id: string;
    old_label_version_id: string;
    requires_manual_selection: boolean;
  };
  preview_sha256: string;
  status: "preview";
  trace_id: string;
};

export type ManualLabelDraftCreatePayload = {
  annotation_kind: "label-fact-draft";
  annotation_id: string;
  label_version_id: string;
  label_id: string;
  subject_scope: string;
  subject_key: string;
  event_or_segment_id: string;
  assertion_slot: string;
  occurred_at: string;
  evidence_ref: {
    type: string;
    id: string;
    sha256: string;
    start_ms: number;
    end_ms: number;
  };
  value_type: ManualLabelValueType;
  value: unknown;
  environment: "production";
  expected_release_head_generation: number;
};

const post = <T>(path: string, payload: unknown, idempotencyScope: string) =>
  apiRequest<T>(path, {
    method: "POST",
    headers: {
      "Idempotency-Key": stableIdempotencyKey(idempotencyScope, payload)
    },
    body: JSON.stringify(payload)
  });

export const getProductionReleaseBundleHead = (): Promise<ApiEnvelope<ReleaseBundleHead>> =>
  apiRequest<ReleaseBundleHead>("/v1/release-bundle-heads/production");

export const listActiveLabelVersionItems = (
  labelVersionId: string
): Promise<ApiEnvelope<{ items: LabelVersionItem[] }>> =>
  apiRequest<{ items: LabelVersionItem[] }>(
    `/v1/label-versions/${encodeURIComponent(labelVersionId)}/items?status=active`
  );

export const getLabelVersionLifecycle = (
  labelVersionId: string
): Promise<ApiEnvelope<Record<string, unknown>>> =>
  apiRequest<Record<string, unknown>>(
    `/v1/label-versions/${encodeURIComponent(labelVersionId)}`
  );

export const createManualLabelDraft = (
  audioSessionId: string,
  payload: ManualLabelDraftCreatePayload
): Promise<ApiEnvelope<ManualLabelDraftReceipt>> =>
  post(
    `/v1/audio-sessions/${encodeURIComponent(audioSessionId)}/annotations`,
    payload,
    `manual_label_draft_create:${audioSessionId}:${payload.annotation_id}`
  );

export const submitManualLabelDraft = (
  audioSessionId: string,
  annotationId: string,
  draftSha256: string,
  expectedReleaseHeadGeneration: number
): Promise<ApiEnvelope<ManualLabelDraftReceipt>> => {
  const payload = {
    expected_draft_sha256: draftSha256,
    expected_release_head_generation: expectedReleaseHeadGeneration,
    confirmation: "submit-frozen-manual-label"
  };
  return post(
    `/v1/audio-sessions/${encodeURIComponent(audioSessionId)}/annotations/${encodeURIComponent(annotationId)}/submissions`,
    payload,
    `manual_label_draft_submit:${audioSessionId}:${annotationId}`
  );
};

export const previewManualLabelDraftRebase = (
  audioSessionId: string,
  annotationId: string,
  mappingBundleId: string,
  targetLabelId: string,
  expectedReleaseHeadGeneration: number
): Promise<ApiEnvelope<ManualLabelRebasePreview>> => {
  const payload = {
    action: "preview",
    mapping_bundle_id: mappingBundleId,
    target_label_id: targetLabelId,
    expected_release_head_generation: expectedReleaseHeadGeneration
  };
  return post(
    `/v1/audio-sessions/${encodeURIComponent(audioSessionId)}/annotations/${encodeURIComponent(annotationId)}/rebases`,
    payload,
    `manual_label_draft_rebase_preview:${audioSessionId}:${annotationId}`
  );
};

export const confirmManualLabelDraftRebase = (
  audioSessionId: string,
  annotationId: string,
  mappingBundleId: string,
  targetLabelId: string,
  expectedReleaseHeadGeneration: number,
  newAnnotationId: string,
  previewSha256: string
): Promise<ApiEnvelope<ManualLabelDraftReceipt>> => {
  const payload = {
    action: "confirm",
    mapping_bundle_id: mappingBundleId,
    target_label_id: targetLabelId,
    expected_release_head_generation: expectedReleaseHeadGeneration,
    new_annotation_id: newAnnotationId,
    preview_sha256: previewSha256,
    confirmation: "confirm-reviewed-manual-label-rebase"
  };
  return post(
    `/v1/audio-sessions/${encodeURIComponent(audioSessionId)}/annotations/${encodeURIComponent(annotationId)}/rebases`,
    payload,
    `manual_label_draft_rebase_confirm:${audioSessionId}:${annotationId}:${newAnnotationId}`
  );
};
