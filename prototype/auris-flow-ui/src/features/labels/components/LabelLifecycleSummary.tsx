import { useEffect, useState } from "react";

import { getLabelVersionResource } from "../../../api/client";
import {
  parseLabelLifecycleSummary,
  type LabelArtifactLifecycleStatus,
  type LabelLifecycleSummary
} from "../controller/labelLifecycleSummary";

type LifecycleReadState =
  | { status: "idle"; labelVersionId: "" }
  | { status: "loading"; labelVersionId: string }
  | { status: "failed"; labelVersionId: string; error: string }
  | { status: "ready"; labelVersionId: string; summary: LabelLifecycleSummary };

const statusLabels: Record<LabelArtifactLifecycleStatus, string> = {
  draft: "草稿",
  candidate: "候选",
  validated: "已校验",
  locked: "已锁定",
  evaluating: "评测中",
  gate_blocked: "门禁阻断",
  review_required: "待复核",
  approved: "已批准",
  published: "已发布",
  deprecated: "已废弃",
  archived: "已归档"
};

function formatTime(value: string | null) {
  if (!value) return "未返回";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }).format(new Date(value));
}

function statusTone(status: LabelArtifactLifecycleStatus | null) {
  if (status === "published") return "bound";
  if (!status) return "missing";
  return "";
}

function LifecycleFacts({ summary }: { summary: LabelLifecycleSummary }) {
  const activation = summary.productionActivation;
  const replacement = summary.replacement;
  const status = summary.status;
  const showDeprecation = status === "deprecated" || status === "archived";

  return (
    <div className="label-v2-list compact" data-testid="label-lifecycle-summary" data-label-artifact-status={status ?? "unavailable"}>
      {summary.issues.length > 0 ? (
        <div className="label-fact-state is-empty" role="status">
          <strong>标签生命周期信息不完整</strong>
          <p>{summary.issues.join("；")}。缺失字段保持不可用，不回退到页面静态版本名。</p>
        </div>
      ) : null}
      <div className={`label-release-eval-binding ${statusTone(status)}`}>
        <span>标签生命周期</span>
        <strong>{status ? statusLabels[status] : "状态未返回"}</strong>
        <em>
          {summary.labelVersionId ?? "LabelVersion ID 未返回"} · 发布时间 {formatTime(summary.publishedAt)}
        </em>
      </div>
      <div
        className={`label-release-eval-binding ${activation.state === "active" ? "bound" : ["ambiguous", "unavailable"].includes(activation.state) ? "missing" : ""}`}
        data-label-release-head-generation={activation.generation ?? ""}
      >
        <span>production Head</span>
        <strong>
          {activation.state === "active"
            ? `generation ${activation.generation}`
            : activation.state === "inactive"
              ? "当前未激活"
              : activation.state === "ambiguous"
                ? "激活指针歧义"
                : "generation 未返回"}
        </strong>
        <em>
          {activation.state === "active"
            ? `Deployment ${activation.deploymentId ?? "未返回"}`
            : activation.state === "inactive"
              ? "权威 environment_activations 中没有 production active Head"
              : "不推断当前环境 generation"}
        </em>
      </div>
      <div
        className={`label-release-eval-binding ${replacement.state === "mapped" ? "bound" : ["incomplete", "unavailable"].includes(replacement.state) ? "missing" : ""}`}
        data-label-replacement-version-id={replacement.labelVersionId ?? ""}
        data-label-mapping-bundle-id={replacement.mappingBundleId ?? ""}
      >
        <span>替代与映射</span>
        <strong>
          {replacement.state === "mapped"
            ? replacement.labelVersionId
            : replacement.state === "none"
              ? "未绑定替代版本"
              : replacement.state === "incomplete"
                ? "绑定不完整"
                : "替代关系未返回"}
        </strong>
        <em>
          {replacement.state === "mapped"
            ? `MappingBundle ${replacement.mappingBundleId}`
            : replacement.state === "none"
              ? "未推断跨版本统计映射"
              : "replacement_label_version_id 与 mapping_bundle_id 不可用"}
        </em>
      </div>
      {showDeprecation ? (
        <div className={`label-release-eval-binding ${summary.deprecationReason && summary.deprecatedAt ? "" : "missing"}`}>
          <span>废弃说明</span>
          <strong>{summary.deprecationReason ?? "废弃原因未返回"}</strong>
          <em>
            废弃时间 {formatTime(summary.deprecatedAt)}
            {status === "archived" ? ` · 归档时间 ${formatTime(summary.archivedAt)}` : ""}
          </em>
        </div>
      ) : null}
    </div>
  );
}

export function LabelLifecycleSummary({
  labelVersionId,
  refreshToken = ""
}: {
  labelVersionId: string;
  refreshToken?: string;
}) {
  const [retryGeneration, setRetryGeneration] = useState(0);
  const [readState, setReadState] = useState<LifecycleReadState>({ status: "idle", labelVersionId: "" });

  useEffect(() => {
    let cancelled = false;
    if (!labelVersionId) {
      setReadState({ status: "idle", labelVersionId: "" });
      return () => {
        cancelled = true;
      };
    }
    setReadState({ status: "loading", labelVersionId });
    void getLabelVersionResource(labelVersionId)
      .then((response) => {
        if (!cancelled) {
          setReadState({
            status: "ready",
            labelVersionId,
            summary: parseLabelLifecycleSummary(response.data)
          });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setReadState({
            status: "failed",
            labelVersionId,
            error: error instanceof Error ? error.message : "读取 LabelVersion 生命周期失败"
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [labelVersionId, refreshToken, retryGeneration]);

  if (!labelVersionId) {
    return (
      <div className="label-fact-state is-empty" data-testid="label-lifecycle-summary-empty" role="status">
        <strong>尚无权威 LabelVersion 生命周期</strong>
        <p>先保存后端 LabelVersion 强 ID；当前页面不会用候选展示名推断发布、废弃或归档状态。</p>
      </div>
    );
  }
  if (
    readState.labelVersionId !== labelVersionId
    || readState.status === "loading"
    || readState.status === "idle"
  ) {
    return (
      <div className="label-fact-state is-loading" data-testid="label-lifecycle-summary-loading" role="status">
        <strong>正在读取标签生命周期</strong>
        <p>{labelVersionId} · 等待后端返回版本状态、production generation 与替代映射。</p>
      </div>
    );
  }
  if (readState.status === "failed") {
    return (
      <div className="label-fact-state is-failed" data-testid="label-lifecycle-summary-failed" role="status">
        <strong>标签生命周期读取失败</strong>
        <p>{readState.error}。发布区仍保留，不展示推断值。</p>
        <button type="button" onClick={() => setRetryGeneration((current) => current + 1)}>重试读取</button>
      </div>
    );
  }
  return <LifecycleFacts summary={readState.summary} />;
}
