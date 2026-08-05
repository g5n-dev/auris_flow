import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const sourceUrl = new URL("./reviewDecisionModel.ts", import.meta.url);

const loadModel = async () => {
  const source = await readFile(sourceUrl, "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022
    }
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
};

test("人审请求把录音判定、低置信、边界、EventLink 和标签修订合并为受控 changes", async () => {
  const { buildHumanReviewDecisionRequest } = await loadModel();
  const request = buildHumanReviewDecisionRequest({
    agentState: "accepted",
    evidencePackId: "AF-128",
    lowConfidence: true,
    markState: "crosstalk",
    note: "串音污染报价标签，人工修订后接受",
    stagedChanges: [
      {
        target_type: "conversation_boundary",
        target_id: "boundary_s128_v1",
        fields: { start_ms: 30000, end_ms: 694000, decision: "manual_confirmed" }
      },
      {
        target_type: "event_link",
        target_id: "event_quote_122718",
        fields: {
          source_event_id: "quote_amount_mismatch",
          document_ref: "SO-2026-001",
          relation_type: "quote",
          confidence: 0.91,
          evidence_window: "12:27:18 - 12:28:30"
        }
      },
      {
        target_type: "label_candidate",
        target_id: "cand_af128_amount_conflict",
        fields: { label: "报价金额", value_or_action: "金额冲突（人工修订）" }
      }
    ]
  });

  assert.equal(request.decision, "modified");
  assert.equal(request.note, "串音污染报价标签，人工修订后接受");
  assert.deepEqual(request.changes, [
    {
      target_type: "evidence_pack",
      target_id: "AF-128",
      fields: {
        low_confidence: true,
        recording_disposition: "crosstalk"
      }
    },
    {
      target_type: "conversation_boundary",
      target_id: "boundary_s128_v1",
      fields: { start_ms: 30000, end_ms: 694000, decision: "manual_confirmed" }
    },
    {
      target_type: "event_link",
      target_id: "event_quote_122718",
      fields: {
        source_event_id: "quote_amount_mismatch",
        document_ref: "SO-2026-001",
        relation_type: "quote",
        confidence: 0.91,
        evidence_window: "12:27:18 - 12:28:30"
      }
    },
    {
      target_type: "label_candidate",
      target_id: "cand_af128_amount_conflict",
      fields: { label: "报价金额", value_or_action: "金额冲突（人工修订）" }
    }
  ]);
});

test("同一目标的多次修订必须合并，不能生成后端会拒绝的重复 change", async () => {
  const { upsertHumanReviewChange } = await loadModel();
  const initial = upsertHumanReviewChange([], {
    target_type: "event_link",
    target_id: "event_quote_122718",
    fields: { document_ref: "SO-2026-001" }
  });
  const merged = upsertHumanReviewChange(initial, {
    target_type: "event_link",
    target_id: "event_quote_122718",
    fields: { evidence_window: "12:27:18 - 12:28:30" }
  });

  assert.deepEqual(merged, [
    {
      target_type: "event_link",
      target_id: "event_quote_122718",
      fields: {
        document_ref: "SO-2026-001",
        evidence_window: "12:27:18 - 12:28:30"
      }
    }
  ]);
});

test("没有字段修订时保留接受或拒绝决定，不伪造 modified", async () => {
  const { buildHumanReviewDecisionRequest } = await loadModel();

  assert.deepEqual(
    buildHumanReviewDecisionRequest({
      agentState: "accepted",
      evidencePackId: "AF-128",
      lowConfidence: false,
      markState: "none",
      note: "证据一致",
      stagedChanges: []
    }),
    { decision: "accepted", note: "证据一致" }
  );
  assert.equal(
    buildHumanReviewDecisionRequest({
      agentState: "rejected",
      evidencePackId: "AF-128",
      lowConfidence: false,
      markState: "none",
      note: "证据不足",
      stagedChanges: []
    }).decision,
    "rejected"
  );
});

