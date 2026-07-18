import type { EvaluationModuleProps } from "../types";
import type { EvaluationState } from "./useEvaluationState";
import type { EvaluationSelection } from "./buildEvaluationSelection";
import type { EvaluationFocusRecovery } from "./useEvaluationFocusRecovery";
import type { EvaluationContextActions } from "./buildEvaluationContextActions";
import type { HotwordPollingActions } from "./buildHotwordPollingActions";
import type { HotwordVersionRecovery } from "./useHotwordVersionRecovery";
import type { EvaluationRunActions } from "./buildEvaluationRunActions";
import { addHotwordVersionItem, decideHotwordBadcase, listHotwordBadcases, patchHotwordBadcase, patchHotwordPackVersion, patchHotwordVersionItem } from "../../../api/client";
import { hotwordVersionView, normalizeHotwordForComparison } from "../../../shared/runtime/hotwordVersionViews";
import type { EvaluationBadcaseWorkflowItem } from "../types";

type BuildEvaluationBadcaseActionsScope = EvaluationModuleProps & EvaluationState & EvaluationSelection & EvaluationFocusRecovery & EvaluationContextActions & HotwordPollingActions & HotwordVersionRecovery & EvaluationRunActions;

export function buildEvaluationBadcaseActions(discoverHotwordCandidateVersion: BuildEvaluationBadcaseActionsScope["discoverHotwordCandidateVersion"], evaluationAction: BuildEvaluationBadcaseActionsScope["evaluationAction"], hotwordPollGenerationRef: BuildEvaluationBadcaseActionsScope["hotwordPollGenerationRef"], pollHotwordBuildRun: BuildEvaluationBadcaseActionsScope["pollHotwordBuildRun"], pushRunRecord: BuildEvaluationBadcaseActionsScope["pushRunRecord"], refreshHotwordCandidateVersion: BuildEvaluationBadcaseActionsScope["refreshHotwordCandidateVersion"], selectedBadcaseDraft: BuildEvaluationBadcaseActionsScope["selectedBadcaseDraft"], selectedBadcaseResolved: BuildEvaluationBadcaseActionsScope["selectedBadcaseResolved"], selectedBadcaseWorkflow: BuildEvaluationBadcaseActionsScope["selectedBadcaseWorkflow"], setBadcaseDrafts: BuildEvaluationBadcaseActionsScope["setBadcaseDrafts"], setBadcaseWorkflow: BuildEvaluationBadcaseActionsScope["setBadcaseWorkflow"], setEvaluationAction: BuildEvaluationBadcaseActionsScope["setEvaluationAction"], setEvaluationNotice: BuildEvaluationBadcaseActionsScope["setEvaluationNotice"], shortTrace: BuildEvaluationBadcaseActionsScope["shortTrace"], syncHotwordVersionState: BuildEvaluationBadcaseActionsScope["syncHotwordVersionState"]) {
  const updateBadcaseDraft = (key: keyof typeof selectedBadcaseDraft, value: string) => {
      setBadcaseDrafts((current) => ({
        ...current,
        [selectedBadcaseWorkflow.id]: {
          ...selectedBadcaseDraft,
          [key]: value
        }
      }));
    };

  const applyBadcaseStatusLocally = (
      status: EvaluationBadcaseWorkflowItem["status"],
      resourceVersion = selectedBadcaseWorkflow.resourceVersion
    ) => {
      setBadcaseWorkflow((current) =>
        current.map((item) =>
          item.id === selectedBadcaseWorkflow.id
            ? {
                ...item,
                status,
                rootCause: selectedBadcaseDraft.rootCause,
                fix: selectedBadcaseDraft.fix,
                target: selectedBadcaseDraft.target,
                owner: selectedBadcaseDraft.owner,
                resourceVersion
              }
            : item
        )
      );
    };

  const moveBadcaseStatus = async (status: EvaluationBadcaseWorkflowItem["status"]) => {
      if (!selectedBadcaseResolved) {
        setEvaluationNotice({
          status: "error",
          title: "Badcase 写入已阻断",
          detail: `${selectedBadcaseWorkflow.id} 未从后端恢复，不会回退或写入其他 Badcase。`
        });
        return;
      }
      if (selectedBadcaseWorkflow.capability === "asr-hotword") {
        if (evaluationAction) {
          setEvaluationNotice({
            status: "pending",
            title: "易错词确认仍在处理",
            detail: "当前 Badcase 写操作尚未返回，请等待对象版本和 Trace 回执。"
          });
          return;
        }
        setEvaluationAction("hotword_badcase_decision");
        setEvaluationNotice({
          status: "pending",
          title: status === "待回流" ? "易错词确认提交中" : "Badcase 状态更新中",
          detail: `${selectedBadcaseWorkflow.id} · 正在读取实时 resource_version · ${status}`
        });
        try {
          const liveBadcases = await listHotwordBadcases({ limit: 50 });
          const liveBadcase = liveBadcases.data.items.find((item) => item.badcase_id === selectedBadcaseWorkflow.id);
          const liveResourceVersion = liveBadcase?.resource_version;
          if (typeof liveResourceVersion !== "number") {
            throw new Error(`${selectedBadcaseWorkflow.id} GET 响应缺少 resource_version，写入已阻断`);
          }
          const response = status === "待回流"
            ? await decideHotwordBadcase(selectedBadcaseWorkflow.id, {
                decision: "confirmed",
                reason: `${selectedBadcaseDraft.rootCause}；${selectedBadcaseDraft.fix}`,
                expected_resource_version: liveResourceVersion
              })
            : await patchHotwordBadcase(selectedBadcaseWorkflow.id, {
                expected_resource_version: liveResourceVersion,
                status: status === "待人审" ? "pending-review" : status === "已入回归" ? "in-regression" : "pending-attribution",
                root_cause: selectedBadcaseDraft.rootCause,
                fix_suggestion: selectedBadcaseDraft.fix
              });
          const backendVersion = response.data.raw.resource_version;
          const nextVersion = typeof backendVersion === "number"
            ? backendVersion
            : liveResourceVersion + 1;
          applyBadcaseStatusLocally(status, nextVersion);
          pushRunRecord(
            status === "待回流" ? "易错词确认已落账" : "ASR 热词 Badcase 状态更新",
            `${selectedBadcaseWorkflow.id} -> ${status} / v${nextVersion} / ${shortTrace(response.meta?.trace_id ?? response.data.trace_id)}`,
            status === "已入回归" ? "完成" : "待确认"
          );
          setEvaluationNotice({
            status: "success",
            title: status === "待回流" ? "易错词确认已落账" : "ASR 热词 Badcase 已更新",
            detail: `${response.data.id} · resource_version ${nextVersion} · Trace ${response.meta?.trace_id ?? response.data.trace_id ?? "no-trace"} · ${status}`
          });
        } catch (error) {
          setEvaluationNotice({
            status: "error",
            title: status === "待回流" ? "易错词确认失败" : "Badcase 状态更新失败",
            detail: error instanceof Error ? error.message : "后端未返回乐观锁版本与 Trace，状态保持不变。"
          });
        } finally {
          setEvaluationAction(null);
        }
        return;
      }
      applyBadcaseStatusLocally(status);
      pushRunRecord("badcase 状态更新", `${selectedBadcaseWorkflow.id} -> ${status}`, status === "已入回归" ? "完成" : "待确认");
      setEvaluationNotice({
        status: "success",
        title: "badcase 已更新",
        detail: `${selectedBadcaseWorkflow.title} 已进入「${status}」，回流目标 ${selectedBadcaseDraft.target}。`
      });
    };

  const addSelectedBadcaseToHotwordCandidate = async () => {
      if (!selectedBadcaseResolved || selectedBadcaseWorkflow.capability !== "asr-hotword" || !selectedBadcaseWorkflow.standardTerm) {
        setEvaluationNotice({
          status: "error",
          title: "候选词包写入已阻断",
          detail: selectedBadcaseResolved
            ? "仅 capability=asr-hotword 且已确认标准词的 Badcase 可以进入候选词包。"
            : `${selectedBadcaseWorkflow.id} 未从后端恢复，候选写入已阻断。`
        });
        return;
      }
      setEvaluationAction("hotword_candidate");
      setEvaluationNotice({
        status: "pending",
        title: "正在加入候选热词",
        detail: `${selectedBadcaseWorkflow.standardTerm} · 正在读取 pack.current_version_id 并发现候选版本。`
      });
      try {
        const candidate = await discoverHotwordCandidateVersion(true);
        if (!candidate) throw new Error("候选不可用");
        if (!["draft", "gate_blocked"].includes(candidate.status)) {
          throw new Error(`${candidate.id} 当前为 ${candidate.status}，不可继续修改词项`);
        }
        const standardTerm = selectedBadcaseWorkflow.standardTerm;
        const normalizedTerm = normalizeHotwordForComparison(standardTerm);
        const existingItem = candidate.items.find((item) =>
          normalizeHotwordForComparison(item.normalizedTerm) === normalizedTerm ||
          normalizeHotwordForComparison(item.canonicalTerm) === normalizedTerm
        );
        const recognizedAlias = selectedBadcaseWorkflow.recognizedText?.trim() ?? "";
        const aliases = Array.from(new Map(
          [...(existingItem?.aliases ?? []), recognizedAlias]
            .filter(Boolean)
            .filter((alias) => normalizeHotwordForComparison(alias) !== normalizedTerm)
            .map((alias) => [normalizeHotwordForComparison(alias), alias])
        ).values());
        const weight = Math.max(existingItem?.weight ?? 0, Math.max(0, Math.min(100, selectedBadcaseWorkflow.priority ?? 80)));
        const response = existingItem
          ? await patchHotwordVersionItem(candidate.id, existingItem.id, {
              expected_resource_version: existingItem.resourceVersion,
              aliases,
              weight,
              source_badcase_id: selectedBadcaseWorkflow.id,
              source_type: "badcase"
            })
          : await addHotwordVersionItem(candidate.id, {
              canonical_term: standardTerm,
              aliases,
              category: "vehicle-model",
              weight,
              source_badcase_id: selectedBadcaseWorkflow.id
            });
        const liveVersion = await refreshHotwordCandidateVersion(candidate.id);
        pushRunRecord("候选热词已加入", `${selectedBadcaseWorkflow.id} → ${liveVersion.id}/${response.data.id} / Trace ${response.meta?.trace_id ?? response.data.trace_id ?? "no-trace"}`);
        const buildResponse = await patchHotwordPackVersion(liveVersion.id, {
          expected_resource_version: liveVersion.resourceVersion,
          status: "validating",
          provider: "auris-audio-stack"
        });
        const buildRunId = typeof buildResponse.data.raw.build_run_id === "string"
          ? buildResponse.data.raw.build_run_id
          : null;
        if (!buildRunId) throw new Error("validating 缺少 build_run_id");
        const validatingVersion = hotwordVersionView(buildResponse.data.raw);
        if (validatingVersion) syncHotwordVersionState(validatingVersion);
        const buildTrace = buildResponse.meta?.trace_id ?? buildResponse.data.trace_id;
        setEvaluationNotice({
          status: "pending",
          title: "候选词包构建运行已创建",
          detail: `${response.data.id} · ${liveVersion.id} · Run ${buildRunId} · Trace ${buildTrace ?? "no-trace"}`
        });
        const generation = hotwordPollGenerationRef.current + 1;
        hotwordPollGenerationRef.current = generation;
        await pollHotwordBuildRun(liveVersion.id, buildRunId, generation, buildTrace);
      } catch (error) {
        setEvaluationNotice({
          status: "error",
          title: "候选热词加入失败",
          detail: error instanceof Error ? error.message : "后端未返回 item_id，候选版本未改变。"
        });
      } finally {
        setEvaluationAction(null);
      }
    };

  return {
    updateBadcaseDraft,
    applyBadcaseStatusLocally,
    moveBadcaseStatus,
    addSelectedBadcaseToHotwordCandidate
  };
}

export type EvaluationBadcaseActions = ReturnType<typeof buildEvaluationBadcaseActions>;
