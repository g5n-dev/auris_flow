import type { Dispatch, SetStateAction } from "react";

import {
  bindProjectSceneProfile,
  createSceneProfileGenerationRun,
  publishSceneProfileVersion,
  reviewSceneProfileVersion,
  validateSceneProfileVersion,
  type ApiRuntimeContext,
  type ProjectSceneProfileBinding,
  type SceneProfileDetail,
  type SceneProfileVersion
} from "../../api/client";
import type { OperationNotice } from "../../shared/contracts/operations";
import type { SceneAction, SceneGenerationDraft } from "./types";

type Setter<T> = Dispatch<SetStateAction<T>>;

type SceneProfileMutationsInput = {
  activeSceneVersion: SceneProfileVersion | null;
  sceneApiContext: ApiRuntimeContext;
  sceneBinding: ProjectSceneProfileBinding | null;
  sceneGenerationDraft: SceneGenerationDraft;
  sceneProfiles: SceneProfileDetail[];
  sceneReviewDecision: "approved" | "rejected";
  sceneReviewReason: string;
  sceneReviewTarget: SceneProfileVersion | null;
  selectedProjectId: string;
  setProjectNotice: Setter<OperationNotice>;
  setSceneAction: Setter<SceneAction>;
  setSceneGenerateOpen: Setter<boolean>;
  setSceneRefreshNonce: Setter<number>;
  setSceneReviewReason: Setter<string>;
  setSceneReviewTarget: Setter<SceneProfileVersion | null>;
};

