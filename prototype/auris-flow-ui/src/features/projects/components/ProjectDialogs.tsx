import { Database, ShieldCheck, X } from "lucide-react";

import { EntitySelect } from "../../../shared/ui/EntitySelect";
import {
  projectDataModeOptions,
  projectLabelVersionOptions,
  projectSceneOptions
} from "../fixtures";
import type { ProjectWorkspace } from "../useProjectWorkspace";

export function ProjectDialogs({ workspace }: { workspace: ProjectWorkspace }) {
  const {
    canCreateProject,
    canSubmitProject,
    createProject,
    createProjectOpen,
    draftProject,
    generateSceneCandidate,
    needsSceneObjective,
    projectCreating,
    reviewSceneVersion,
    sceneAction,
    sceneGenerateOpen,
    sceneGenerationDraft,
    sceneReviewDecision,
    sceneReviewReason,
    sceneReviewTarget,
    setCreateProjectOpen,
    setDraftProject,
    setSceneGenerateOpen,
    setSceneGenerationDraft,
    setSceneReviewDecision,
    setSceneReviewReason,
    setSceneReviewTarget
  } = workspace;
  return (
    <>
      {sceneGenerateOpen && (
        <div className="entity-modal-scrim" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setSceneGenerateOpen(false)}>
          <section className="entity-modal-panel scene-profile-modal" role="dialog" aria-modal="true" aria-label="AI 生成场景候选">
            <div className="entity-modal-head">
              <div>
                <span>SceneProfile</span>
                <strong>AI 生成场景候选</strong>
                <p>模型读取受控引用后生成 scene-profile/1 清单，只写候选，不会直接发布或绑定生产。</p>
              </div>
              <button type="button" aria-label="关闭" onClick={() => setSceneGenerateOpen(false)}><X size={16} /></button>
            </div>
            <div className="entity-form-grid">
              <label>
                场景 Key
                <input value={sceneGenerationDraft.sceneKey} onChange={(event) => setSceneGenerationDraft((draft) => ({ ...draft, sceneKey: event.target.value }))} placeholder="after-sales-repair-quality" autoFocus />
              </label>
              <label>
                候选版本
                <input value={sceneGenerationDraft.version} onChange={(event) => setSceneGenerationDraft((draft) => ({ ...draft, version: event.target.value }))} placeholder="v1.0.0-rc1" />
              </label>
              <label>
                显示名称
                <input value={sceneGenerationDraft.name} onChange={(event) => setSceneGenerationDraft((draft) => ({ ...draft, name: event.target.value }))} placeholder="例如：售后维修工单质检" />
              </label>
              <label>
                模型引用
                <input value={sceneGenerationDraft.modelRef} onChange={(event) => setSceneGenerationDraft((draft) => ({ ...draft, modelRef: event.target.value }))} />
              </label>
              <label className="entity-form-wide">
                场景说明
                <textarea value={sceneGenerationDraft.description} onChange={(event) => setSceneGenerationDraft((draft) => ({ ...draft, description: event.target.value }))} rows={2} />
              </label>
              <label className="entity-form-wide">
                生成目标
                <textarea value={sceneGenerationDraft.objective} onChange={(event) => setSceneGenerationDraft((draft) => ({ ...draft, objective: event.target.value }))} rows={4} />
              </label>
              <label className="entity-form-wide">
                受控输入引用
                <textarea value={sceneGenerationDraft.inputRefs} onChange={(event) => setSceneGenerationDraft((draft) => ({ ...draft, inputRefs: event.target.value }))} rows={3} placeholder="每行一个 data contract / knowledge index / eval dataset / asset 引用" />
              </label>
            </div>
            <div className="entity-create-note warning">
              <ShieldCheck size={15} />
              <span>候选完成后必须依次通过依赖校验、其他实名用户复核、发布与项目绑定。当前操作者不能自审。</span>
            </div>
            <div className="entity-modal-actions">
              <button type="button" disabled={sceneAction === "generating"} onClick={() => setSceneGenerateOpen(false)}>取消</button>
              <button type="button" className="primary" disabled={sceneAction === "generating"} onClick={() => void generateSceneCandidate()}>
                {sceneAction === "generating" ? "创建运行中..." : "生成候选"}
              </button>
            </div>
          </section>
        </div>
      )}
      {sceneReviewTarget && (
        <div className="entity-modal-scrim" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && sceneAction !== "reviewing" && setSceneReviewTarget(null)}>
          <section className="entity-modal-panel scene-profile-modal" role="dialog" aria-modal="true" aria-label="独立复核场景候选">
            <div className="entity-modal-head">
              <div>
                <span>SceneProfile / Human Review</span>
                <strong>独立复核 {sceneReviewTarget.version}</strong>
                <p>核对角色、业务对象、数据契约、任务、标签、知识、指标和三层评测门禁；模型不能执行本步骤。</p>
              </div>
              <button type="button" aria-label="关闭" disabled={sceneAction === "reviewing"} onClick={() => setSceneReviewTarget(null)}><X size={16} /></button>
            </div>
            <div className="entity-form-grid">
              <label>
                复核决定
                <EntitySelect
                  value={sceneReviewDecision}
                  options={[
                    { value: "approved", label: "通过", description: "允许进入发布步骤，不自动绑定项目" },
                    { value: "rejected", label: "退回", description: "保留审计记录，修改后需重新校验" }
                  ]}
                  ariaLabel="选择场景复核决定"
                  onChange={(value) => setSceneReviewDecision(value as "approved" | "rejected")}
                />
              </label>
              <label className="entity-form-wide">
                复核理由
                <textarea
                  value={sceneReviewReason}
                  onChange={(event) => setSceneReviewReason(event.target.value)}
                  rows={4}
                  placeholder="说明通过或退回的事实依据、影响范围和后续动作"
                  autoFocus
                />
              </label>
            </div>
            <div className="entity-create-note warning">
              <ShieldCheck size={15} />
              <span>后端会校验申请人与复核人分离。复核通过后仍需由项目管理员发布，再通过 CAS 绑定项目生产环境。</span>
            </div>
            <div className="entity-modal-actions">
              <button type="button" disabled={sceneAction === "reviewing"} onClick={() => setSceneReviewTarget(null)}>取消</button>
              <button type="button" className="primary" disabled={sceneAction === "reviewing" || sceneReviewReason.trim().length < 3} onClick={() => void reviewSceneVersion()}>
                {sceneAction === "reviewing" ? "提交中..." : sceneReviewDecision === "approved" ? "确认通过" : "确认退回"}
              </button>
            </div>
          </section>
        </div>
      )}
      {createProjectOpen && (
        <div className="entity-modal-scrim" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setCreateProjectOpen(false)}>
          <section className="entity-modal-panel" role="dialog" aria-modal="true" aria-label="新建项目">
            <div className="entity-modal-head">
              <div>
                <span>项目管理</span>
                <strong>新建项目</strong>
                <p>项目只定义业务范围、负责人、标签版本和质量目标；数据接入在任务配置完成。</p>
              </div>
              <button type="button" aria-label="关闭" onClick={() => setCreateProjectOpen(false)}>
                <X size={16} />
              </button>
            </div>
            <div className="entity-form-grid">
              <label>
                项目名称
                <input value={draftProject.name} onChange={(event) => setDraftProject((draft) => ({ ...draft, name: event.target.value }))} placeholder="例如：华南售后维修质检" autoFocus />
              </label>
              <label>
                负责人
                <input value={draftProject.owner} onChange={(event) => setDraftProject((draft) => ({ ...draft, owner: event.target.value }))} />
              </label>
              <label>
                场景配置方式
                <EntitySelect
                  value={draftProject.scene}
                  options={projectSceneOptions}
                  ariaLabel="选择项目场景类型"
                  onChange={(scene) => setDraftProject((draft) => ({ ...draft, scene }))}
                />
              </label>
              {needsSceneObjective && (
                <label className="entity-form-wide">
                  场景目标
                  <textarea
                    value={draftProject.sceneObjective}
                    onChange={(event) => setDraftProject((draft) => ({ ...draft, sceneObjective: event.target.value }))}
                    placeholder="描述业务参与者、输入数据、需要识别的实体/事件、关键指标和不可自动处理的风险。创建项目后由模型生成候选清单。"
                    rows={3}
                  />
                </label>
              )}
              <label>
                数据接入方式
                <EntitySelect
                  value={draftProject.dataMode}
                  options={projectDataModeOptions}
                  ariaLabel="选择数据接入方式"
                  onChange={(dataMode) => setDraftProject((draft) => ({ ...draft, dataMode }))}
                />
              </label>
              <label>
                标签绑定策略
                <EntitySelect
                  value={draftProject.labelVersion}
                  options={projectLabelVersionOptions}
                  ariaLabel="选择初始标签版本"
                  onChange={(labelVersion) => setDraftProject((draft) => ({ ...draft, labelVersion }))}
                />
              </label>
              <label>
                自动通过率目标
                <input value={draftProject.qualityTarget} onChange={(event) => setDraftProject((draft) => ({ ...draft, qualityTarget: event.target.value }))} />
              </label>
            </div>
            <div className="entity-create-note">
              <Database size={15} />
              <span>项目不内置汽车语义。创建后先生成、导入或绑定 SceneProfile，再按清单中的数据契约进入任务配置接入真实数据；模型只能生成候选。</span>
            </div>
            {!canCreateProject && <div className="disabled-reason">项目名称和负责人必填；选择 AI 生成时，场景目标至少填写 10 个字符。</div>}
            {projectCreating && <div className="disabled-reason">正在创建项目，写入完成前不能重复提交。</div>}
            <div className="entity-modal-actions">
              <button type="button" disabled={projectCreating} onClick={() => setCreateProjectOpen(false)}>取消</button>
              <button type="button" disabled={!canSubmitProject} onClick={() => createProject(false)}>
                {projectCreating ? "创建中..." : "创建项目"}
              </button>
              <button type="button" className="primary" disabled={!canSubmitProject} onClick={() => createProject(true)}>
                {projectCreating ? "创建中..." : "创建并接入数据"}
              </button>
            </div>
          </section>
        </div>
      )}
    </>
  );
}
