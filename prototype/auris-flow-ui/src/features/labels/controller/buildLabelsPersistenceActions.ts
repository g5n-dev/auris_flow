import type { LabelsActionScope } from "./labelsActionScope";
import { createLabelBadcase, createPlatformMutation, createUserIntentIdempotencyKey, submitHumanReviewDecisionBatch } from "../../../api/client";

type BuildLabelsPersistenceActionsScope = LabelsActionScope;

export function buildLabelsPersistenceActions(activeCandidate: BuildLabelsPersistenceActionsScope["activeCandidate"], activeConflict: BuildLabelsPersistenceActionsScope["activeConflict"], activeIntent: BuildLabelsPersistenceActionsScope["activeIntent"], activeReviewTask: BuildLabelsPersistenceActionsScope["activeReviewTask"], applyReviewDecision: BuildLabelsPersistenceActionsScope["applyReviewDecision"], batchPreflightPassed: BuildLabelsPersistenceActionsScope["batchPreflightPassed"], conflictNote: BuildLabelsPersistenceActionsScope["conflictNote"], draftInputs: BuildLabelsPersistenceActionsScope["draftInputs"], editableDraftTagName: BuildLabelsPersistenceActionsScope["editableDraftTagName"], ensureLabelHumanReviewTask: BuildLabelsPersistenceActionsScope["ensureLabelHumanReviewTask"], executeLabelOptimization: BuildLabelsPersistenceActionsScope["executeLabelOptimization"], hasAuthoritativeCandidate: BuildLabelsPersistenceActionsScope["hasAuthoritativeCandidate"], hasBoundReviewTask: BuildLabelsPersistenceActionsScope["hasBoundReviewTask"], humanChangeDraft: BuildLabelsPersistenceActionsScope["humanChangeDraft"], labelAgentBackendRun: BuildLabelsPersistenceActionsScope["labelAgentBackendRun"], labelEntityAction: BuildLabelsPersistenceActionsScope["labelEntityAction"], labelPublishPollGenerationRef: BuildLabelsPersistenceActionsScope["labelPublishPollGenerationRef"], labelRootTraceId: BuildLabelsPersistenceActionsScope["labelRootTraceId"], labelShortTrace: BuildLabelsPersistenceActionsScope["labelShortTrace"], lastLabelPublishIntentRef: BuildLabelsPersistenceActionsScope["lastLabelPublishIntentRef"], lockedLabelVersionId: BuildLabelsPersistenceActionsScope["lockedLabelVersionId"], lockedPromptVersionId: BuildLabelsPersistenceActionsScope["lockedPromptVersionId"], optimizationInputs: BuildLabelsPersistenceActionsScope["optimizationInputs"], resetCandidateReview: BuildLabelsPersistenceActionsScope["resetCandidateReview"], resetLabelEvalState: BuildLabelsPersistenceActionsScope["resetLabelEvalState"], reviewInputs: BuildLabelsPersistenceActionsScope["reviewInputs"], reviewState: BuildLabelsPersistenceActionsScope["reviewState"], selectedBatchAggregates: BuildLabelsPersistenceActionsScope["selectedBatchAggregates"], setActionFeedback: BuildLabelsPersistenceActionsScope["setActionFeedback"], setAgentRunState: BuildLabelsPersistenceActionsScope["setAgentRunState"], setBackendLabelBadcaseIds: BuildLabelsPersistenceActionsScope["setBackendLabelBadcaseIds"], setBackendLabelVersionId: BuildLabelsPersistenceActionsScope["setBackendLabelVersionId"], setBackendPromptCandidateId: BuildLabelsPersistenceActionsScope["setBackendPromptCandidateId"], setBackendPromptVersionId: BuildLabelsPersistenceActionsScope["setBackendPromptVersionId"], setBackendReleaseDeployment: BuildLabelsPersistenceActionsScope["setBackendReleaseDeployment"], setBackendReleaseDeploymentId: BuildLabelsPersistenceActionsScope["setBackendReleaseDeploymentId"], setBatchDecisionReceipt: BuildLabelsPersistenceActionsScope["setBatchDecisionReceipt"], setClosedLoopReviewProgress: BuildLabelsPersistenceActionsScope["setClosedLoopReviewProgress"], setConflictDecision: BuildLabelsPersistenceActionsScope["setConflictDecision"], setDraftStatus: BuildLabelsPersistenceActionsScope["setDraftStatus"], setExperimentState: BuildLabelsPersistenceActionsScope["setExperimentState"], setLabelAgentBackendRun: BuildLabelsPersistenceActionsScope["setLabelAgentBackendRun"], setLabelAggregates: BuildLabelsPersistenceActionsScope["setLabelAggregates"], setLabelAggregationBackendRun: BuildLabelsPersistenceActionsScope["setLabelAggregationBackendRun"], setLabelEntityAction: BuildLabelsPersistenceActionsScope["setLabelEntityAction"], setLabelEntityNotice: BuildLabelsPersistenceActionsScope["setLabelEntityNotice"], setLabelExtractionBackendRun: BuildLabelsPersistenceActionsScope["setLabelExtractionBackendRun"], setLabelFactReadState: BuildLabelsPersistenceActionsScope["setLabelFactReadState"], setLabelObservations: BuildLabelsPersistenceActionsScope["setLabelObservations"], setLabelPublishRequest: BuildLabelsPersistenceActionsScope["setLabelPublishRequest"], setLabelRootTraceId: BuildLabelsPersistenceActionsScope["setLabelRootTraceId"], setLabelTaxonomySuggestions: BuildLabelsPersistenceActionsScope["setLabelTaxonomySuggestions"], setPromptCandidateFact: BuildLabelsPersistenceActionsScope["setPromptCandidateFact"], setPromptReviewProgress: BuildLabelsPersistenceActionsScope["setPromptReviewProgress"], setReleaseChecks: BuildLabelsPersistenceActionsScope["setReleaseChecks"], setReviewDraftStatesByCandidateId: BuildLabelsPersistenceActionsScope["setReviewDraftStatesByCandidateId"], setReviewState: BuildLabelsPersistenceActionsScope["setReviewState"], setReviewStatesByCandidateId: BuildLabelsPersistenceActionsScope["setReviewStatesByCandidateId"], setSelectedCandidateIds: BuildLabelsPersistenceActionsScope["setSelectedCandidateIds"], setSelectedReviewId: BuildLabelsPersistenceActionsScope["setSelectedReviewId"]) {
  const applyConflictDecision = async (decision: string) => {
      const state: typeof reviewState = decision.includes("接受") ? "已接受" : decision.includes("阻断") ? "已拒绝" : decision.includes("修改") ? "已修改" : "待人工";
      const saved = await applyReviewDecision(state, decision, `${activeConflict.label}：${conflictNote}`);
      if (saved) setConflictDecision(decision);
    };

  const persistLabelDraft = async (action: "create" | "rule") => {
      if (labelEntityAction) return;
      setLabelEntityAction(action === "create" ? "label-draft" : "rule-draft");
      setLabelEntityNotice({ status: "pending", title: action === "create" ? "正在创建候选标签" : "正在保存规则候选", detail: `${editableDraftTagName} 正在写入候选 LabelVersion。` });
      try {
        const receipt = await createPlatformMutation("labels", {
          base_version: optimizationInputs.currentTagVersion,
          action: action === "create" ? "create_label_candidate" : "save_rule_candidate",
          candidate_version: optimizationInputs.candidateTagVersion,
          ...(hasAuthoritativeCandidate ? { candidate_id: activeCandidate.id } : {}),
          label_name: editableDraftTagName,
          definition: draftInputs.definition,
          trigger_rules: draftInputs.trigger,
          conflict_rules: draftInputs.conflict,
          examples_json: [draftInputs.positive],
          negative_examples_json: [draftInputs.negative],
          prompt_version: optimizationInputs.promptVersion,
          source: "labels_ui"
        });
        const rootTraceId = receipt.meta?.trace_id ?? receipt.data.trace_id;
        if (!rootTraceId) throw new Error("LabelVersion 回执缺少 trace_id，无法建立闭环 root trace");
        setBackendLabelVersionId(receipt.data.id);
        setLabelRootTraceId(rootTraceId);
        setLabelExtractionBackendRun(null);
        setLabelAggregationBackendRun(null);
        setLabelObservations([]);
        setLabelAggregates([]);
        setLabelTaxonomySuggestions([]);
        setClosedLoopReviewProgress({});
        setReviewStatesByCandidateId({});
        setReviewDraftStatesByCandidateId({});
        setSelectedCandidateIds([]);
        setBatchDecisionReceipt(null);
        setLabelAgentBackendRun(null);
        setAgentRunState("idle");
        setBackendPromptCandidateId("");
        setBackendPromptVersionId("");
        setBackendLabelBadcaseIds([]);
        setPromptCandidateFact(null);
        setPromptReviewProgress(null);
        setDraftStatus(action === "create" ? "草稿" : "已校准");
        setLabelEntityNotice({ status: "success", title: action === "create" ? "候选标签已创建" : "规则候选已保存", detail: `${receipt.data.id} · ${receipt.data.status} · root trace ${labelShortTrace(rootTraceId)}。` });
        setActionFeedback(`${editableDraftTagName} 已写入 ${receipt.data.id}，不会覆盖线上 v1.8.4。`);
      } catch (error) {
        setLabelEntityNotice({ status: "error", title: action === "create" ? "候选标签创建失败，可重试" : "规则候选保存失败，可重试", detail: `${error instanceof Error ? error.message : "unknown error"}。本地编辑内容已保留。` });
      } finally {
        setLabelEntityAction(null);
      }
    };

  const saveDraftRule = () => { void persistLabelDraft("rule"); };

  const createLabelDraft = () => {
      resetCandidateReview(activeCandidate.id);
      setExperimentState("未开始");
      void persistLabelDraft("create");
    };

  const modifyLabelRule = () => {
      saveDraftRule();
    };

  const sendLabelHumanLoop = async () => {
      if (labelEntityAction) return;
      if (!hasAuthoritativeCandidate || !hasBoundReviewTask) {
        setLabelEntityNotice({
          status: "error",
          title: "尚无可复核的后端候选",
          detail: "请先保存 LabelVersion、绑定 PromptVersion 并运行抽取；只有已物化且带 review_task_id 的 LabelAggregate 才能进入 Human Loop。"
        });
        return;
      }
      const candidate = activeCandidate;
      const taskTitle = activeReviewTask.title;
      setLabelEntityAction("human-task");
      setLabelEntityNotice({ status: "pending", title: "正在创建 Human Loop", detail: `${candidate.id} 正在写入人审队列。` });
      try {
        const task = await ensureLabelHumanReviewTask(candidate, taskTitle);
        setReviewState("待人工", candidate.id);
        setSelectedReviewId(activeReviewTask.id);
        setLabelEntityNotice({ status: "success", title: "Human Loop 已创建并读回", detail: `${task.id} · ${String(task.data.status ?? "pending")} · trace ${labelShortTrace(task.traceId)}。` });
        setActionFeedback(`${candidate.id} 已进入 ${String(task.data.queue ?? "label_candidate_review")}；等待 ${activeIntent.owner} 复核。`);
      } catch (error) {
        setLabelEntityNotice({ status: "error", title: "Human Loop 创建失败，可重试", detail: `${error instanceof Error ? error.message : "unknown error"}。未生成本地成功任务。` });
      } finally {
        setLabelEntityAction(null);
      }
    };

  const saveCandidateVersion = async () => {
      if (labelEntityAction) return;
      setLabelEntityAction("candidate-version");
      setLabelEntityNotice({
        status: "pending",
        title: "正在保存候选版本",
        detail: hasAuthoritativeCandidate
          ? `${activeCandidate.id} 正在写入并等待读回 LabelVersion。`
          : "正在创建本轮 root LabelVersion；抽取完成前不会写入占位候选。"
      });
      try {
        const receipt = await createPlatformMutation("labels", {
          base_version: optimizationInputs.currentTagVersion,
          candidate_version: "v1.9.0-rc2",
          ...(hasAuthoritativeCandidate ? { candidate_id: activeCandidate.id } : {}),
          ...(labelAgentBackendRun?.id ? { optimization_run_id: labelAgentBackendRun.id } : {})
        });
        const rootTraceId = receipt.meta?.trace_id ?? receipt.data.trace_id;
        if (!rootTraceId) throw new Error("LabelVersion 回执缺少 trace_id，无法建立闭环 root trace");
        setBackendLabelVersionId(receipt.data.id);
        setLabelRootTraceId(rootTraceId);
        setLabelExtractionBackendRun(null);
        setLabelAggregationBackendRun(null);
        setLabelObservations([]);
        setLabelAggregates([]);
        setLabelTaxonomySuggestions([]);
        setLabelFactReadState("idle");
        setClosedLoopReviewProgress({});
        setReviewStatesByCandidateId({});
        setReviewDraftStatesByCandidateId({});
        setSelectedCandidateIds([]);
        setBatchDecisionReceipt(null);
        setLabelAgentBackendRun(null);
        setAgentRunState("idle");
        setBackendPromptCandidateId("");
        setBackendPromptVersionId("");
        setBackendLabelBadcaseIds([]);
        setPromptCandidateFact(null);
        setPromptReviewProgress(null);
        setDraftStatus("待实验");
        setExperimentState("影子评测中");
        setReleaseChecks((current) => ({ ...current, "Human Loop 已处理": reviewState !== "待人工" }));
        resetLabelEvalState();
        setBackendReleaseDeploymentId("");
        setBackendReleaseDeployment(null);
        labelPublishPollGenerationRef.current += 1;
        lastLabelPublishIntentRef.current = null;
        setLabelPublishRequest({ status: "idle" });
        setLabelEntityNotice({ status: "success", title: "候选版本已保存", detail: `${receipt.data.id} · ${receipt.data.status} · root trace ${labelShortTrace(rootTraceId)}。` });
        setActionFeedback(`${receipt.data.id} 已保存；线上版本未被覆盖。`);
      } catch (error) {
        setLabelEntityNotice({ status: "error", title: "候选版本保存失败，可重试", detail: `${error instanceof Error ? error.message : "未知错误"}。页面版本状态未更新。` });
        setActionFeedback(`候选版本保存失败：${error instanceof Error ? error.message : "未知错误"}`);
      } finally {
        setLabelEntityAction(null);
      }
    };

  const runExtractionTask = () => {
      setDraftStatus("草稿");
      void executeLabelOptimization("extraction");
    };

  const applyCandidateBatchDecision = async () => {
      if (labelEntityAction || !batchPreflightPassed) return;
      const items = selectedBatchAggregates.map((aggregate) => ({
        review_task_id: aggregate.review_task_id as string,
        decision: "accepted" as const,
        note: `标签工作台批量接受；${reviewInputs.note}`
      }));
      setLabelEntityAction("human-decision-batch");
      setBatchDecisionReceipt(null);
      setLabelEntityNotice({
        status: "pending",
        title: "正在提交批量人审决策",
        detail: `${items.length} 个显式 review_task_id 等待服务端逐项裁决。`
      });
      try {
        const response = await submitHumanReviewDecisionBatch(
          { items },
          {
            idempotencyKey: createUserIntentIdempotencyKey("label-human-review-batch"),
            correlationId: labelRootTraceId
          }
        );
        const receipt = response.data;
        setBatchDecisionReceipt(receipt);
        receipt.results.forEach((result) => {
          if (result.status === "success" && result.aggregate_id) {
            setReviewState("已接受", result.aggregate_id);
          }
        });
        setSelectedCandidateIds((current) => current.filter((candidateId) =>
          !receipt.results.some((result) => result.aggregate_id === candidateId && result.status === "success")
        ));
        const noticeStatus = receipt.status === "completed" ? "success" : receipt.status === "failed" ? "error" : "success";
        setLabelEntityNotice({
          status: noticeStatus,
          title: receipt.status === "completed" ? "批量决策全部完成" : receipt.status === "partial" ? "批量决策部分完成" : "批量决策失败",
          detail: `${receipt.batch_id} · success ${receipt.counts.success} / skipped ${receipt.counts.skipped} / failed ${receipt.counts.failed} · trace ${labelShortTrace(receipt.trace_id)}。`
        });
      } catch (error) {
        setLabelEntityNotice({
          status: "error",
          title: "批量人审请求失败，可安全重试",
          detail: error instanceof Error ? error.message : "unknown error"
        });
      } finally {
        setLabelEntityAction(null);
      }
    };

  const applyCandidateAction = async (action: "accept" | "batch" | "human" | "badcase" | "rule") => {
      if (action === "accept") {
        await applyReviewDecision("已接受", "接受当前候选", `${activeCandidate.id} 人工确认后进入候选版本。`);
        return;
      }
      if (action === "batch") {
        await applyCandidateBatchDecision();
        return;
      }
      if (action === "human") {
        await sendLabelHumanLoop();
        return;
      }
      if (action === "badcase") {
        if (labelEntityAction) return;
        if (!hasAuthoritativeCandidate) {
          setLabelEntityNotice({
            status: "error",
            title: "尚无可回流的后端候选",
            detail: "请先运行抽取并读取 LabelAggregate；占位对象不能写入 LabelBadcase。"
          });
          return;
        }
        setLabelEntityAction("label-badcase");
        setLabelEntityNotice({
          status: "pending",
          title: "正在写入标签 badcase",
          detail: `${activeCandidate.id} 正在绑定证据、版本与失败原因。`
        });
        try {
          const receipt = await createLabelBadcase({
            capability: "labeling",
            failure_reason: activeCandidate.conflict === "无" ? "manual-hard-example" : "source-conflict",
            severity: activeIntent.risk === "高" ? "high" : "medium",
            source_ref: { type: "label_candidate", id: activeCandidate.id },
            evidence_refs: [
              { type: "trace", id: activeCandidate.traceId },
              { type: "asset_impact", id: activeCandidate.assetImpact }
            ],
            label_version_id: lockedLabelVersionId,
            prompt_version_id: lockedPromptVersionId || optimizationInputs.promptVersion,
            expected_value: humanChangeDraft.after,
            actual_value: activeCandidate.value,
            field_diff: {
              conflict: activeCandidate.conflict,
              human_state: reviewState,
              reason: humanChangeDraft.reason
            }
          }, { correlationId: labelRootTraceId });
          setBackendLabelBadcaseIds((current) => Array.from(new Set([...current, receipt.data.id])));
          setLabelEntityNotice({
            status: "success",
            title: "标签 badcase 已写入",
            detail: `${receipt.data.id} · ${receipt.data.status} · trace ${labelShortTrace(receipt.meta?.trace_id ?? receipt.data.trace_id)}。`
          });
          setActionFeedback(`${activeCandidate.id} 已回流为 ${receipt.data.id}；后续可进入 Prompt 优化触发扫描。`);
        } catch (error) {
          setLabelEntityNotice({
            status: "error",
            title: "标签 badcase 写入失败，可重试",
            detail: error instanceof Error ? error.message : "unknown error"
          });
        } finally {
          setLabelEntityAction(null);
        }
        return;
      }
      await persistLabelDraft("rule");
    };

  return {
    applyConflictDecision,
    persistLabelDraft,
    saveDraftRule,
    createLabelDraft,
    modifyLabelRule,
    sendLabelHumanLoop,
    saveCandidateVersion,
    runExtractionTask,
    applyCandidateBatchDecision,
    applyCandidateAction
  };
}

export type LabelsPersistenceActions = ReturnType<typeof buildLabelsPersistenceActions>;
