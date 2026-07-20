import type { WorkspaceProjectSceneBinding } from "../contracts/moduleWorkspaceGateway";

export type ProjectSceneState = "pending" | "bound" | "unbound" | "error";

export type ProjectSceneLock = {
  scene_profile_id: string;
  scene_profile_version_id: string;
  scene_profile_snapshot_sha256: string;
};

export function resolveProjectSceneLock(
  binding: WorkspaceProjectSceneBinding | null,
  state: ProjectSceneState
): { lock: ProjectSceneLock | null; blockedReason: string } {
  const authoritative = state === "bound"
    && binding?.environment === "production"
    && binding.status === "active"
    && binding.version.status === "published"
    && binding.scene_profile_id === binding.version.scene_profile_id
    && binding.scene_profile_version_id === binding.version.scene_profile_version_id
    && binding.manifest_sha256 === binding.version.manifest_sha256
    && /^[0-9a-f]{64}$/.test(binding.manifest_sha256);

  if (authoritative && binding) {
    return {
      lock: {
        scene_profile_id: binding.scene_profile_id,
        scene_profile_version_id: binding.scene_profile_version_id,
        scene_profile_snapshot_sha256: binding.manifest_sha256
      },
      blockedReason: ""
    };
  }

  const blockedReason = state === "pending"
    ? "正在读取当前项目的已发布 SceneProfile，项目级写入与导出保持禁用"
    : state === "error"
      ? "SceneProfile 绑定读取失败，项目级写入与导出保持禁用"
      : state === "unbound"
        ? "当前项目未绑定已发布 SceneProfile，项目级写入与导出保持禁用"
        : "SceneProfile 绑定快照不完整或发生漂移，项目级写入与导出保持禁用";

  return { lock: null, blockedReason };
}
