import {
  BrainCircuit,
  Database,
  Gauge,
  GitBranch,
  Link2,
  Settings,
  ShieldCheck,
  Tags,
  UserCheck
} from "lucide-react";

import { QuotaPanel, StackedFacts } from "../../../shared/ui/FactDisplays";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import type { ProjectWorkspace } from "../useProjectWorkspace";
import { ProjectList } from "./ProjectList";
import { SceneProfilePanel } from "./SceneProfilePanel";

export function ProjectTabViews({ workspace }: { workspace: ProjectWorkspace }) {
  const {
    activeSceneManifest,
    activeTab,
    projectLabelRows,
    projectMembers,
    projectQualityRows,
    projectSources,
    selectedProfile,
    selectedProject,
    setActiveModule
  } = workspace;

  const renderProjectSources = () => (
    <>
      <section className="module-panel wide">
        <div className="compact-panel-head">
          <PanelHeader title="项目数据源" subtitle={`${selectedProject.name} / 数据源只在项目内生效`} icon={<Database size={16} />} />
          <button className="entity-primary-action" type="button" onClick={() => setActiveModule("canvas")}>
            打开任务配置
          </button>
        </div>
        <div className="project-source-list">
          {projectSources.map((source) => (
            <button key={source.key} type="button" className={source.status === "待配置" ? "project-source-row pending" : "project-source-row"} onClick={() => setActiveModule("canvas")}>
              <span>{source.type}</span>
              <strong>{source.name}</strong>
              <em>{source.detail}</em>
              <b>{source.status}</b>
              <small>{source.key}</small>
            </button>
          ))}
        </div>
      </section>
      <section className="module-panel">
        <PanelHeader title="接入逻辑" subtitle="数据契约由场景声明，连接方式由任务配置绑定" icon={<Link2 size={16} />} />
        <StackedFacts facts={activeSceneManifest
          ? activeSceneManifest.data_contract_refs.map((reference, index) => [`契约 ${index + 1}`, reference])
          : [["前置条件", "先生成或导入 SceneProfile"], ["连接器", "在任务配置中绑定实际接口"], ["资产", "输出必须落到版本化资产"], ["运行", "未绑定场景时禁止生产执行"]]}
        />
      </section>
    </>
  );
  const renderProjectMembers = () => (
    <>
      <section className="module-panel wide">
        <PanelHeader title="项目成员" subtitle={`${selectedProject.name} / 权限只作用于当前项目`} icon={<UserCheck size={16} />} />
        <div className="tenant-member-list">
          {projectMembers.map((member) => (
            <button key={`${member.name}-${member.role}`} type="button" className="tenant-member-row">
              <span>{member.name}</span>
              <strong>{member.role}</strong>
              <em>{member.scope}</em>
              <b className={member.status === "启用" ? "ok" : ""}>{member.status}</b>
              <small>{selectedProject.name}</small>
            </button>
          ))}
        </div>
      </section>
      <section className="module-panel">
        <PanelHeader title="项目权限" subtitle="项目成员不能改租户配额和全局审计" icon={<ShieldCheck size={16} />} />
        <StackedFacts facts={[["项目负责人", "配置项目、成员、质量目标"], ["数据接入", "数据源、任务配置、运行记录"], ["标注主管", "标签体系、人工复核"], ["只读审计", "查看日志、不可发布"]]} />
      </section>
    </>
  );
  const renderProjectLabels = () => (
    <>
      <section className="module-panel wide">
        <div className="compact-panel-head">
          <PanelHeader title="标签体系" subtitle={`${selectedProject.name} / 当前版本 ${selectedProfile.labelVersion}`} icon={<Tags size={16} />} />
          <button className="entity-primary-action" type="button" onClick={() => setActiveModule("labels")}>
            打开标签治理
          </button>
        </div>
        <div className="project-label-grid">
          {projectLabelRows.map(([layer, tags, version]) => (
            <button key={layer} type="button" className="project-label-card" onClick={() => setActiveModule("labels")}>
              <span>{layer}</span>
              <strong>{tags}</strong>
              <em>{version}</em>
            </button>
          ))}
        </div>
      </section>
      <section className="module-panel">
        <PanelHeader title="版本规则" subtitle="项目可引用租户默认版本，也可创建候选版本" icon={<Tags size={16} />} />
        <StackedFacts facts={[["默认版本", "继承租户标签体系"], ["候选版本", "仅当前项目灰度"], ["冲突处理", "进入标签治理仲裁"], ["发布影响", "触发资产回填评估"]]} />
      </section>
    </>
  );
  const renderProjectQuality = () => (
    <>
      <section className="module-panel wide">
        <PanelHeader title="质量目标" subtitle={`${selectedProject.name} / 通过率、数据健康、待处理压降`} icon={<Gauge size={16} />} />
        <QuotaPanel rows={projectQualityRows} />
      </section>
      <section className="module-panel">
        <PanelHeader title="模型链路" subtitle={selectedProfile.modelChain} icon={<BrainCircuit size={16} />} />
        <StackedFacts facts={[["场景角色", selectedProfile.ownerTeam], ["评测入口", "核心能力 / 场景评测 / 项目留出集"], ["阻断条件", activeSceneManifest ? activeSceneManifest.release_requirements.map((item) => item.requirement_key).join(" / ") : "由 SceneProfile 发布门禁声明"], ["回流策略", "badcase -> 候选规则或 Prompt -> 评测 -> 独立人审"]]} />
      </section>
    </>
  );

  if (activeTab === "sources") return renderProjectSources();
  if (activeTab === "members") return renderProjectMembers();
  if (activeTab === "labels") return renderProjectLabels();
  if (activeTab === "quality") return renderProjectQuality();
  return (
    <>
      <ProjectList workspace={workspace} />
      <SceneProfilePanel workspace={workspace} />
      <section className="module-panel">
        <PanelHeader title="项目概览" subtitle={`项目是业务容器 · 配置来源：${selectedProfile.source}`} icon={<Settings size={16} />} />
        <StackedFacts facts={[["场景版本", selectedProfile.scene], ["数据契约", selectedProfile.datasource], ["标签引用", selectedProfile.labelVersion], ["能力链路", selectedProfile.modelChain], ["发布门禁", selectedProfile.quality]]} />
      </section>
      <section className="module-panel">
        <PanelHeader title="下一步动作" subtitle="根据项目状态给出合理入口" icon={<GitBranch size={16} />} />
        <div className="project-action-list">
          <button type="button" onClick={() => setActiveModule("canvas")}>
            <GitBranch size={14} />
            配置数据源与任务配置
          </button>
          <button type="button" onClick={() => setActiveModule("labels")}>
            <Tags size={14} />
            查看标签体系
          </button>
          <button type="button" onClick={() => setActiveModule("evaluation")}>
            <Gauge size={14} />
            查看质量评测
          </button>
        </div>
      </section>
    </>
  );
}
