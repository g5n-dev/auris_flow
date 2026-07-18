import type { CanvasActionScope } from "./canvasActionScope";
import type { CanvasDraftModelActions } from "./buildCanvasDraftModelActions";
import type { CanvasExecutionActions } from "./buildCanvasExecutionActions";
import type { CanvasExperimentCreateActions } from "./buildCanvasExperimentCreateActions";
import type { CanvasExperimentStateActions } from "./buildCanvasExperimentStateActions";
import type { ControlledExperiment, ControlledExperimentMetricSnapshot } from "../../../api/client";
import { computeControlledExperimentMetrics, decideBackendRun, decideControlledExperiment, publishTaskVersionDraft, startControlledExperiment } from "../../../api/client";
import { normalizeBackendRunStatus } from "../../../shared/runtime/backendRunStatus";

export function buildCanvasExperimentActions(scope: CanvasActionScope & CanvasDraftModelActions & CanvasExecutionActions & CanvasExperimentStateActions & CanvasExperimentCreateActions) {
  const { applyControlledExperiment, controlledExperiment, createTaskControlledExperiment, pushRunHistory, reloadControlledExperiment, setCanvasAction, setCanvasNotice, setExecutionState, setExperimentActionPending, setTaskReleaseGate, shortTrace, taskReleaseGate } = scope;

  const startTaskControlledExperiment = async () => {
      if (!controlledExperiment) return;
      setExperimentActionPending("start");
      setCanvasNotice({ status: "pending", title: "正在启动实验", detail: "校验 SceneProfile 锁和任务版本后开启确定性分流。" });
      try {
        const started = await startControlledExperiment(
          controlledExperiment.experiment_id,
          controlledExperiment.resource_version,
          { correlationId: controlledExperiment.trace_id }
        );
        applyControlledExperiment(started.data);
        setCanvasNotice({
          status: "success",
          title: "实验已启动",
          detail: `${started.data.experiment_id} 已进入稳定分桶；原始分流键不会落库。`
        });
        pushRunHistory(`Experiment · ${started.data.experiment_id}`, `运行中 / ${shortTrace(started.data.trace_id)}`);
      } catch (error) {
        setCanvasNotice({ status: "error", title: "实验启动失败", detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。" });
      } finally {
        setExperimentActionPending(null);
      }
    };

  const computeTaskExperimentMetrics = async () => {
      if (!controlledExperiment) return;
      setExperimentActionPending("compute");
      setCanvasNotice({ status: "pending", title: "正在计算实验指标", detail: "聚合不可变曝光结果并生成带证据 SHA 的指标快照。" });
      try {
        const computed = await computeControlledExperimentMetrics(
          controlledExperiment.experiment_id,
          { correlationId: controlledExperiment.trace_id }
        );
        const refreshed = await reloadControlledExperiment(controlledExperiment.experiment_id);
        const verdictLabels: Record<ControlledExperimentMetricSnapshot["verdict"], string> = {
          insufficient_sample: "样本不足",
          blocked_sample_ratio: "样本比例异常",
          blocked_guardrail: "守护指标阻断",
          promote: "满足晋级门槛",
          hold: "继续观测"
        };
        setCanvasNotice({
          status: computed.data.verdict === "promote" ? "success" : computed.data.verdict.startsWith("blocked_") ? "error" : "idle",
          title: `指标快照：${verdictLabels[computed.data.verdict]}`,
          detail: `快照 v${computed.data.snapshot_version} · 证据 ${computed.data.evidence_sha256.slice(0, 12)} · trace ${shortTrace(computed.data.trace_id)}。`
        });
        pushRunHistory(`ExperimentMetricSnapshot · ${computed.data.metric_snapshot_id}`, `${verdictLabels[computed.data.verdict]} / ${refreshed.counts.outcomes} 条结果`);
      } catch (error) {
        setCanvasNotice({ status: "error", title: "实验指标计算失败", detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。" });
      } finally {
        setExperimentActionPending(null);
      }
    };

  const requestTaskExperimentRelease = async (
      experiment: ControlledExperiment,
      metricSnapshotId: string,
      correlationId?: string
    ) => {
      const release = await publishTaskVersionDraft(experiment.candidate_task_version_id, {
        source: "controlled_experiment",
        experiment_id: experiment.experiment_id,
        metric_snapshot_id: metricSnapshotId,
        design_sha256: experiment.design_sha256,
        release_channel: "production",
        reason: "受控实验晋级候选版本，继续执行独立任务版本发布门禁"
      }, { correlationId });
      setTaskReleaseGate(release.data);
      return release.data;
    };

  const retryTaskExperimentRelease = async () => {
      const snapshot = controlledExperiment?.latest_metric_snapshot;
      const latestDecision = controlledExperiment?.decisions?.[0];
      if (
        !controlledExperiment
        || controlledExperiment.status !== "decided"
        || latestDecision?.decision !== "promote_candidate"
        || !snapshot
      ) {
        setCanvasNotice({
          status: "error",
          title: "无法创建发布门禁",
          detail: "需要后端已记录晋级决策，并且该决策仍绑定最新指标快照。"
        });
        return;
      }
      setExperimentActionPending("release-gate");
      setCanvasNotice({
        status: "pending",
        title: "正在重试发布门禁",
        detail: "重新校验实验设计、最新证据快照和候选版本内容锁。"
      });
      try {
        const release = await requestTaskExperimentRelease(
          controlledExperiment,
          snapshot.metric_snapshot_id,
          String(latestDecision.trace_id ?? controlledExperiment.trace_id ?? "")
        );
        setCanvasNotice({
          status: "success",
          title: "发布门禁已创建",
          detail: `${release.id} 等待独立管理员审批；实验晋级尚未直接切换生产版本。`
        });
        pushRunHistory(`TaskVersionPublish · ${release.id}`, `等待独立审批 / ${shortTrace(release.trace_id)}`);
      } catch (error) {
        setCanvasNotice({
          status: "error",
          title: "发布门禁重试失败",
          detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。"
        });
      } finally {
        setExperimentActionPending(null);
      }
    };

  const decideTaskControlledExperiment = async (
      decision: "pause" | "resume" | "stop" | "promote_candidate" | "reject_candidate"
    ) => {
      if (!controlledExperiment) return;
      const snapshot = controlledExperiment.latest_metric_snapshot;
      if (["promote_candidate", "reject_candidate"].includes(decision) && !snapshot) {
        setCanvasNotice({ status: "error", title: "实验决策被阻断", detail: "终局决策前必须先生成不可变指标快照。" });
        return;
      }
      setExperimentActionPending(decision);
      setCanvasNotice({ status: "pending", title: "正在记录实验决策", detail: "决策、指标快照和操作者将写入不可变审计事实。" });
      try {
        const result = await decideControlledExperiment(
          controlledExperiment.experiment_id,
          {
            decision,
            ...(snapshot ? { metric_snapshot_id: snapshot.metric_snapshot_id } : {}),
            expected_resource_version: controlledExperiment.resource_version,
            reason: decision === "promote_candidate"
              ? "主指标通过、守护指标未退化，提交候选任务版本发布门禁"
              : `${decision} 由任务配置实验工作台人工确认`
          },
          { correlationId: controlledExperiment.trace_id }
        );
        const refreshed = await reloadControlledExperiment(controlledExperiment.experiment_id);
        pushRunHistory(`ExperimentDecision · ${controlledExperiment.experiment_id}`, `${decision} / ${shortTrace(String(result.data.trace_id ?? ""))}`);
        if (decision === "promote_candidate") {
          try {
            const release = await requestTaskExperimentRelease(
              refreshed,
              String(snapshot?.metric_snapshot_id ?? ""),
              String(result.data.trace_id ?? refreshed.trace_id ?? "")
            );
            setCanvasNotice({
              status: "success",
              title: "实验已决策，发布门禁已创建",
              detail: `${release.id} 仍需独立发布审批；实验决策不会直接覆盖生产。`
            });
          } catch (releaseError) {
            setCanvasNotice({
              status: "error",
              title: "实验已决策，发布门禁待重试",
              detail: releaseError instanceof Error
                ? releaseError.message
                : "晋级事实已保存，但发布门禁创建失败，可单独重试。"
            });
          }
        } else {
          setCanvasNotice({
            status: "success",
            title: "实验状态已更新",
            detail: `${refreshed.experiment_id} 当前为 ${refreshed.status}，trace ${shortTrace(String(result.data.trace_id ?? refreshed.trace_id))}。`
          });
        }
      } catch (error) {
        setCanvasNotice({ status: "error", title: "实验决策失败", detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。" });
      } finally {
        setExperimentActionPending(null);
      }
    };

  const rejectTaskVersionRelease = async () => {
      if (!taskReleaseGate || normalizeBackendRunStatus(taskReleaseGate.status) !== "blocked") return;
      setCanvasAction("publish");
      setCanvasNotice({
        status: "pending",
        title: "正在拒绝发布",
        detail: `${taskReleaseGate.id} 将取消，不会修改当前任务版本。`
      });
      try {
        const receipt = await decideBackendRun(
          taskReleaseGate.id,
          "rejected",
          "发布门禁人工退回，保留草稿继续修改"
        );
        setTaskReleaseGate(receipt.data);
        setExecutionState("idle");
        setCanvasNotice({
          status: "error",
          title: "发布已退回",
          detail: `${receipt.data.id} 已取消，当前草稿未发布；trace：${shortTrace(receipt.data.trace_id)}。`
        });
        pushRunHistory(`TaskVersionPublish · ${receipt.data.id}`, "门禁已拒绝 / 草稿保留");
      } catch (error) {
        setCanvasNotice({
          status: "error",
          title: "发布退回失败",
          detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。"
        });
      } finally {
        setCanvasAction(null);
      }
    };

  return {
    applyControlledExperiment,
    reloadControlledExperiment,
    createTaskControlledExperiment,
    startTaskControlledExperiment,
    computeTaskExperimentMetrics,
    requestTaskExperimentRelease,
    retryTaskExperimentRelease,
    decideTaskControlledExperiment,
    rejectTaskVersionRelease
  };
}

export type CanvasExperimentActions = ReturnType<typeof buildCanvasExperimentActions>;
