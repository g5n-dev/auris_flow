import type { EvaluationModuleProps } from "../types";
import type { EvaluationState } from "./useEvaluationState";
import type { EvaluationSelection } from "./buildEvaluationSelection";
import type { EvaluationFocusRecovery } from "./useEvaluationFocusRecovery";
import type { EvaluationContextActions } from "./buildEvaluationContextActions";
import { createHotwordPackVersion, getBackendRun, getHotwordPackVersion, listHotwordPacks, listHotwordPackVersions } from "../../../api/client";
import { backendRunStatusLabel } from "../../../shared/runtime/backendRunStatus";
import type { HotwordPackVersionView } from "../../../shared/runtime/hotwordVersionViews";
import { HOTWORD_PACK_DOMAIN, hotwordVersionView, nextHotwordVersionLabel } from "../../../shared/runtime/hotwordVersionViews";
import { buildHotwordPublishPollingAction } from "./buildHotwordPublishPollingAction";

type BuildHotwordPollingActionsScope = EvaluationModuleProps & EvaluationState & EvaluationSelection & EvaluationFocusRecovery & EvaluationContextActions;

export function buildHotwordPollingActions(hotwordPollGenerationRef: BuildHotwordPollingActionsScope["hotwordPollGenerationRef"], hotwordPollTimerRef: BuildHotwordPollingActionsScope["hotwordPollTimerRef"], hotwordPublishRetryRunRef: BuildHotwordPollingActionsScope["hotwordPublishRetryRunRef"], pushRunRecord: BuildHotwordPollingActionsScope["pushRunRecord"], setEvaluationNotice: BuildHotwordPollingActionsScope["setEvaluationNotice"], setHotwordBaselineVersion: BuildHotwordPollingActionsScope["setHotwordBaselineVersion"], setHotwordCandidateVersion: BuildHotwordPollingActionsScope["setHotwordCandidateVersion"], setHotwordEvalPassed: BuildHotwordPollingActionsScope["setHotwordEvalPassed"], setHotwordEvalResult: BuildHotwordPollingActionsScope["setHotwordEvalResult"], setHotwordEvalRunId: BuildHotwordPollingActionsScope["setHotwordEvalRunId"], setHotwordPublishRecovery: BuildHotwordPollingActionsScope["setHotwordPublishRecovery"], setHotwordPublished: BuildHotwordPollingActionsScope["setHotwordPublished"], shortTrace: BuildHotwordPollingActionsScope["shortTrace"]) {
  const numericHotwordMetrics = (value: unknown): Record<string, number> | null => {
      if (!value || typeof value !== "object" || Array.isArray(value)) return null;
      const metrics = Object.fromEntries(
        Object.entries(value as Record<string, unknown>).filter((entry): entry is [string, number] =>
          typeof entry[1] === "number" && Number.isFinite(entry[1])
        )
      );
      return Object.keys(metrics).length ? metrics : null;
    };

  const captureHotwordEvalResult = (raw: Record<string, unknown>) => {
      const nested = raw.hotword_eval && typeof raw.hotword_eval === "object" && !Array.isArray(raw.hotword_eval)
        ? raw.hotword_eval as Record<string, unknown>
        : raw;
      const gate = nested.gate && typeof nested.gate === "object" && !Array.isArray(nested.gate)
        ? nested.gate as Record<string, unknown>
        : null;
      setHotwordEvalResult({
        baselineMetrics: numericHotwordMetrics(nested.baseline_metrics),
        candidateMetrics: numericHotwordMetrics(nested.candidate_metrics),
        gatePassed: gate?.passed === true ? true : gate?.passed === false ? false : null,
        blockedReasons: Array.isArray(gate?.blocked_reasons)
          ? gate.blocked_reasons.filter((reason): reason is string => typeof reason === "string")
          : []
      });
    };

  const syncHotwordVersionState = (version: HotwordPackVersionView | null) => {
      setHotwordCandidateVersion(version);
      setHotwordEvalRunId(version?.evalRunId ?? null);
      setHotwordEvalPassed(Boolean(version?.evalLocked && ["review_required", "approved", "published"].includes(version.status)));
      setHotwordPublished(version?.status === "published");
    };

  const discoverHotwordCandidateVersion = async (createIfMissing: boolean): Promise<HotwordPackVersionView | null> => {
      const packsResponse = await listHotwordPacks();
      const pack = packsResponse.data.items.find((item) => item.domain === HOTWORD_PACK_DOMAIN);
      const packId = typeof pack?.pack_id === "string" ? pack.pack_id : typeof pack?.id === "string" ? pack.id : null;
      const currentVersionId = typeof pack?.current_version_id === "string" ? pack.current_version_id : null;
      if (!packId) throw new Error("未找到汽车销售热词包");
      if (!currentVersionId) throw new Error(`${packId} 缺少 current_version_id，不能判定发布基线`);
      const versionsResponse = await listHotwordPackVersions(packId, { limit: 50 });
      const versions = versionsResponse.data.items
        .map((item) => hotwordVersionView(item))
        .filter((item): item is HotwordPackVersionView => item !== null);
      let baseline = versions.find((version) => version.id === currentVersionId);
      if (!baseline || baseline.status !== "published") {
        throw new Error(`current_version_id=${currentVersionId} 不是已发布版本`);
      }
      const baselineDetail = await getHotwordPackVersion(baseline.id);
      baseline = hotwordVersionView(baselineDetail.data) ?? baseline;
      setHotwordBaselineVersion(baseline);
      let candidate = versions
        .filter((version) => version.id !== baseline.id && version.baselineVersionId === baseline.id)
        .sort((left, right) => right.resourceVersion - left.resourceVersion)[0] ?? null;
      if (!candidate && createIfMissing) {
        const created = await createHotwordPackVersion(packId, {
          version: nextHotwordVersionLabel(baseline.version),
          baseline_version_id: baseline.id
        });
        const createdCandidate = hotwordVersionView(created.data.raw);
        if (!createdCandidate) throw new Error("候选版本创建成功但响应缺少 version_id/resource_version");
        candidate = createdCandidate;
      }
      if (candidate) {
        const detail = await getHotwordPackVersion(candidate.id);
        candidate = hotwordVersionView(detail.data) ?? candidate;
      }
      syncHotwordVersionState(candidate);
      return candidate;
    };

  const refreshHotwordCandidateVersion = async (versionId: string): Promise<HotwordPackVersionView> => {
      const response = await getHotwordPackVersion(versionId);
      const version = hotwordVersionView(response.data);
      if (!version) throw new Error("词包版本详情缺少 version_id/resource_version");
      syncHotwordVersionState(version);
      return version;
    };

  const waitForHotwordPoll = () => new Promise<void>((resolve) => {
      hotwordPollTimerRef.current = window.setTimeout(() => {
        hotwordPollTimerRef.current = null;
        resolve();
      }, 500);
    });

  const pollHotwordBuildRun = async (
      versionId: string,
      runId: string,
      generation: number,
      initialTrace?: string
    ): Promise<HotwordPackVersionView | null> => {
      const maxAttempts = 10;
      for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
        if (hotwordPollGenerationRef.current !== generation) return null;
        const response = await getBackendRun(runId);
        const raw = response.data.raw;
        const status = typeof raw.status === "string" ? raw.status.toLowerCase() : response.data.status.toLowerCase();
        const trace = response.meta?.trace_id ?? response.data.trace_id ?? initialTrace;
        if (["failed", "error", "dead_letter", "canceled", "cancelled"].includes(status)) {
          setEvaluationNotice({
            status: "error",
            title: "候选词包构建失败",
            detail: `${runId} · ${backendRunStatusLabel(status)} · Trace ${trace ?? "no-trace"}`
          });
          pushRunRecord("ASR 热词 Provider 构建", `${runId} / ${status} / ${shortTrace(trace)}`, "待确认");
          return null;
        }
        if (["success", "succeeded", "complete", "completed"].includes(status)) {
          const liveVersion = await refreshHotwordCandidateVersion(versionId);
          if (liveVersion.status !== "ready_for_eval" || !liveVersion.providerArtifactRef || liveVersion.compiledProvider !== "auris-audio-stack") {
            setEvaluationNotice({
              status: "error",
              title: "候选词包构建回执异常",
              detail: `${runId} 已完成，但版本为 ${liveVersion.status} 或缺少冻结的 auris-audio-stack 产物。`
            });
            return null;
          }
          setEvaluationNotice({
            status: "success",
            title: "候选词包构建完成",
            detail: `${liveVersion.id} · ready_for_eval / rv${liveVersion.resourceVersion} · Run ${runId} · Trace ${trace ?? "no-trace"}`
          });
          pushRunRecord("ASR 热词 Provider 构建", `${runId} / ready_for_eval / ${shortTrace(trace)}`);
          return liveVersion;
        }
        setEvaluationNotice({
          status: "pending",
          title: "候选词包构建运行已创建",
          detail: `${runId} · ${backendRunStatusLabel(status)} · 轮询 ${attempt}/${maxAttempts} · Trace ${trace ?? "no-trace"}`
        });
        if (attempt < maxAttempts) await waitForHotwordPoll();
      }
      if (hotwordPollGenerationRef.current === generation) {
        setEvaluationNotice({
          status: "error",
          title: "候选词包构建等待超时",
          detail: `${runId} 在有限轮询窗口内未完成，可稍后从 RunRecord 恢复。`
        });
      }
      return null;
    };

  const pollHotwordEvalRun = async (
      versionId: string,
      runId: string,
      generation: number,
      initialTrace?: string
    ): Promise<void> => {
      const maxAttempts = 10;
      for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
        if (hotwordPollGenerationRef.current !== generation) return;
        const response = await getBackendRun(runId);
        const raw = response.data.raw;
        const status = typeof raw.status === "string" ? raw.status.toLowerCase() : response.data.status.toLowerCase();
        const trace = response.meta?.trace_id ?? response.data.trace_id ?? initialTrace;
        if (["failed", "error", "dead_letter", "canceled", "cancelled"].includes(status)) {
          setHotwordEvalPassed(false);
          setEvaluationNotice({
            status: "error",
            title: "影子评测执行失败",
            detail: `${runId} · ${backendRunStatusLabel(status)} · Trace ${trace ?? "no-trace"}`
          });
          pushRunRecord("ASR 热词影子评测", `${runId} / ${status} / ${shortTrace(trace)}`, "待确认");
          return;
        }
        if (["success", "succeeded", "complete", "completed"].includes(status)) {
          const evalRaw = raw.hotword_eval && typeof raw.hotword_eval === "object" && !Array.isArray(raw.hotword_eval)
            ? raw.hotword_eval as Record<string, unknown>
            : raw;
          const gate = evalRaw.gate && typeof evalRaw.gate === "object" ? evalRaw.gate as Record<string, unknown> : null;
          const locked = evalRaw.locked === true;
          const passed = gate?.passed === true && locked;
          const blockedReasons = Array.isArray(gate?.blocked_reasons)
            ? gate.blocked_reasons.filter((reason): reason is string => typeof reason === "string")
            : [];
          captureHotwordEvalResult(raw);
          const liveVersion = await refreshHotwordCandidateVersion(versionId);
          setHotwordEvalPassed(passed && liveVersion.evalLocked);
          setEvaluationNotice({
            status: passed && liveVersion.evalLocked ? "success" : "error",
            title: passed && liveVersion.evalLocked ? "影子评测门禁通过" : "影子评测门禁阻断",
            detail: passed && liveVersion.evalLocked
              ? `${runId} · success + gate.passed + locked · ${liveVersion.status} · rv${liveVersion.resourceVersion} · Trace ${trace ?? "no-trace"}`
              : `${runId} · ${blockedReasons.join("、") || (!locked ? "评测结果未锁定" : "门禁未通过")} · Trace ${trace ?? "no-trace"}`
          });
          pushRunRecord(
            "ASR 热词影子评测",
            `${runId} / ${passed && liveVersion.evalLocked ? "通过" : `阻断 ${blockedReasons.join(",") || "unlocked"}`} / ${shortTrace(trace)}`,
            passed && liveVersion.evalLocked ? "完成" : "待确认"
          );
          return;
        }
        setEvaluationNotice({
          status: "pending",
          title: "影子评测运行已创建",
          detail: `${runId} · ${backendRunStatusLabel(status)} · 轮询 ${attempt}/${maxAttempts} · Trace ${trace ?? "no-trace"}`
        });
        if (attempt < maxAttempts) await waitForHotwordPoll();
      }
      if (hotwordPollGenerationRef.current !== generation) return;
      setHotwordEvalPassed(false);
      setEvaluationNotice({
        status: "error",
        title: "影子评测等待超时",
        detail: `${runId} 在有限轮询窗口内未完成，可稍后从 RunRecord 恢复。`
      });
    };

  const pollHotwordPublishRun = buildHotwordPublishPollingAction(
    hotwordPollGenerationRef,
    hotwordPublishRetryRunRef,
    pushRunRecord,
    refreshHotwordCandidateVersion,
    setEvaluationNotice,
    setHotwordPublishRecovery,
    setHotwordPublished,
    shortTrace,
    waitForHotwordPoll
  );

  return {
    numericHotwordMetrics,
    captureHotwordEvalResult,
    syncHotwordVersionState,
    discoverHotwordCandidateVersion,
    refreshHotwordCandidateVersion,
    waitForHotwordPoll,
    pollHotwordBuildRun,
    pollHotwordEvalRun,
    pollHotwordPublishRun
  };
}

export type HotwordPollingActions = ReturnType<typeof buildHotwordPollingActions>;