test("写后回读必须同时匹配任务、EvidencePack、receipt 全量 affected_objects 和每个变更目标", async () => {
  const { validateHumanReviewDecisionClosure } = await loadModel();
  const request = {
    decision: "modified",
    note: "人工修订",
    changes: [
      {
        target_type: "evidence_pack",
        target_id: "AF-128",
        fields: { recording_disposition: "main", low_confidence: false }
      },
      {
        target_type: "event_link",
        target_id: "event_quote_122718",
        fields: { document_ref: "SO-2026-001" }
      },
      {
        target_type: "label_candidate",
        target_id: "cand_af128_amount_conflict",
        fields: { value_or_action: "金额冲突（人工修订）" }
      }
    ]
  };
  const base = {
    decisionId: "hrd_001",
    expectedRootTraceId: "trace_root_001",
    receiptRootTraceId: "trace_root_001",
    evidencePackId: "AF-128",
    evidencePackReadback: {
      evidence_pack_id: "AF-128",
      review_decision_id: "hrd_001",
      root_trace_id: "trace_root_001",
      recording_disposition: "main",
      low_confidence: false,
      label_candidates: [
        {
          candidate_id: "cand_af128_amount_conflict",
          review_decision_id: "hrd_001",
          root_trace_id: "trace_root_001",
          value_or_action: "金额冲突（人工修订）"
        }
      ]
    },
    affectedObjects: [
      {
        type: "human_review_task",
        id: "hrt_amount_001",
        readback_url: "/api/v1/human-review-decisions/hrd_001/affected-objects/human_review_task/hrt_amount_001"
      },
      {
        type: "evidence_pack",
        id: "AF-128",
        readback_url: "/api/v1/human-review-decisions/hrd_001/affected-objects/evidence_pack/AF-128",
        resource_version: 1
      },
      {
        type: "event_link",
        id: "event_quote_122718",
        readback_url: "/api/v1/human-review-decisions/hrd_001/affected-objects/event_link/event_quote_122718"
      },
      {
        type: "label_candidate",
        id: "cand_af128_amount_conflict",
        readback_url: "/api/v1/human-review-decisions/hrd_001/affected-objects/label_candidate/cand_af128_amount_conflict"
      },
      {
        type: "human_review_decision",
        id: "hrd_001",
        readback_url: "/api/v1/human-review-decisions/hrd_001/affected-objects/human_review_decision/hrd_001"
      },
      {
        type: "platform_callback",
        id: "callback_review_001",
        readback_url: "/api/v1/human-review-decisions/hrd_001/affected-objects/platform_callback/callback_review_001",
        resource_version: 1
      }
    ],
    affectedReadbacks: {
      "human_review_task:hrt_amount_001": {
        type: "human_review_task",
        id: "hrt_amount_001",
        review_decision_id: "hrd_001",
        root_trace_id: "trace_root_001",
        resource: {
          id: "hrt_amount_001",
          decision_id: "hrd_001",
          root_trace_id: "trace_root_001"
        }
      },
      "evidence_pack:AF-128": {
        type: "evidence_pack",
        id: "AF-128",
        review_decision_id: "hrd_001",
        root_trace_id: "trace_root_001",
        resource_version: 1,
        resource: {
          evidence_pack_id: "AF-128",
          review_decision_id: "hrd_001",
          root_trace_id: "trace_root_001",
          review_overrides: {
            recording_disposition: "main",
            low_confidence: false
          }
        }
      },
      "event_link:event_quote_122718": {
        type: "event_link",
        id: "event_quote_122718",
        review_decision_id: "hrd_001",
        root_trace_id: "trace_root_001",
        resource: {
          id: "event_quote_122718",
          review_decision_id: "hrd_001",
          root_trace_id: "trace_root_001",
          relation_state: "modified",
          document_ref: "SO-2026-001"
        }
      },
      "label_candidate:cand_af128_amount_conflict": {
        type: "label_candidate",
        id: "cand_af128_amount_conflict",
        review_decision_id: "hrd_001",
        root_trace_id: "trace_root_001",
        resource: {
          candidate_id: "cand_af128_amount_conflict",
          review_decision_id: "hrd_001",
          root_trace_id: "trace_root_001",
          value_or_action: "金额冲突（人工修订）"
        }
      },
      "human_review_decision:hrd_001": {
        type: "human_review_decision",
        id: "hrd_001",
        review_decision_id: "hrd_001",
        root_trace_id: "trace_root_001",
        resource: { decision_id: "hrd_001", root_trace_id: "trace_root_001" }
      },
      "platform_callback:callback_review_001": {
        type: "platform_callback",
        id: "callback_review_001",
        review_decision_id: "hrd_001",
        root_trace_id: "trace_root_001",
        resource_version: 1,
        resource: {
          run_id: "callback_review_001",
          source_review_decision_id: "hrd_001",
          root_trace_id: "trace_root_001"
        }
      }
    },
    request,
    reviewTaskId: "hrt_amount_001",
    taskReadback: {
      id: "hrt_amount_001",
      status: "modified",
      decision: "modified",
      decision_id: "hrd_001",
      root_trace_id: "trace_root_001"
    }
  };

  assert.deepEqual(validateHumanReviewDecisionClosure(base), []);
  assert.deepEqual(
    validateHumanReviewDecisionClosure({
      ...base,
      evidencePackReadback: {
        evidence_pack_id: "AF-128",
        review_decision_id: "hrd_001",
        root_trace_id: "trace_root_001",
        review_overrides: {
          recording_disposition: "main",
          low_confidence: false
        },
        label_candidates: base.evidencePackReadback.label_candidates
      }
    }),
    [],
    "EvidencePack 内容保持不可变时，应从 review_overrides 核验人工覆盖层"
  );
  assert.match(
    validateHumanReviewDecisionClosure({
      ...base,
      affectedReadbacks: {
        ...base.affectedReadbacks,
        "event_link:event_quote_122718": {
          type: "event_link",
          id: "event_quote_122718",
          review_decision_id: "hrd_001",
          root_trace_id: "trace_root_001",
          resource: {
            id: "event_quote_122718",
            review_decision_id: "hrd_001",
            root_trace_id: "trace_root_001",
            document_ref: "SO-WRONG"
          }
        }
      }
    }).join("；"),
    /event_quote_122718.*document_ref/
  );
  assert.match(
    validateHumanReviewDecisionClosure({
      ...base,
      evidencePackReadback: {
        ...base.evidencePackReadback,
        review_decision_id: "hrd_other"
      }
    }).join("；"),
    /EvidencePack.*decision/
  );
  assert.match(
    validateHumanReviewDecisionClosure({
      ...base,
      receiptRootTraceId: "trace_action_999"
    }).join("；"),
    /回执.*root_trace_id/
  );
  assert.match(
    validateHumanReviewDecisionClosure({
      ...base,
      taskReadback: {
        ...base.taskReadback,
        root_trace_id: "trace_other"
      }
    }).join("；"),
    /HumanReviewTask.*root_trace_id/
  );
  assert.match(
    validateHumanReviewDecisionClosure({
      ...base,
      affectedReadbacks: {
        ...base.affectedReadbacks,
        "event_link:event_quote_122718": {
          ...base.affectedReadbacks["event_link:event_quote_122718"],
          root_trace_id: "trace_other"
        }
      }
    }).join("；"),
    /event_link.*root_trace_id/
  );
  const missingCallbackReadback = { ...base.affectedReadbacks };
  delete missingCallbackReadback["platform_callback:callback_review_001"];
  assert.match(
    validateHumanReviewDecisionClosure({
      ...base,
      affectedReadbacks: missingCallbackReadback
    }).join("；"),
    /platform_callback.*缺少写后回读/
  );
  assert.match(
    validateHumanReviewDecisionClosure({
      ...base,
      affectedReadbacks: {
        ...base.affectedReadbacks,
        "evidence_pack:AF-128": {
          ...base.affectedReadbacks["evidence_pack:AF-128"],
          resource_version: 2
        }
      }
    }).join("；"),
    /evidence_pack.*resource_version/
  );
});
