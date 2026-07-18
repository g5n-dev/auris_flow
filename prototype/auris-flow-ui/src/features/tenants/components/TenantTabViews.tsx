import {
  BrainCircuit,
  Check,
  Database,
  Eye,
  FileText,
  Gauge,
  GitBranch,
  Layers,
  RotateCcw,
  Settings,
  ShieldCheck,
  UserCheck
} from "lucide-react";

import { QuotaPanel, StackedFacts, TimelineList } from "../../../shared/ui/FactDisplays";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import type { TenantWorkspace } from "../useTenantWorkspace";
import { TenantDirectory } from "./TenantDirectory";

export function TenantTabViews({ workspace }: { workspace: TenantWorkspace }) {
  const {
    activeAuditItems,
    activeTab,
    activeTenantAsr,
    activeTenantAsrRuns,
    activeTenantMembers,
    activeTenantProjects,
    openTenantOutputAsset,
    pullTenantAsrOnce,
    quotaRows,
    selectedTenant,
    setActiveModule,
    tenantAction
  } = workspace;

  const renderTenantProjects = () => (
    <>
      <section className="module-panel wide">
        <div className="compact-panel-head">
          <PanelHeader title="租户项目" subtitle={`${selectedTenant.name} / 项目归属只读，项目内部配置进入项目工作区`} icon={<Layers size={16} />} />
          <button className="entity-primary-action" type="button" onClick={() => setActiveModule("projects")}>
            打开项目工作区
          </button>
        </div>
        <div className="tenant-project-grid">
          {activeTenantProjects.length > 0 ? (
            activeTenantProjects.map((project) => (
              <button key={project.name} type="button" className={project.risk === "正常" ? "tenant-project-card" : "tenant-project-card danger"} onClick={() => setActiveModule("projects")}>
                <span>{project.status}</span>
                <strong>{project.name}</strong>
                <em>{project.dataScope}</em>
                <small>
                  <b>{project.members} 成员</b>
                  <b>{project.assets}</b>
                  <b>{project.risk}</b>
                </small>
              </button>
            ))
          ) : (
            <div className="tenant-empty-state">
              <GitBranch size={22} />
              <strong>还没有项目，租户边界已创建</strong>
              <span>下一步在项目工作区选择业务场景，配置数据源、成员、标签体系和质量目标。</span>
              <button type="button" onClick={() => setActiveModule("projects")}>
                打开项目工作区
              </button>
            </div>
          )}
        </div>
      </section>
      <section className="module-panel">
        <PanelHeader title="项目隔离规则" subtitle="租户页只展示归属，不直接修改项目任务" icon={<ShieldCheck size={16} />} />
        <StackedFacts facts={[["隔离主键", "tenant_id + project_id"], ["数据访问", "项目成员按角色授权"], ["跨项目实体", "通过实体映射表关联，不复制资产"], ["变更入口", "项目工作区 / 任务配置"]]} />
      </section>
    </>
  );
  const renderTenantMembers = () => (
    <>
      <section className="module-panel wide">
        <PanelHeader title="成员权限" subtitle={`${selectedTenant.name} / 租户角色、项目范围和最后活跃`} icon={<UserCheck size={16} />} />
        <div className="tenant-member-list">
          {activeTenantMembers.map((member) => (
            <button key={`${member.name}-${member.role}`} type="button" className="tenant-member-row">
              <span>{member.name}</span>
              <strong>{member.role}</strong>
              <em>{member.scope}</em>
              <b className={member.status === "启用" ? "ok" : ""}>{member.status}</b>
              <small>{member.lastSeen}</small>
            </button>
          ))}
        </div>
      </section>
      <section className="module-panel">
        <PanelHeader title="权限边界" subtitle="平台级角色只控制租户边界，不越权改项目标签" icon={<ShieldCheck size={16} />} />
        <StackedFacts facts={[["租户管理员", "成员、配额、全局审计"], ["项目管理员", "项目数据源、任务、成员"], ["标注主管", "标签版本、人工复核"], ["审计员", "只读审计、导出审批"]]} />
      </section>
    </>
  );
  const renderTenantQuota = () => (
    <>
      <section className="module-panel wide">
        <PanelHeader title="资源配额" subtitle={`${selectedTenant.name} / 资源限制会影响项目任务调度`} icon={<Gauge size={16} />} />
        <QuotaPanel rows={quotaRows} />
      </section>
      <section className="module-panel">
        <PanelHeader title="配额策略" subtitle="调整配额必须进入审计日志" icon={<Settings size={16} />} />
        <StackedFacts facts={[["超限行为", "暂停新任务，不影响已有资产读取"], ["并发控制", "项目任务共享租户并发池"], ["存储策略", "原始音频和派生资产分开统计"], ["审批策略", "高风险租户需管理员确认"]]} />
      </section>
    </>
  );
  const renderTenantAsr = () => (
    <>
      <section className="module-panel wide tenant-asr-panel">
        <div className="compact-panel-head">
          <PanelHeader title="ASR 数据接入" subtitle={`${selectedTenant.name} / 租户级拉取授权、游标、资产写入和审计边界`} icon={<BrainCircuit size={16} />} />
          <button className="entity-primary-action" type="button" onClick={() => setActiveModule("canvas")}>
            配置拉取任务
          </button>
        </div>
        <div className="tenant-asr-hero">
          <div>
            <span>当前 Provider</span>
            <strong>{activeTenantAsr.provider}</strong>
            <em>{activeTenantAsr.serviceId}</em>
          </div>
          <div>
            <span>接入状态</span>
            <strong>{activeTenantAsr.status}</strong>
            <em>{activeTenantAsr.auth}</em>
          </div>
          <div>
            <span>拉取策略</span>
            <strong>{activeTenantAsr.pullMode}</strong>
            <em>下次同步 {activeTenantAsr.nextRun}</em>
          </div>
          <div>
            <span>质量摘要</span>
            <strong>{activeTenantAsr.quality}</strong>
            <em>{activeTenantAsr.quota}</em>
          </div>
        </div>
        <div className="tenant-asr-endpoint">
          <span>租户授权入口</span>
          <strong>{activeTenantAsr.endpoint}</strong>
          <em>游标 {activeTenantAsr.cursor} · {activeTenantAsr.retention}</em>
        </div>
        <div className="tenant-asr-columns">
          <section>
            <span>可拉取数据</span>
            <div className="tenant-asr-list">
              {activeTenantAsr.pullSources.map(([name, schema, detail]) => (
                <div key={name} className="tenant-asr-row">
                  <Database size={14} />
                  <strong>{name}</strong>
                  <code>{schema}</code>
                  <em>{detail}</em>
                </div>
              ))}
            </div>
          </section>
          <section>
            <span>写入资产</span>
            <div className="tenant-asr-list">
              {activeTenantAsr.outputAssets.map(([asset, kind, detail]) => (
                <button key={asset} type="button" className="tenant-asr-row" onClick={() => openTenantOutputAsset(asset)}>
                  <GitBranch size={14} />
                  <strong>{asset}</strong>
                  <code>{kind}</code>
                  <em>{detail}</em>
                </button>
              ))}
            </div>
          </section>
        </div>
      </section>
      <section className="module-panel tenant-asr-panel">
        <PanelHeader title="最近同步" subtitle="拉取、物化、回填都在租户审计下留痕" icon={<RotateCcw size={16} />} />
        <TimelineList items={activeTenantAsrRuns} />
        <div className="tenant-asr-actions">
          <button type="button" disabled={tenantAction === "asr-pull"} onClick={pullTenantAsrOnce}>
            {tenantAction === "asr-pull" ? "拉取中" : "拉取一次"}
          </button>
          <button type="button" onClick={() => setActiveModule("settings")}>服务注册</button>
          <button type="button" onClick={() => openTenantOutputAsset(activeTenantAsr.outputAssets[0]?.[0] ?? "asr_transcript_asset")}>查看资产详情</button>
        </div>
      </section>
      <section className="module-panel tenant-asr-panel">
        <PanelHeader title="接入护栏" subtitle="租户授权不等于项目可随意读取" icon={<ShieldCheck size={16} />} />
        <div className="tenant-asr-guardrails">
          {activeTenantAsr.guardrails.map(([label, detail]) => (
            <div key={label}>
              <Check size={13} />
              <span>{label}</span>
              <strong>{detail}</strong>
            </div>
          ))}
        </div>
      </section>
    </>
  );
  const renderTenantAudit = () => (
    <>
      <section className="module-panel wide">
        <PanelHeader title="全局审计日志" subtitle={`${selectedTenant.name} / 成员、配额、导出、模型发布`} icon={<FileText size={16} />} />
        <TimelineList items={activeAuditItems} />
      </section>
      <section className="module-panel">
        <PanelHeader title="审计范围" subtitle="平台审计只记录租户边界动作，项目运行日志在项目页查看" icon={<Eye size={16} />} />
        <StackedFacts facts={[["成员权限", "角色变更、邀请、禁用"], ["资源配额", "存储、并发、处理小时"], ["数据动作", "导出、回填审批"], ["模型发布", "灰度、回滚、阻断"]]} />
      </section>
    </>
  );

  if (activeTab === "projects") return renderTenantProjects();
  if (activeTab === "members") return renderTenantMembers();
  if (activeTab === "asr") return renderTenantAsr();
  if (activeTab === "quota") return renderTenantQuota();
  if (activeTab === "audit") return renderTenantAudit();
  return (
    <>
      <TenantDirectory workspace={workspace} />
      <section className="module-panel">
        <PanelHeader title="ASR 数据接入" subtitle={`${selectedTenant.name} / ${activeTenantAsr.status}`} icon={<BrainCircuit size={16} />} />
        <div className="tenant-asr-mini">
          <strong>{activeTenantAsr.provider}</strong>
          <span>{activeTenantAsr.pullMode}</span>
          <em>游标 {activeTenantAsr.cursor}</em>
          <button type="button" onClick={() => setActiveModule("canvas")}>
            配置拉取任务
          </button>
        </div>
      </section>
      <section className="module-panel">
        <PanelHeader title="资源配额" subtitle={`${selectedTenant.name} / 当前用量`} icon={<Gauge size={16} />} />
        <QuotaPanel rows={quotaRows} />
      </section>
      <section className="module-panel">
        <PanelHeader title="最近审计" subtitle={`${selectedTenant.name} / 权限、配额、发布`} icon={<FileText size={16} />} />
        <TimelineList items={activeAuditItems.slice(0, 3)} />
      </section>
    </>
  );
}