export function createSceneProfileMutations(input: SceneProfileMutationsInput) {
  const generateSceneCandidate = async () => {
    const refs = input.sceneGenerationDraft.inputRefs
      .split(/[\n,]/)
      .map((item) => item.trim())
      .filter(Boolean);
    if (!/^[a-z][a-z0-9_.-]{1,95}$/.test(input.sceneGenerationDraft.sceneKey.trim())) {
      input.setProjectNotice({ status: "error", title: "场景候选未提交", detail: "场景 Key 必须以小写字母开头，只能包含小写字母、数字、点、下划线和连字符。" });
      return;
    }
    if (input.sceneGenerationDraft.objective.trim().length < 10 || refs.length === 0) {
      input.setProjectNotice({ status: "error", title: "场景候选未提交", detail: "请填写至少 10 个字符的生成目标，并提供至少一个受控输入引用。" });
      return;
    }
    input.setSceneAction("generating");
    input.setProjectNotice({ status: "pending", title: "正在生成场景候选", detail: "模型只能写入 candidate；完成后仍需依赖校验、独立人审和发布。" });
    try {
      const existing = input.sceneProfiles.find((profile) => profile.scene_key === input.sceneGenerationDraft.sceneKey.trim());
      const receipt = await createSceneProfileGenerationRun(
        {
          ...(existing ? { scene_profile_id: existing.scene_profile_id } : {}),
          scene_key: input.sceneGenerationDraft.sceneKey.trim(),
          name: input.sceneGenerationDraft.name.trim(),
          description: input.sceneGenerationDraft.description.trim(),
          version: input.sceneGenerationDraft.version.trim(),
          objective: input.sceneGenerationDraft.objective.trim(),
          model_ref: input.sceneGenerationDraft.modelRef.trim(),
          input_refs: refs,
          ...(input.activeSceneVersion ? { parent_version_id: input.activeSceneVersion.scene_profile_version_id } : {})
        },
        undefined,
        input.sceneApiContext
      );
      input.setSceneGenerateOpen(false);
      input.setSceneRefreshNonce((value) => value + 1);
      input.setProjectNotice({
        status: "success",
        title: "场景生成运行已创建",
        detail: `${receipt.data.id} · ${receipt.data.status} · Trace ${receipt.meta?.trace_id ?? receipt.data.trace_id ?? "待生成"}。模型结果只会进入候选版本。`
      });
    } catch (error) {
      input.setProjectNotice({ status: "error", title: "场景生成失败", detail: error instanceof Error ? error.message : "生成运行创建失败，请重试。" });
    } finally {
      input.setSceneAction("idle");
    }
  };

  const validateSceneVersion = async (version: SceneProfileVersion) => {
    input.setSceneAction("validating");
    input.setProjectNotice({ status: "pending", title: "正在校验场景依赖", detail: `${version.version} 将校验任务、标签、Prompt、知识索引和三层评测门禁。` });
    try {
      const receipt = await validateSceneProfileVersion(version.scene_profile_version_id, undefined, input.sceneApiContext);
      input.setSceneRefreshNonce((value) => value + 1);
      input.setProjectNotice({ status: "success", title: "场景校验已完成", detail: `${version.version} · ${receipt.data.status} · Trace ${receipt.meta?.trace_id ?? receipt.data.trace_id ?? "已记录"}` });
    } catch (error) {
      input.setProjectNotice({ status: "error", title: "场景校验未通过", detail: error instanceof Error ? error.message : "依赖或评测门禁不完整。" });
    } finally {
      input.setSceneAction("idle");
    }
  };

  const reviewSceneVersion = async () => {
    if (!input.sceneReviewTarget) return;
    const reason = input.sceneReviewReason.trim();
    if (reason.length < 3) {
      input.setProjectNotice({ status: "error", title: "场景复核未提交", detail: "请输入可审计的复核理由，至少 3 个字符。" });
      return;
    }
    input.setSceneAction("reviewing");
    input.setProjectNotice({
      status: "pending",
      title: input.sceneReviewDecision === "approved" ? "正在提交独立复核" : "正在退回场景候选",
      detail: `${input.sceneReviewTarget.version} 将记录实名复核人、决定、理由和 Trace。`
    });
    try {
      const receipt = await reviewSceneProfileVersion(
        input.sceneReviewTarget.scene_profile_version_id,
        input.sceneReviewDecision,
        reason,
        undefined,
        input.sceneApiContext
      );
      input.setSceneReviewTarget(null);
      input.setSceneReviewReason("");
      input.setSceneRefreshNonce((value) => value + 1);
      input.setProjectNotice({
        status: input.sceneReviewDecision === "approved" ? "success" : "error",
        title: input.sceneReviewDecision === "approved" ? "场景候选已通过独立复核" : "场景候选已退回",
        detail: `${input.sceneReviewTarget.version} · ${receipt.data.status} · Trace ${receipt.meta?.trace_id ?? receipt.data.trace_id ?? "已记录"}`
      });
    } catch (error) {
      input.setProjectNotice({ status: "error", title: "场景复核失败", detail: error instanceof Error ? error.message : "复核写入失败，请刷新后重试。" });
    } finally {
      input.setSceneAction("idle");
    }
  };

  const publishSceneVersion = async (version: SceneProfileVersion) => {
    input.setSceneAction("publishing");
    input.setProjectNotice({ status: "pending", title: "正在发布场景版本", detail: `${version.version} 只会在独立人审和快照校验通过后发布。` });
    try {
      const receipt = await publishSceneProfileVersion(version.scene_profile_version_id, "项目工作区发布已批准场景版本", undefined, input.sceneApiContext);
      input.setSceneRefreshNonce((value) => value + 1);
      input.setProjectNotice({ status: "success", title: "场景版本已发布", detail: `${version.version} · Trace ${receipt.meta?.trace_id ?? receipt.data.trace_id ?? "已记录"}，下一步绑定项目。` });
    } catch (error) {
      input.setProjectNotice({ status: "error", title: "场景发布被阻断", detail: error instanceof Error ? error.message : "发布门禁未通过。" });
    } finally {
      input.setSceneAction("idle");
    }
  };

  const bindSceneVersion = async (version: SceneProfileVersion) => {
    input.setSceneAction("binding");
    input.setProjectNotice({ status: "pending", title: "正在绑定场景快照", detail: `${version.version} 将成为当前项目生产环境的权威场景版本。` });
    try {
      const receipt = await bindProjectSceneProfile(
        input.selectedProjectId,
        version.scene_profile_version_id,
        input.sceneBinding?.resource_version,
        undefined,
        input.sceneApiContext
      );
      input.setSceneRefreshNonce((value) => value + 1);
      input.setProjectNotice({ status: "success", title: "项目场景已绑定", detail: `${version.version} · 快照 ${receipt.data.manifest_sha256.slice(0, 10)} · Trace ${receipt.meta?.trace_id ?? receipt.data.trace_id ?? "已记录"}` });
    } catch (error) {
      input.setProjectNotice({ status: "error", title: "场景绑定失败", detail: error instanceof Error ? error.message : "绑定发生并发冲突，请刷新重试。" });
    } finally {
      input.setSceneAction("idle");
    }
  };

  return {
    bindSceneVersion,
    generateSceneCandidate,
    publishSceneVersion,
    reviewSceneVersion,
    validateSceneVersion
  };
}
