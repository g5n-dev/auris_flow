import { Sparkles } from "lucide-react";

import { PanelHeader } from "../../../shared/ui/PanelHeader";
import type { ProjectWorkspace } from "../useProjectWorkspace";

export function SceneProfilePanel({ workspace }: { workspace: ProjectWorkspace }) {
  const {
    activeSceneManifest,
    activeSceneVersion,
    bindSceneVersion,
    currentUser,
    openSceneGenerator,
    publishSceneVersion,
    sceneAction,
    sceneBinding,
    sceneLoading,
    sceneProfiles,
    selectedProjectId,
    setSceneReviewDecision,
    setSceneReviewReason,
    setSceneReviewTarget,
    validateSceneVersion
  } = workspace;
  return (
    <section className="module-panel wide project-scene-profile-panel" data-testid="project-scene-profile">
      <div className="compact-panel-head">
        <PanelHeader
          title="场景配置"
          subtitle="SceneProfile 决定角色、业务对象、数据契约、标签、指标和发布门禁；汽车只是当前演示 Profile"
          icon={<Sparkles size={16} />}
        />
        <button className="entity-primary-action" type="button" onClick={openSceneGenerator} disabled={sceneAction !== "idle" || !selectedProjectId}>
          <Sparkles size={14} />
          AI 生成候选
        </button>
      </div>
      {sceneLoading ? (
        <div className="scene-profile-empty" role="status">正在读取当前项目场景配置...</div>
      ) : (
        <>
          <div className="scene-profile-authority">
            <div>
              <span>生产绑定</span>
              <strong>{activeSceneManifest?.display_name ?? "未绑定"}</strong>
              <em>{activeSceneVersion ? `${activeSceneVersion.version} · ${activeSceneVersion.status}` : "没有已发布场景时，生产任务会被后端阻断"}</em>
            </div>
            <div>
              <span>权威快照</span>
              <strong>{sceneBinding?.manifest_sha256.slice(0, 12) ?? "—"}</strong>
              <em>{sceneBinding ? `${sceneBinding.environment} · rv${sceneBinding.resource_version}` : "等待发布并绑定"}</em>
            </div>
            <div>
              <span>治理边界</span>
              <strong>{activeSceneManifest?.governance.human_review_required ? "独立人审" : "按清单策略"}</strong>
              <em>模型只能创建候选，不能发布或覆盖线上版本</em>
            </div>
          </div>
          <div className="scene-profile-version-list">
            {sceneProfiles.flatMap((profile) => profile.versions.map((version) => ({ profile, version }))).map(({ profile, version }) => {
              const isBound = sceneBinding?.scene_profile_version_id === version.scene_profile_version_id;
              const canValidate = ["draft", "candidate", "blocked", "validated"].includes(version.status);
              const canPublish = version.status === "approved";
              const canBind = version.status === "published" && !isBound;
              return (
                <div key={version.scene_profile_version_id} className={isBound ? "scene-profile-version active" : "scene-profile-version"}>
                  <div className="scene-profile-version-main">
                    <span>{version.source_type === "model" ? "模型候选" : version.source_type === "import" ? "导入版本" : "人工版本"}</span>
                    <strong>{profile.name} · {version.version}</strong>
                    <em>{version.manifest.roles.length} 角色 / {version.manifest.entities.length} 实体 / {version.manifest.events.length} 事件 / {version.manifest.release_requirements.length} 门禁</em>
                  </div>
                  <div className="scene-profile-version-meta">
                    <b className={`scene-status is-${version.status}`}>{isBound ? "生产已绑定" : version.status}</b>
                    <code title={version.manifest_sha256}>{version.manifest_sha256.slice(0, 10)}</code>
                  </div>
                  <div className="scene-profile-version-actions">
                    {canValidate && (
                      <button type="button" disabled={sceneAction !== "idle"} onClick={() => void validateSceneVersion(version)}>
                        {version.status === "validated" ? "重新校验" : "校验依赖"}
                      </button>
                    )}
                    {version.status === "validated" && (
                      <button
                        type="button"
                        disabled={sceneAction !== "idle" || !currentUser?.roles.some((role) => role === "project_admin" || role === "review_arbitrator")}
                        title="创建/生成申请人与复核人必须是不同实名用户，后端会再次校验"
                        onClick={() => {
                          setSceneReviewTarget(version);
                          setSceneReviewDecision("approved");
                          setSceneReviewReason("");
                        }}
                      >
                        独立复核
                      </button>
                    )}
                    {canPublish && (
                      <button type="button" disabled={sceneAction !== "idle"} onClick={() => void publishSceneVersion(version)}>发布版本</button>
                    )}
                    {canBind && (
                      <button type="button" className="primary" disabled={sceneAction !== "idle"} onClick={() => void bindSceneVersion(version)}>绑定项目</button>
                    )}
                  </div>
                </div>
              );
            })}
            {sceneProfiles.length === 0 && (
              <div className="scene-profile-empty">
                <strong>当前项目还没有 SceneProfile</strong>
                <span>可以让模型基于业务目标和受控数据引用生成候选，也可以导入 scene-profile/1 清单；发布前必须经过校验与独立人审。</span>
              </div>
            )}
            {sceneProfiles.some((profile) => profile.status === "generating" && profile.versions.length === 0) && (
              <div className="scene-profile-empty pending">模型生成运行已排队，完成后会新增 candidate 版本。</div>
            )}
          </div>
        </>
      )}
    </section>
  );
}
