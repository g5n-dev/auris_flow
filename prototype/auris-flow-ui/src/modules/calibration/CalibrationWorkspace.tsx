import {
  Check,
  EyeOff,
  FileCheck2,
  GitCompareArrows,
  LoaderCircle,
  RefreshCw,
  Scale,
  ShieldCheck,
  UserCheck
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  adjudicateCalibrationConflict,
  claimCalibrationConflict,
  createCalibrationRound,
  createUserIntentIdempotencyKey,
  getCalibrationRound,
  listCalibrationAssignments,
  listCalibrationConflicts,
  listCalibrationRounds,
  listGoldSetVersions,
  releaseCalibrationGold,
  submitCalibrationAssignment,
  type CalibrationAssignment,
  type CalibrationConflict,
  type CalibrationGoldRelease,
  type CalibrationRound,
  type CalibrationSample,
  type GoldSetVersion
} from "../../api/client";

type NoticeState = "idle" | "pending" | "success" | "error";
type AdjudicationDecision = "accept_a" | "accept_b" | "revise" | "exclude";
type CalibrationUser = { id: string; role: string; name: string };
type AdjudicationDraft = {
  decision: AdjudicationDecision;
  reason: string;
  revisedDecision: "pass" | "fail";
};
type CreateRoundDraft = {
  datasetId: string;
  datasetVersion: string;
  labelVersion: string;
  rubricVersion: string;
  reviewerAId: string;
  reviewerBId: string;
  adjudicatorId: string;
  samplesText: string;
};

type CalibrationWorkspaceProps = {
  currentUser: CalibrationUser;
  onOpenEvidence?: (evidenceRef: string, sourceCaseId: string) => void;
};

const decisionOptions = [
  { key: "pass", label: "符合" },
  { key: "fail", label: "不符合" }
] as const;

const adjudicationOptions: Array<{ key: AdjudicationDecision; label: string }> = [
  { key: "accept_a", label: "采用 A" },
  { key: "accept_b", label: "采用 B" },
  { key: "revise", label: "修订结论" },
  { key: "exclude", label: "排除样本" }
];

const defaultSamplesText = [
  "AF-128|evidence://audio-session/S20250526-000128/quote",
  "AF-129|evidence://audio-session/S20250526-000129/trial",
  "AF-130|evidence://audio-session/S20250526-000130/intent",
  "AF-131|evidence://audio-session/S20250526-000131/risk"
].join("\n");

const shortTrace = (trace?: string) => (trace ? trace.slice(0, 16) : "-");
const formatPercent = (ppm?: number) =>
  typeof ppm === "number" ? `${(ppm / 10_000).toFixed(1)}%` : "--";
const formatKappa = (round: CalibrationRound) => {
  if (round.cohen_kappa_defined === false) return "N/A";
  return typeof round.cohen_kappa_micros === "number"
    ? (round.cohen_kappa_micros / 1_000_000).toFixed(2)
    : "--";
};
const roleLabel = (role: CalibrationRound["my_role"]) => {
  if (role === "reviewer_a" || role === "reviewer_b") return "盲审评审者";
  if (role === "adjudicator") return "指定仲裁者";
  return "只读观察者";
};
const isManagerRole = (role: string) => /管理员|负责人|主管|仲裁/.test(role);

function displaySubmissionValue(value: unknown) {
  if (value && typeof value === "object" && "decision" in value) {
    const decision = (value as { decision?: unknown }).decision;
    if (decision === "pass") return "符合";
    if (decision === "fail") return "不符合";
  }
  return "结构化结论";
}

function parseSamples(value: string): { samples: CalibrationSample[]; error: string } {
  const rows = value
    .split("\n")
    .map((row) => row.trim())
    .filter(Boolean);
  const samples: CalibrationSample[] = [];
  for (const [index, row] of rows.entries()) {
    const separator = row.indexOf("|");
    const sourceCaseId = separator >= 0 ? row.slice(0, separator).trim() : "";
    const evidenceRef = separator >= 0 ? row.slice(separator + 1).trim() : "";
    if (!sourceCaseId || !evidenceRef) {
      return { samples: [], error: `第 ${index + 1} 行应使用“样本ID|证据引用”格式。` };
    }
    samples.push({ source_case_id: sourceCaseId, evidence_ref: evidenceRef });
  }
  if (!samples.length) return { samples: [], error: "至少配置 1 个校准样本。" };
  if (new Set(samples.map((sample) => sample.source_case_id)).size !== samples.length) {
    return { samples: [], error: "同一批次中的样本 ID 不能重复。" };
  }
  return { samples, error: "" };
}

