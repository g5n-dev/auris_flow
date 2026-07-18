import type { Dispatch, SetStateAction } from "react";

import { createProjectResource } from "../../api/client";
import type { AuthUser } from "../../shared/contracts/auth";
import type { ModuleKey } from "../../shared/contracts/navigation";
import type { OperationNotice } from "../../shared/contracts/operations";
import { projectIdFromName } from "./model";
import type { ProjectDraft, ProjectRow } from "./types";

type Setter<T> = Dispatch<SetStateAction<T>>;

type CreateProjectMutationInput = {
  canCreateProject: boolean;
  currentUser: AuthUser | null;
  draftProject: ProjectDraft;
  onProjectActivated: (projectName: string, projectId: string) => void;
  projectCreating: boolean;
  setActiveModule: (module: ModuleKey) => void;
  setCreateProjectOpen: Setter<boolean>;
  setDraftProject: Setter<ProjectDraft>;
  setProjectCreating: Setter<boolean>;
  setProjectNotice: Setter<OperationNotice>;
  setProjects: Setter<ProjectRow[]>;
  setSelectedProjectName: Setter<string>;
};

export function createProjectMutation(input: CreateProjectMutationInput) {
  return async (goConnector: boolean) => {
    if (!input.canCreateProject) {
      input.setProjectNotice({
        status: "error",
        title: "项目创建失败",
        detail: "项目名称和负责人是必填项。"
      });
      return;
    }
    if (input.projectCreating) return;
    const name = input.draftProject.name.trim();
    const owner = input.draftProject.owner.trim();
    const normalizedPass = input.draftProject.qualityTarget.includes("%") ? input.draftProject.qualityTarget : `${input.draftProject.qualityTarget}%`;
    const projectId = projectIdFromName(name);
    input.setProjectCreating(true);
    input.setProjectNotice({
      status: "pending",
      title: "项目创建中",
      detail: `${name} 正在写入 /api/v1/projects，成功后才会进入当前项目列表。`
    });
    try {
      const receipt = await createProjectResource({
        project_id: projectId,
        name,
        owner_name: owner,
        scene: input.draftProject.scene,
        scene_setup_mode: input.draftProject.scene === "AI 生成场景候选" ? "model_candidate" : input.draftProject.scene === "导入场景配置" ? "manifest_import" : "deferred",
        scene_objective: input.draftProject.sceneObjective.trim() || undefined,
        data_mode: input.draftProject.dataMode,
        label_binding_mode: input.draftProject.labelVersion,
        quality_target: normalizedPass,
        status: input.draftProject.dataMode === "先创建空项目" ? "pending_ingest" : "configuring",
        source: "ui_project_modal",
        next_action: goConnector ? "open_task_configuration" : "stay_project",
        members: [
          {
            user_id: input.currentUser?.userId ?? "u_admin_001",
            name: input.currentUser?.name ?? owner,
            roles: input.currentUser?.roles?.length ? input.currentUser.roles : ["project_admin"]
          }
        ],
        member_user_ids: [input.currentUser?.userId ?? "u_admin_001"]
      });
      const backendProjectId = String(receipt.data.raw.project_id ?? receipt.data.id);
      const traceId = receipt.meta?.trace_id ?? receipt.data.trace_id ?? "pending";
      input.onProjectActivated(name, backendProjectId);
      input.setProjects((current) => [
        {
          name,
          owner,
          status: input.draftProject.dataMode === "先创建空项目" ? "待接入" : "配置中",
          added: "0",
          pending: 0,
          pass: normalizedPass,
          asset: "待接入",
          projectId: backendProjectId,
          traceId,
          scene: input.draftProject.scene,
          dataMode: input.draftProject.dataMode,
          labelVersion: input.draftProject.labelVersion,
          qualityTarget: normalizedPass
        },
        ...current.filter((project) => project.projectId !== backendProjectId && project.name !== name)
      ]);
      input.setSelectedProjectName(name);
      input.setDraftProject({
        name: "",
        owner: "王敏",
        scene: "AI 生成场景候选",
        sceneObjective: "",
        dataMode: "连接器画布导入",
        labelVersion: "由 SceneProfile 候选生成",
        qualityTarget: "88%"
      });
      input.setCreateProjectOpen(false);
      input.setProjectNotice({
        status: "success",
        title: "项目已创建",
        detail: goConnector
          ? `${name} 已创建：${backendProjectId} · Trace ${traceId}，正在进入任务配置接入数据。`
          : `${name} 已创建：${backendProjectId} · Trace ${traceId}，可稍后通过任务配置接入数据。`
      });
      if (goConnector) {
        input.setActiveModule("canvas");
      }
    } catch (error) {
      input.setProjectNotice({
        status: "error",
        title: "项目创建失败",
        detail: error instanceof Error ? error.message : "后端写入失败，请重试。"
      });
    } finally {
      input.setProjectCreating(false);
    }
  };
}
