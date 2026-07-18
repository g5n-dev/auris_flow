import { useEffect, useState } from "react";

import {
  getProjectSceneProfile,
  getSceneProfile,
  listSceneProfiles,
  type ApiRuntimeContext,
  type ProjectSceneProfileBinding,
  type SceneProfileDetail,
  type SceneProfileVersion
} from "../../api/client";
import type { OperationNotice } from "../../shared/contracts/operations";
import {
  automotiveDemoProfiles,
  projectRows
} from "./fixtures";
import { filterProjects, normalizeProjectProjectionItems } from "./model";
import { createProjectMutation } from "./projectMutation";
import { createSceneProfileMutations } from "./sceneProfileMutations";
import {
  deriveProjectLabelRows,
  deriveProjectMembers,
  deriveProjectQualityRows,
  deriveProjectSources,
  deriveSelectedProfile
} from "./selectors";
import type {
  ProjectDraft,
  ProjectModuleProps,
  ProjectRow,
  ProjectStatusFilter,
  SceneAction,
  SceneGenerationDraft
} from "./types";

export function useProjectWorkspace({
  activeTab,
  setActiveModule,
  currentUser,
  onProjectActivated,
  apiContext,
  projectionItems,
  projectionSource
}: ProjectModuleProps) {
  const initialProjects = projectionSource === "bff"
    ? normalizeProjectProjectionItems(projectionItems ?? [])
    : projectRows;
  const [projects, setProjects] = useState<ProjectRow[]>(initialProjects);
  const [selectedProjectName, setSelectedProjectName] = useState(initialProjects[0]?.name ?? "");
  const [projectQuery, setProjectQuery] = useState("");
  const [projectStatusFilter, setProjectStatusFilter] = useState<ProjectStatusFilter>("all");
  const [createProjectOpen, setCreateProjectOpen] = useState(false);
  const [projectCreating, setProjectCreating] = useState(false);
  const [projectNotice, setProjectNotice] = useState<OperationNotice>({
    status: "idle",
    title: "等待项目操作",
    detail: "项目创建、筛选和数据接入入口会记录为当前项目工作区回执。"
  });
  const [sceneProfiles, setSceneProfiles] = useState<SceneProfileDetail[]>([]);
  const [sceneBinding, setSceneBinding] = useState<ProjectSceneProfileBinding | null>(null);
  const [sceneLoading, setSceneLoading] = useState(true);
  const [sceneRefreshNonce, setSceneRefreshNonce] = useState(0);
  const [sceneAction, setSceneAction] = useState<SceneAction>("idle");
  const [sceneGenerateOpen, setSceneGenerateOpen] = useState(false);
  const [sceneReviewTarget, setSceneReviewTarget] = useState<SceneProfileVersion | null>(null);
  const [sceneReviewDecision, setSceneReviewDecision] = useState<"approved" | "rejected">("approved");
  const [sceneReviewReason, setSceneReviewReason] = useState("");
  const [sceneGenerationDraft, setSceneGenerationDraft] = useState<SceneGenerationDraft>({
    sceneKey: "",
    name: "",
    description: "",
    version: "v1.0.0-rc1",
    objective: "",
    modelRef: "model:scene-planner/v1",
    inputRefs: ""
  });
  const [draftProject, setDraftProject] = useState<ProjectDraft>({
    name: "",
    owner: "王敏",
    scene: "AI 生成场景候选",
    sceneObjective: "",
    dataMode: "连接器画布导入",
    labelVersion: "由 SceneProfile 候选生成",
    qualityTarget: "88%"
  });

  useEffect(() => {
    const nextProjects = projectionSource === "bff"
      ? normalizeProjectProjectionItems(projectionItems ?? [])
      : projectRows;
    setProjects(nextProjects);
    setSelectedProjectName((current) => nextProjects.some((project) => project.name === current)
      ? current
      : nextProjects[0]?.name ?? "");
  }, [projectionItems, projectionSource]);

  const selectedProject = projects.find((project) => project.name === selectedProjectName) ?? projects[0];
  const selectedProjectId = selectedProject?.projectId ?? String(apiContext.projectId ?? "");
  const sceneApiContext: ApiRuntimeContext = { ...apiContext, projectId: selectedProjectId };
  useEffect(() => {
    if (!selectedProjectId) {
      setSceneProfiles([]);
      setSceneBinding(null);
      setSceneLoading(false);
      return;
    }
    let disposed = false;
    setSceneLoading(true);
    void listSceneProfiles(sceneApiContext)
      .then(async (response) => {
        const summaries = response.data.items ?? [];
        const details = await Promise.all(
          summaries.map((profile) => getSceneProfile(profile.scene_profile_id, sceneApiContext).then((item) => item.data))
        );
        let binding: ProjectSceneProfileBinding | null = null;
        try {
          binding = (await getProjectSceneProfile(selectedProjectId, "production", sceneApiContext)).data;
        } catch {
          binding = null;
        }
        if (disposed) return;
        setSceneProfiles(details);
        setSceneBinding(binding);
      })
      .catch((error) => {
        if (disposed) return;
        setSceneProfiles([]);
        setSceneBinding(null);
        setProjectNotice({
          status: "error",
          title: "场景配置读取失败",
          detail: error instanceof Error ? error.message : "无法读取当前项目的 SceneProfile。"
        });
      })
      .finally(() => {
        if (!disposed) setSceneLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [selectedProjectId, sceneRefreshNonce, apiContext.tenantId]);

  const sceneVersions = sceneProfiles.flatMap((profile) => profile.versions);
  const pendingSceneVersions = sceneVersions.filter((version) => version.status !== "published" && version.status !== "deprecated");
  const activeSceneVersion = sceneBinding?.version ?? null;
  const activeSceneManifest = activeSceneVersion?.manifest ?? null;
  const demoProfile = projectionSource === "mock" ? automotiveDemoProfiles[selectedProject?.name] : undefined;
  const selectedProfile = deriveSelectedProfile(activeSceneManifest, activeSceneVersion, demoProfile);
  const filteredProjectRows = filterProjects(projects, projectQuery, projectStatusFilter);
  const projectSources = deriveProjectSources(activeSceneManifest, sceneBinding, projectionSource, selectedProject);
  const projectMembers = deriveProjectMembers(selectedProject);
  const projectLabelRows = deriveProjectLabelRows(activeSceneManifest, activeSceneVersion, projectionSource, selectedProfile.labelVersion);
  const projectQualityRows = deriveProjectQualityRows(activeSceneManifest, pendingSceneVersions.length, selectedProject);
  const needsSceneObjective = draftProject.scene === "AI 生成场景候选";
  const canCreateProject = draftProject.name.trim().length > 0
    && draftProject.owner.trim().length > 0
    && (!needsSceneObjective || draftProject.sceneObjective.trim().length >= 10);
  const canSubmitProject = canCreateProject && !projectCreating;

  const createProject = createProjectMutation({
    canCreateProject,
    currentUser,
    draftProject,
    onProjectActivated,
    projectCreating,
    setActiveModule,
    setCreateProjectOpen,
    setDraftProject,
    setProjectCreating,
    setProjectNotice,
    setProjects,
    setSelectedProjectName
  });
  const openSceneGenerator = () => {
    const profile = sceneProfiles[0];
    const name = activeSceneManifest?.display_name ?? selectedProject.name;
    const fallbackKey = Array.from(name).reduce((value, character) => (value * 31 + character.charCodeAt(0)) >>> 0, 7).toString(36);
    const sceneKey = activeSceneManifest?.scene_key ?? `scene-${fallbackKey}`;
    const inputRefs = [
      ...(activeSceneManifest?.data_contract_refs ?? []),
      ...(activeSceneManifest?.knowledge_index_refs ?? []),
      ...(activeSceneManifest?.eval_dataset_version_refs ?? [])
    ];
    setSceneGenerationDraft({
      sceneKey,
      name,
      description: activeSceneManifest?.description ?? `${selectedProject.name} 的版本化业务场景配置`,
      version: `v${Math.max(1, (profile?.versions.length ?? 0) + 1)}.0.0-rc1`,
      objective: activeSceneManifest
        ? `基于当前 ${activeSceneManifest.display_name} 的运行证据、数据契约和评测结果生成下一版场景候选，保留有效能力并修复质量缺口。`
        : `根据 ${selectedProject.name} 的业务目标、数据契约、知识资产和评测要求，生成角色、实体、事件、文档、标签、指标及发布门禁完整的场景候选。`,
      modelRef: "model:scene-planner/v1",
      inputRefs: (inputRefs.length ? inputRefs : [`project:${selectedProjectId}`]).join("\n")
    });
    setSceneGenerateOpen(true);
  };
  const sceneMutations = createSceneProfileMutations({
    activeSceneVersion,
    sceneApiContext,
    sceneBinding,
    sceneGenerationDraft,
    sceneProfiles,
    sceneReviewDecision,
    sceneReviewReason,
    sceneReviewTarget,
    selectedProjectId,
    setProjectNotice,
    setSceneAction,
    setSceneGenerateOpen,
    setSceneRefreshNonce,
    setSceneReviewReason,
    setSceneReviewTarget
  });

  return {
    activeSceneManifest,
    activeSceneVersion,
    activeTab,
    canCreateProject,
    canSubmitProject,
    createProject,
    createProjectOpen,
    currentUser,
    draftProject,
    filteredProjects: filteredProjectRows,
    needsSceneObjective,
    openSceneGenerator,
    pendingSceneVersions,
    projectCreating,
    projectLabelRows,
    projectMembers,
    projectNotice,
    projectQualityRows,
    projectQuery,
    projectSources,
    projectStatusFilter,
    projectionSource,
    apiContext,
    sceneAction,
    sceneBinding,
    sceneGenerateOpen,
    sceneGenerationDraft,
    sceneLoading,
    sceneProfiles,
    sceneReviewDecision,
    sceneReviewReason,
    sceneReviewTarget,
    selectedProfile,
    selectedProject,
    selectedProjectId,
    selectedProjectName,
    setActiveModule,
    setCreateProjectOpen,
    setDraftProject,
    setProjectNotice,
    setProjectQuery,
    setProjectStatusFilter,
    setSceneGenerateOpen,
    setSceneGenerationDraft,
    setSceneReviewDecision,
    setSceneReviewReason,
    setSceneReviewTarget,
    setSelectedProjectName,
    ...sceneMutations
  };
}

export type ProjectWorkspace = ReturnType<typeof useProjectWorkspace>;