export default function CalibrationWorkspace({ currentUser, onOpenEvidence }: CalibrationWorkspaceProps) {
  const [rounds, setRounds] = useState<CalibrationRound[]>([]);
  const [selectedRoundId, setSelectedRoundId] = useState("");
  const [roundDetail, setRoundDetail] = useState<CalibrationRound | null>(null);
  const [assignments, setAssignments] = useState<CalibrationAssignment[]>([]);
  const [conflicts, setConflicts] = useState<CalibrationConflict[]>([]);
  const [decisions, setDecisions] = useState<Record<string, "pass" | "fail">>({});
  const [adjudicationDrafts, setAdjudicationDrafts] = useState<Record<string, AdjudicationDraft>>({});
  const [claimedVersions, setClaimedVersions] = useState<Record<string, number>>({});
  const [goldRelease, setGoldRelease] = useState<CalibrationGoldRelease | null>(null);
  const [goldVersions, setGoldVersions] = useState<GoldSetVersion[]>([]);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [createFormOpen, setCreateFormOpen] = useState(false);
  const [createDraft, setCreateDraft] = useState<CreateRoundDraft>({
    datasetId: "evalset_quote_risk_v12",
    datasetVersion: "v12",
    labelVersion: "label_v1_8_4",
    rubricVersion: "rubric_quote_risk_v3",
    reviewerAId: "u_annotator_001",
    reviewerBId: "u_annotator_002",
    adjudicatorId: currentUser.id,
    samplesText: defaultSamplesText
  });
  const [notice, setNotice] = useState<{
    status: NoticeState;
    title: string;
    detail: string;
  }>({
    status: "idle",
    title: "盲审校准待开始",
    detail: "当前登录身份由统一会话提供；评审答案不会在浏览器中跨身份读取。"
  });
  const claimIntentKeys = useRef<Record<string, string>>({});
  const adjudicationIntentKeys = useRef<Record<string, string>>({});

  const canManageCalibration = isManagerRole(currentUser.role);
  const roundRole = roundDetail?.my_role ?? "observer";
  const isReviewer = roundRole === "reviewer_a" || roundRole === "reviewer_b";
  const isAdjudicator = roundRole === "adjudicator";
  const canViewOutcomeMetrics = Boolean(roundDetail && !isReviewer && (canManageCalibration || isAdjudicator));
  const pendingAssignments = assignments.filter((item) => item.status === "pending").length;
  const submittedAssignments = assignments.length - pendingAssignments;
  const selectedConflict = conflicts[0] ?? null;
  const selectedAdjudicationDraft = selectedConflict
    ? adjudicationDrafts[selectedConflict.item_id] ?? {
        decision: "accept_a" as const,
        reason: "",
        revisedDecision: "pass" as const
      }
    : null;
  const selectedClaimedVersion = selectedConflict
    ? claimedVersions[selectedConflict.item_id]
    : undefined;

  const managerProgress = useMemo(() => {
    if (!roundDetail?.sample_count || typeof roundDetail.paired_submission_count !== "number") return 0;
    return Math.round((roundDetail.paired_submission_count / roundDetail.sample_count) * 100);
  }, [roundDetail]);

  const refreshWorkspace = async (preferredRoundId?: string, quiet = false) => {
    if (!quiet) {
      setNotice({ status: "pending", title: "正在刷新校准状态", detail: `${currentUser.name} / ${currentUser.role}` });
    }
    try {
      const roundResponse = await listCalibrationRounds();
      const nextRounds = roundResponse.data.items ?? [];
      const targetRoundId =
        preferredRoundId && nextRounds.some((item) => item.round_id === preferredRoundId)
          ? preferredRoundId
          : nextRounds[0]?.round_id ?? "";
      setRounds(nextRounds);
      setSelectedRoundId(targetRoundId);
      setAssignments([]);
      setConflicts([]);
      if (!targetRoundId) {
        setRoundDetail(null);
        if (!quiet) {
          setNotice({
            status: "idle",
            title: "暂无可见校准批次",
            detail: canManageCalibration ? "可创建首个盲审批次。" : "等待项目管理员分配校准任务。"
          });
        }
        return;
      }
      const detailResponse = await getCalibrationRound(targetRoundId);
      const detail = detailResponse.data;
      setRoundDetail(detail);
      if (detail.my_role === "reviewer_a" || detail.my_role === "reviewer_b") {
        const assignmentResponse = await listCalibrationAssignments(targetRoundId);
        setAssignments(assignmentResponse.data.items ?? []);
      } else if (detail.my_role === "adjudicator") {
        const conflictResponse = await listCalibrationConflicts(targetRoundId);
        setConflicts(conflictResponse.data.items ?? []);
      }
      if (detail.my_role === "adjudicator" || canManageCalibration) {
        const goldResponse = await listGoldSetVersions("quote-risk-gold");
        setGoldVersions(goldResponse.data.items ?? []);
      } else {
        setGoldVersions([]);
      }
      if (!quiet) {
        setNotice({
          status: "success",
          title: "校准状态已刷新",
          detail: `${targetRoundId} / ${roleLabel(detail.my_role)} / trace ${shortTrace(detail.current_trace_id)}`
        });
      }
    } catch (error) {
      setNotice({
        status: "error",
        title: "校准状态加载失败",
        detail: error instanceof Error ? error.message : "BFF 请求失败"
      });
    }
  };

  useEffect(() => {
    setGoldRelease(null);
    setDecisions({});
    setClaimedVersions({});
    setCreateDraft((current) => ({ ...current, adjudicatorId: currentUser.id }));
    claimIntentKeys.current = {};
    adjudicationIntentKeys.current = {};
    void refreshWorkspace(undefined, true);
  }, [currentUser.id]);

  const selectRound = async (roundId: string) => {
    setSelectedRoundId(roundId);
    setGoldRelease(null);
    await refreshWorkspace(roundId);
  };

  const updateCreateDraft = (key: keyof CreateRoundDraft, value: string) => {
    setCreateDraft((current) => ({ ...current, [key]: value }));
  };

  const createRound = async () => {
    if (!canManageCalibration) return;
    const required = [
      createDraft.datasetId,
      createDraft.datasetVersion,
      createDraft.labelVersion,
      createDraft.rubricVersion,
      createDraft.reviewerAId,
      createDraft.reviewerBId,
      createDraft.adjudicatorId
    ].every((value) => value.trim());
    if (!required) {
      setNotice({ status: "error", title: "校准批次配置不完整", detail: "数据集、版本和三位参与者均为必填。" });
      return;
    }
    const participantIds = [
      createDraft.reviewerAId.trim(),
      createDraft.reviewerBId.trim(),
      createDraft.adjudicatorId.trim()
    ];
    if (new Set(participantIds).size !== 3) {
      setNotice({ status: "error", title: "参与者配置冲突", detail: "评审 A、评审 B 与仲裁者必须是三个不同用户。" });
      return;
    }
    const parsed = parseSamples(createDraft.samplesText);
    if (parsed.error) {
      setNotice({ status: "error", title: "校准样本格式错误", detail: parsed.error });
      return;
    }
    setBusyAction("create-round");
    setNotice({
      status: "pending",
      title: "正在冻结盲审输入",
      detail: `${parsed.samples.length} 个样本、参与者与 rubric 将形成不可变 manifest。`
    });
    try {
      const response = await createCalibrationRound(
        {
          dataset_id: createDraft.datasetId.trim(),
          dataset_version: createDraft.datasetVersion.trim(),
          label_version: createDraft.labelVersion.trim(),
          rubric_version: createDraft.rubricVersion.trim(),
          reviewer_ids: [createDraft.reviewerAId.trim(), createDraft.reviewerBId.trim()],
          adjudicator_id: createDraft.adjudicatorId.trim(),
          samples: parsed.samples
        },
        { idempotencyKey: createUserIntentIdempotencyKey("calibration_round") }
      );
      setCreateFormOpen(false);
      setNotice({
        status: "success",
        title: "盲审批次已创建",
        detail: `${response.data.round_id} / ${parsed.samples.length * 2} 个独立 assignment / trace ${shortTrace(response.meta?.trace_id)}`
      });
      await refreshWorkspace(response.data.round_id, true);
    } catch (error) {
      setNotice({
        status: "error",
        title: "校准批次创建失败",
        detail: error instanceof Error ? error.message : "BFF 请求失败"
      });
    } finally {
      setBusyAction(null);
    }
  };

  const submitAssignment = async (assignment: CalibrationAssignment) => {
    const decision = decisions[assignment.assignment_id];
    if (!decision) {
      setNotice({ status: "error", title: "请选择校准结论", detail: assignment.source_case_id });
      return;
    }
    const action = `submit-${assignment.assignment_id}`;
    setBusyAction(action);
    setNotice({
      status: "pending",
      title: "正在密封评审结论",
      detail: "提交后不可覆盖；响应不会返回另一位评审者的答案或聚合结果。"
    });
    try {
      const response = await submitCalibrationAssignment(
        assignment.assignment_id,
        { value: { decision }, expected_resource_version: assignment.resource_version },
        { idempotencyKey: createUserIntentIdempotencyKey(`calibration_submission_${assignment.assignment_id}`) }
      );
      setNotice({
        status: "success",
        title: "盲审结论已密封",
        detail: `${assignment.source_case_id} / trace ${shortTrace(response.meta?.trace_id)}`
      });
      await refreshWorkspace(assignment.round_id, true);
    } catch (error) {
      setNotice({
        status: "error",
        title: "盲审提交失败",
        detail: error instanceof Error ? error.message : "BFF 请求失败"
      });
    } finally {
      setBusyAction(null);
    }
  };

  const updateAdjudicationDraft = (itemId: string, patch: Partial<AdjudicationDraft>) => {
    setAdjudicationDrafts((current) => {
      const existing = current[itemId] ?? {
        decision: "accept_a",
        reason: "",
        revisedDecision: "pass"
      };
      return { ...current, [itemId]: { ...existing, ...patch } };
    });
  };

  const claimConflict = async (conflict: CalibrationConflict) => {
    const action = `claim-${conflict.item_id}`;
    setBusyAction(action);
    setNotice({
      status: "pending",
      title: "正在领取冲突",
      detail: `${conflict.source_case_id} / 乐观锁版本 ${conflict.resource_version}`
    });
    try {
      claimIntentKeys.current[conflict.item_id] ??=
        createUserIntentIdempotencyKey(`calibration_claim_${conflict.item_id}`);
      const response = await claimCalibrationConflict(
        conflict.item_id,
        conflict.resource_version,
        { idempotencyKey: claimIntentKeys.current[conflict.item_id] }
      );
      const version = Number(response.data.resource_version);
      if (!Number.isInteger(version) || version < 1) throw new Error("领取响应缺少有效 resource_version");
      setClaimedVersions((current) => ({ ...current, [conflict.item_id]: version }));
      setNotice({
        status: "success",
        title: "冲突已领取",
        detail: `${conflict.source_case_id} / claimed v${version}；裁决失败时可直接重试提交。`
      });
    } catch (error) {
      setNotice({
        status: "error",
        title: "冲突领取失败",
        detail: error instanceof Error ? error.message : "BFF 请求失败"
      });
    } finally {
      setBusyAction(null);
    }
  };

  const submitAdjudication = async (conflict: CalibrationConflict) => {
    const claimedVersion = claimedVersions[conflict.item_id];
    const draft = adjudicationDrafts[conflict.item_id] ?? {
      decision: "accept_a" as const,
      reason: "",
      revisedDecision: "pass" as const
    };
    if (!claimedVersion) {
      setNotice({ status: "error", title: "请先领取冲突", detail: "领取成功后系统会保留裁决所需的资源版本。" });
      return;
    }
    if (draft.reason.trim().length < 4) {
      setNotice({ status: "error", title: "请补充裁决原因", detail: "至少 4 个字符，用于审计与后续校准回放。" });
      return;
    }
    const action = `adjudicate-${conflict.item_id}`;
    setBusyAction(action);
    setNotice({
      status: "pending",
      title: "正在提交不可变裁决",
      detail: `${conflict.source_case_id} / 使用已领取版本 v${claimedVersion}`
    });
    try {
      adjudicationIntentKeys.current[conflict.item_id] ??=
        createUserIntentIdempotencyKey(`calibration_adjudication_${conflict.item_id}`);
      const response = await adjudicateCalibrationConflict(
        conflict.item_id,
        {
          decision: draft.decision,
          reason: draft.reason.trim(),
          expected_resource_version: claimedVersion,
          ...(draft.decision === "revise" ? { value: { decision: draft.revisedDecision } } : {})
        },
        { idempotencyKey: adjudicationIntentKeys.current[conflict.item_id] }
      );
      setClaimedVersions((current) => {
        const next = { ...current };
        delete next[conflict.item_id];
        return next;
      });
      delete claimIntentKeys.current[conflict.item_id];
      delete adjudicationIntentKeys.current[conflict.item_id];
      setNotice({
        status: "success",
        title: "冲突已形成不可变裁决",
        detail: `${conflict.source_case_id} / ${draft.decision} / trace ${shortTrace(response.meta?.trace_id)}`
      });
      await refreshWorkspace(conflict.round_id, true);
    } catch (error) {
      setNotice({
        status: "error",
        title: "冲突裁决失败，可直接重试",
        detail: `${error instanceof Error ? error.message : "BFF 请求失败"}；已保留 claimed v${claimedVersion}，不会重复领取。`
      });
    } finally {
      setBusyAction(null);
    }
  };

  const publishGold = async () => {
    if (!roundDetail || !isAdjudicator) return;
    setBusyAction("release-gold");
    setNotice({ status: "pending", title: "正在发布金标版本", detail: "发布只追加新版本，不覆盖历史 annotation。" });
    try {
      const response = await releaseCalibrationGold(
        roundDetail.round_id,
        { gold_set_key: "quote-risk-gold", expected_resource_version: roundDetail.resource_version },
        { idempotencyKey: createUserIntentIdempotencyKey(`calibration_gold_${roundDetail.round_id}`) }
      );
      setGoldRelease(response.data);
      setNotice({
        status: "success",
        title: `金标 v${response.data.version_number} 已发布`,
        detail: `${response.data.annotation_count} 条 annotation / trace ${shortTrace(response.data.trace_id)}`
      });
      await refreshWorkspace(roundDetail.round_id, true);
    } catch (error) {
      setNotice({ status: "error", title: "金标发布失败", detail: error instanceof Error ? error.message : "BFF 请求失败" });
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <div className="calibration-workspace wide" data-testid="calibration-workspace">
      <section className={`operation-toast calibration-notice is-${notice.status}`} role="status" aria-live="polite">
        <strong>{notice.title}</strong>
        <span>{notice.detail}</span>
      </section>

      <section className="module-panel calibration-control-bar">
        <div className="calibration-heading">
          <div className="panel-icon"><GitCompareArrows size={16} /></div>
          <div><strong>盲审校准</strong><span>A/B 独立判断 → 第三方仲裁 → 不可变金标版本</span></div>
        </div>
        <div className="calibration-current-identity" aria-label="当前校准身份">
          <UserCheck size={14} />
          <span>当前身份</span>
          <strong>{currentUser.name}</strong>
          <em>{currentUser.role} · {currentUser.id}</em>
        </div>
        <div className="calibration-toolbar-actions">
          <button type="button" onClick={() => void refreshWorkspace(selectedRoundId)}>
            <RefreshCw size={14} /> 刷新
          </button>
          {canManageCalibration && (
            <button type="button" className="primary" onClick={() => setCreateFormOpen((open) => !open)}>
              <FileCheck2 size={14} /> {createFormOpen ? "收起批次配置" : "创建校准批次"}
            </button>
          )}
        </div>
      </section>

      {createFormOpen && canManageCalibration && (
        <section className="module-panel calibration-create-panel">
          <div className="calibration-section-heading">
            <div><strong>新建校准批次</strong><span>参与者与样本为显式表单字段，提交后冻结为 manifest</span></div>
            <b>管理动作</b>
          </div>
          <div className="calibration-create-grid">
            <label><span>数据集 ID</span><input value={createDraft.datasetId} onChange={(event) => updateCreateDraft("datasetId", event.target.value)} /></label>
            <label><span>数据集版本</span><input value={createDraft.datasetVersion} onChange={(event) => updateCreateDraft("datasetVersion", event.target.value)} /></label>
            <label><span>标签版本</span><input value={createDraft.labelVersion} onChange={(event) => updateCreateDraft("labelVersion", event.target.value)} /></label>
            <label><span>Rubric 版本</span><input value={createDraft.rubricVersion} onChange={(event) => updateCreateDraft("rubricVersion", event.target.value)} /></label>
            <label><span>评审 A 用户 ID</span><input value={createDraft.reviewerAId} onChange={(event) => updateCreateDraft("reviewerAId", event.target.value)} /></label>
            <label><span>评审 B 用户 ID</span><input value={createDraft.reviewerBId} onChange={(event) => updateCreateDraft("reviewerBId", event.target.value)} /></label>
            <label><span>仲裁者用户 ID</span><input value={createDraft.adjudicatorId} onChange={(event) => updateCreateDraft("adjudicatorId", event.target.value)} /></label>
            <label className="calibration-sample-input"><span>样本清单（每行：样本ID|证据引用）</span><textarea rows={5} value={createDraft.samplesText} onChange={(event) => updateCreateDraft("samplesText", event.target.value)} /></label>
          </div>
          <div className="calibration-create-actions">
            <button type="button" onClick={() => setCreateFormOpen(false)}>取消</button>
            <button type="button" className="primary" disabled={busyAction === "create-round"} onClick={() => void createRound()}>
              {busyAction === "create-round" ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />} 冻结并创建
            </button>
          </div>
        </section>
      )}

      <section className="module-panel calibration-round-list">
        <div className="calibration-section-heading">
          <div><strong>校准批次</strong><span>列表仅返回当前登录身份有权查看的轮次</span></div>
          <b>{rounds.length} 轮</b>
        </div>
        {rounds.length ? (
          <div className="calibration-round-buttons">
            {rounds.map((round) => {
              const reviewerRound = round.my_role === "reviewer_a" || round.my_role === "reviewer_b";
              return (
                <button type="button" key={round.round_id} className={round.round_id === selectedRoundId ? "active" : ""} onClick={() => void selectRound(round.round_id)}>
                  <span>{reviewerRound ? "我的盲审" : round.status === "in_review" ? "盲审中" : round.status === "ready" ? "待发布" : "已发布"}</span>
                  <strong>{round.dataset_id}</strong>
                  <em>{round.round_id}</em>
                  <b>{reviewerRound || typeof round.paired_submission_count !== "number" ? `${round.sample_count} 样本` : `${round.paired_submission_count}/${round.sample_count} 配对`}</b>
                </button>
              );
            })}
          </div>
        ) : (
          <div className="calibration-empty">
            <EyeOff size={22} />
            <strong>{canManageCalibration ? "尚未创建校准批次" : "当前没有分配给你的盲审任务"}</strong>
            <span>空状态来自 BFF，不在前端切换或伪造其他身份。</span>
          </div>
        )}
      </section>

      {roundDetail && isReviewer && (
        <section className="module-panel calibration-reviewer-summary">
          <div className="calibration-section-heading">
            <div><strong>我的提交进度</strong><span>{roundDetail.dataset_version} / {roundDetail.rubric_version}</span></div>
            <b><EyeOff size={13} /> 同伴答案与聚合结果隐藏</b>
          </div>
          <div className="calibration-reviewer-facts">
            <div><span>我的角色</span><strong>{roleLabel(roundRole)}</strong></div>
            <div><span>已提交</span><strong>{submittedAssignments}/{assignments.length || roundDetail.sample_count}</strong></div>
            <div><span>待提交</span><strong>{pendingAssignments}</strong></div>
            <div><span>输入状态</span><strong>{roundDetail.sealed ? "已冻结" : "只读"}</strong></div>
          </div>
          <div className="calibration-trace-line">
            <ShieldCheck size={14} /><span>trace {shortTrace(roundDetail.current_trace_id)}</span>
            <span>manifest {roundDetail.sample_manifest_sha256.slice(0, 12)}</span>
          </div>
        </section>
      )}

      {roundDetail && canViewOutcomeMetrics && (
        <section className="module-panel calibration-metric-panel">
          <div className="calibration-section-heading">
            <div><strong>校准质量</strong><span>{roundDetail.dataset_version} / {roundDetail.rubric_version}</span></div>
            <b className={`calibration-status ${roundDetail.status}`}>{roundDetail.status}</b>
          </div>
          <div className="calibration-metrics">
            <div><span>配对完成</span><strong>{roundDetail.paired_submission_count ?? "--"}/{roundDetail.sample_count}</strong><em>{managerProgress}%</em></div>
            <div><span>观察一致率</span><strong>{formatPercent(roundDetail.observed_agreement_ppm)}</strong><em>integer ppm</em></div>
            <div><span>Cohen κ</span><strong>{formatKappa(roundDetail)}</strong><em>{roundDetail.cohen_kappa_defined === false ? "not applicable" : "chance-corrected"}</em></div>
            <div><span>冲突 / 仲裁</span><strong>{roundDetail.conflict_count ?? "--"} / {roundDetail.adjudication_count ?? "--"}</strong><em>第三方处理</em></div>
          </div>
          <div className="calibration-progress" aria-label={`校准配对进度 ${managerProgress}%`}><i style={{ width: `${managerProgress}%` }} /></div>
          <div className="calibration-trace-line">
            <ShieldCheck size={14} /><span>trace {shortTrace(roundDetail.current_trace_id)}</span>
            <span>manifest {roundDetail.sample_manifest_sha256.slice(0, 12)}</span>
            <b>{roleLabel(roundRole)}</b>
          </div>
        </section>
      )}

      {roundDetail && isReviewer && (
        <section className="module-panel calibration-assignment-panel">
          <div className="calibration-section-heading">
            <div><strong>我的盲审任务</strong><span>只返回当前登录用户的 assignment，提交后不可修改</span></div>
            <div className="calibration-heading-actions"><b>{pendingAssignments} 待提交</b></div>
          </div>
          <div className="calibration-assignment-list">
            {assignments.map((assignment) => (
              <article key={assignment.assignment_id} data-testid="calibration-assignment">
                <div className="calibration-assignment-index">{assignment.ordinal + 1}</div>
                <div className="calibration-assignment-copy"><span>{assignment.source_case_id}</span><strong>证据样本 #{assignment.ordinal + 1}</strong><em>{assignment.evidence_ref}</em></div>
                <button type="button" className="calibration-evidence-link" onClick={() => onOpenEvidence?.(assignment.evidence_ref, assignment.source_case_id)}>查看证据</button>
                <div className="calibration-decision-control">
                  {decisionOptions.map((option) => (
                    <button type="button" key={option.key} className={decisions[assignment.assignment_id] === option.key ? "active" : ""} disabled={assignment.status !== "pending"} onClick={() => setDecisions((current) => ({ ...current, [assignment.assignment_id]: option.key }))}>{option.label}</button>
                  ))}
                  <button type="button" className="primary" disabled={assignment.status !== "pending" || busyAction === `submit-${assignment.assignment_id}`} onClick={() => void submitAssignment(assignment)}>
                    {assignment.status === "submitted" ? <><Check size={13} /> 已密封</> : "提交结论"}
                  </button>
                </div>
              </article>
            ))}
          </div>
        </section>
      )}

      {roundDetail && isAdjudicator && (
        <>
          <section className="module-panel calibration-conflict-panel">
            <div className="calibration-section-heading"><div><strong>冲突仲裁</strong><span>仅指定仲裁者可读取匿名 A/B 结论</span></div><b>{conflicts.length} 条冲突</b></div>
            {selectedConflict && selectedAdjudicationDraft ? (
              <div className="calibration-conflict-card" data-testid="calibration-conflict">
                <div className="calibration-conflict-evidence">
                  <span>{selectedConflict.source_case_id}</span><strong>匿名结论不一致</strong><em>{selectedConflict.evidence_ref}</em>
                  <button type="button" onClick={() => onOpenEvidence?.(selectedConflict.evidence_ref, selectedConflict.source_case_id)}>打开完整证据</button>
                </div>
                <div className="calibration-anonymous-submissions">
                  {selectedConflict.submissions.map((submission) => (
                    <div key={submission.submission_id}><span>匿名提交 {submission.slot}</span><strong>{displaySubmissionValue(submission.value)}</strong><em>{new Date(submission.submitted_at).toLocaleTimeString("zh-CN", { hour12: false })}</em></div>
                  ))}
                </div>
                <div className="calibration-adjudication-form">
                  <span>裁决动作</span>
                  <div className="calibration-adjudication-options">
                    {adjudicationOptions.map((option) => (
                      <button type="button" key={option.key} className={selectedAdjudicationDraft.decision === option.key ? "active" : ""} onClick={() => updateAdjudicationDraft(selectedConflict.item_id, { decision: option.key })}>{option.label}</button>
                    ))}
                  </div>
                  {selectedAdjudicationDraft.decision === "revise" && (
                    <label><span>修订值</span><select value={selectedAdjudicationDraft.revisedDecision} onChange={(event) => updateAdjudicationDraft(selectedConflict.item_id, { revisedDecision: event.target.value as "pass" | "fail" })}><option value="pass">符合</option><option value="fail">不符合</option></select></label>
                  )}
                  <label><span>裁决原因 <b>必填</b></span><textarea rows={3} value={selectedAdjudicationDraft.reason} onChange={(event) => updateAdjudicationDraft(selectedConflict.item_id, { reason: event.target.value })} placeholder="说明采用、修订或排除的证据依据，写入审计记录。" /></label>
                  <div className="calibration-claim-status">
                    <ShieldCheck size={14} />
                    <span>{selectedClaimedVersion ? `已领取资源版本 v${selectedClaimedVersion}，失败可直接重试裁决` : selectedConflict.adjudication_claimed ? "该冲突已有领取记录，可尝试恢复领取" : "提交裁决前必须先领取冲突"}</span>
                  </div>
                  <div className="calibration-adjudication-actions">
                    <button type="button" disabled={Boolean(selectedClaimedVersion) || busyAction === `claim-${selectedConflict.item_id}`} onClick={() => void claimConflict(selectedConflict)}>
                      {busyAction === `claim-${selectedConflict.item_id}` ? <LoaderCircle className="spin" size={14} /> : <Scale size={14} />} {selectedConflict.adjudication_claimed ? "恢复领取" : "领取冲突"}
                    </button>
                    <button type="button" className="primary" disabled={!selectedClaimedVersion || selectedAdjudicationDraft.reason.trim().length < 4 || busyAction === `adjudicate-${selectedConflict.item_id}`} onClick={() => void submitAdjudication(selectedConflict)}>
                      {busyAction === `adjudicate-${selectedConflict.item_id}` ? <LoaderCircle className="spin" size={14} /> : <FileCheck2 size={14} />} 提交裁决
                    </button>
                  </div>
                </div>
              </div>
            ) : (
              <div className="calibration-empty compact"><UserCheck size={20} /><strong>{roundDetail.status === "in_review" ? "等待 A/B 完成配对" : "没有待裁决冲突"}</strong><span>一致样本直接形成候选金标，冲突样本必须由指定仲裁者裁决。</span></div>
            )}
          </section>

          <section className="module-panel calibration-release-panel">
            <div className="calibration-section-heading"><div><strong>金标发布</strong><span>版本追加发布，历史 annotation 不可覆盖</span></div><FileCheck2 size={17} /></div>
            <div className="calibration-release-facts">
              <div><span>发布条件</span><strong>状态 ready</strong></div>
              <div><span>当前状态</span><strong>{roundDetail.status}</strong></div>
              <div><span>有效 annotation</span><strong>{roundDetail.sample_count - (roundDetail.excluded_count ?? 0)}</strong></div>
            </div>
            {goldRelease && (
              <div className="calibration-gold-receipt" data-testid="calibration-gold-receipt"><Check size={15} /><div><strong>{goldRelease.gold_set_key} v{goldRelease.version_number}</strong><span>{goldRelease.gold_set_version_id}</span></div><em>trace {shortTrace(goldRelease.trace_id)}</em></div>
            )}
            {goldVersions.length > 0 && (
              <div className="calibration-gold-history" aria-label="已发布金标版本">
                <span>已发布历史</span>
                {goldVersions.slice(0, 4).map((version) => (
                  <div key={version.gold_set_version_id}>
                    <strong>{version.gold_set_key} v{version.version_number}</strong>
                    <em>{version.annotation_count}/{version.sample_count} 条 · trace {shortTrace(version.trace_id)}</em>
                  </div>
                ))}
              </div>
            )}
            <button type="button" className="primary calibration-release-button" disabled={roundDetail.status !== "ready" || busyAction === "release-gold"} onClick={() => void publishGold()}>
              {busyAction === "release-gold" ? <LoaderCircle className="spin" size={14} /> : <FileCheck2 size={14} />} {roundDetail.status === "published" ? "本轮已发布" : roundDetail.status === "ready" ? "发布新金标版本" : "完成配对与仲裁后可发布"}
            </button>
          </section>
        </>
      )}

      {roundDetail && !isReviewer && !isAdjudicator && (
        <section className="module-panel calibration-observer-panel">
          <div className="calibration-empty compact"><EyeOff size={20} /><strong>当前批次为只读观察</strong><span>管理角色可查看质量汇总，但匿名提交、领取、裁决和发布仅对指定参与者开放。</span></div>
        </section>
      )}
    </div>
  );
}
