import {
  Activity,
  BarChart3,
  BrainCircuit,
  ChevronDown,
  GitBranch,
  LayoutDashboard,
  Link2,
  ShieldCheck
} from "lucide-react";
import { useState, type CSSProperties } from "react";

import type { ModuleDeepLink, ModuleKey } from "../../shared/contracts/navigation";
import { withDeepLinkOrigin } from "../../shared/runtime/deepLinks";
import { PanelHeader } from "../../shared/ui/PanelHeader";
import { homeModuleEntrypoints } from "./entrypoints";
import {
  homeAlerts,
  homeCatalogData,
  homeEvidenceChains,
  homePipelineStages,
  homeProjects,
  homeRunQueues,
  homeRunTrendLabels,
  type HomeRunTrendKey
} from "./fixtures";
import { HomeRunDashboard } from "./HomeRunDashboard";
import { HomeRunSparkline } from "./HomeRunTrendChart";

export type HomeModuleProps = {
  setActiveModule: (module: ModuleKey) => void;
  navigateToTarget: (target: ModuleDeepLink) => void;
  activeTab: string;
};

export function HomeModule({
  setActiveModule,
  navigateToTarget,
  activeTab
}: HomeModuleProps) {
  const [selectedModuleKey, setSelectedModuleKey] = useState<ModuleKey>("listening");
  const [activeRunTrendKey, setActiveRunTrendKey] = useState<HomeRunTrendKey>("health");
  const [activeRunTrendPoint, setActiveRunTrendPoint] = useState(homeRunTrendLabels.length - 1);
  const selectedModule = homeModuleEntrypoints.find((module) => module.key === selectedModuleKey) ?? homeModuleEntrypoints[1];
  const SelectedIcon = selectedModule.icon;
  const openHomeTarget = (target: ModuleDeepLink, originLabel: string, objectLabel?: string) =>
    navigateToTarget(withDeepLinkOrigin(target, originLabel, "home", objectLabel));
  const targetForAgentTask = (taskTitle: string): ModuleDeepLink => {
    if (taskTitle.includes("金额")) {
      return { module: "listening", objectKind: "reviewSample", objectId: "sample-af-128", focusMode: "evidence", title: taskTitle, detail: "报价单 #BJ-041 / 12:27:18" };
    }
    if (taskTitle.includes("串音")) {
      return { module: "listening", objectKind: "reviewSample", objectId: "sample-af-129", focusMode: "matrix", title: taskTitle, detail: "B-2001 / Hall-Mic 同峰" };
    }
    return { module: "assets", tab: "backfill", objectKind: "asset", objectId: "auris/label/event_tags", title: taskTitle, detail: "影响 12 下游 / 标签资产回填" };
  };
  const targetForEvidenceItem = (title: string): ModuleDeepLink => {
    if (title.includes("报价")) return { module: "listening", objectKind: "evidence", objectId: "EVP-quote-128", focusMode: "evidence", title };
    if (title.includes("试驾")) return { module: "data", tab: "relations", objectKind: "dataAsset", objectId: "AF-129", title, detail: "试驾单 #SJ-028 / PBX 空窗" };
    return { module: "assets", tab: "lineage", objectKind: "asset", objectId: "auris/label/event_tags", title, detail: "训练候选 / 标签资产" };
  };
	  const targetForAlert = (title: string, tone: string): ModuleDeepLink => {
	    if (title.includes("金额")) return { module: "listening", objectKind: "reviewSample", objectId: "sample-af-128", focusMode: "evidence", title };
	    if (title.includes("F1")) return { module: "evaluation", tab: "compare", objectKind: "evaluationCapability", objectId: "boundary", title };
	    if (title.includes("原始音频资产")) return { module: "assets", tab: "backfill", objectKind: "asset", objectId: "auris/audio/raw_recordings", focusMode: "lineage", title };
	    if (title.includes("音频")) return { module: "data", tab: "relations", objectKind: "dataAsset", objectId: "AF-129", title };
	    return { module: tone === "teal" ? "assets" : "listening", tab: "backfill", objectKind: "asset", objectId: "auris/label/event_tags", title };
	  };
  const targetForEntityLink = (entityId: string, label: string, route: ModuleKey): ModuleDeepLink => {
    if (entityId.includes("store")) return { module: route, tab: route === "data" ? "relations" : undefined, objectKind: route === "data" ? "dataAsset" : "insightFact", objectId: route === "data" ? "AF-128" : "store-BJ-AURORA-001", title: label, detail: "极光中心店 / 跨项目门店画像" };
    if (entityId.includes("person")) return label.includes("证据") ? { module: "listening", objectKind: "reviewSample", objectId: "sample-af-128", focusMode: "evidence", title: label } : label.includes("标签") ? { module: "labels", tab: "schema", objectKind: "labelIntent", objectId: "quote", title: label } : { module: "evaluation", tab: "badcase", objectKind: "evaluationBadcase", objectId: "B-2031", title: label };
    if (entityId.includes("quote")) return label.includes("证据") ? { module: "listening", objectKind: "evidence", objectId: "EVP-quote-128", focusMode: "evidence", title: label } : label.includes("资产") ? { module: "assets", tab: "lineage", objectKind: "asset", objectId: "auris/label/event_tags", title: label } : { module: "canvas", tab: "inputs", objectKind: "canvasNode", objectId: "eventApi", title: label };
    return label.includes("证据") ? { module: "listening", objectKind: "reviewSample", objectId: "sample-af-128", focusMode: "evidence", title: label } : { module: "assets", tab: "lineage", objectKind: "asset", objectId: "auris/audio/voice_segments", title: label };
  };


  const renderFlowPanel = (title = "今日处理闭环", subtitle = "从接入到洞察的可追踪链路") => (
    <section className="module-panel home-flow-panel">
      <PanelHeader title={title} subtitle={subtitle} icon={<GitBranch size={16} />} />
      <div className="home-flow">
        {homePipelineStages.map((stage) => (
          <button key={stage.label} type="button" className={`home-flow-step ${stage.tone}`} onClick={() => setActiveModule(stage.route)}>
            <span>{stage.label}</span>
            <strong>{stage.value}</strong>
            <em>{stage.meta}</em>
            <HomeRunSparkline
              values={(homeRunQueues.find((queue) => queue.route === stage.route)?.trend ?? [stage.progress - 18, stage.progress - 10, stage.progress - 4, stage.progress]).map((value) => Math.max(0, value))}
              color={stage.tone === "amber" ? "#ff7d00" : stage.tone === "green" ? "#00b42a" : stage.tone === "violet" ? "#722ed1" : stage.tone === "blue" ? "#165dff" : "#14c9c9"}
            />
            <i>
              <b style={{ width: `${stage.progress}%` }} />
            </i>
          </button>
        ))}
      </div>
    </section>
  );

  const renderModulePanel = () => (
    <section className="module-panel home-module-panel">
      <PanelHeader title="模块工作台" subtitle="按当前异常推荐入口和处理路径" icon={<LayoutDashboard size={16} />} />
      <div className="home-module-grid">
        {homeModuleEntrypoints.map((module) => {
          const Icon = module.icon;
          return (
            <button
              key={module.key}
              type="button"
              className={selectedModule.key === module.key ? `home-module-card active ${module.tone}` : `home-module-card ${module.tone}`}
              onClick={() => setSelectedModuleKey(module.key)}
            >
              <span>
                <Icon size={15} />
                {module.label}
              </span>
              <strong>{module.signal}</strong>
              <em>{module.summary}</em>
            </button>
          );
        })}
      </div>
      <div className={`home-module-detail ${selectedModule.tone}`}>
        <div>
          <span>
            <SelectedIcon size={15} />
            当前建议模块
          </span>
          <strong>{selectedModule.label}</strong>
          <p>{selectedModule.summary}</p>
        </div>
        <div className="home-route-chips">
          {selectedModule.path.map((item) => (
            <b key={item}>{item}</b>
          ))}
        </div>
        <button type="button" onClick={() => setActiveModule(selectedModule.key)}>
          {selectedModule.action}
        </button>
      </div>
    </section>
  );

  const renderAgentPanel = (title = "Agent 待办队列", subtitle = "按风险、置信度和影响范围自动排序") => (
    <section className="module-panel home-agent-panel home-focus-panel">
      <PanelHeader title={title} subtitle={subtitle} icon={<BrainCircuit size={16} />} />
      <div className="home-agent-list">
        {homeCatalogData.agentTasks.map((task) => (
          <button key={task.title} type="button" className="home-agent-task" onClick={() => openHomeTarget(targetForAgentTask(task.title), "首页 Agent 待办", task.title)}>
            <div>
              <b>{task.level}</b>
              <span>{task.confidence}</span>
            </div>
            <strong>{task.title}</strong>
            <em>{task.context}</em>
            <p>{task.reason}</p>
            <small>
              {task.chips.map((chip) => (
                <i key={chip}>{chip}</i>
              ))}
            </small>
          </button>
        ))}
      </div>
    </section>
  );

  const renderEvidencePanel = (title = "最近证据与资产链路", subtitle = "音频、事件、标签、单据和 Agent 建议可互相回跳") => (
    <section className="module-panel home-evidence-panel home-focus-panel">
      <PanelHeader title={title} subtitle={subtitle} icon={<ShieldCheck size={16} />} />
      <div className="home-evidence-list">
        {homeEvidenceChains.map((item) => (
          <button key={item.title} type="button" className="home-evidence-item" onClick={() => openHomeTarget(targetForEvidenceItem(item.title), "首页证据链", item.title)}>
            <span>{item.time}</span>
            <div>
              <strong>{item.title}</strong>
              <p>{item.detail}</p>
              <small>
                {item.links.map((link) => (
                  <i key={link}>{link}</i>
                ))}
              </small>
            </div>
          </button>
        ))}
      </div>
    </section>
  );

  const renderStatusPanel = (title = "项目与资产状态", subtitle = "只展示需要运营动作的项目，不做完整列表") => (
    <section className="module-panel home-status-panel">
      <PanelHeader title={title} subtitle={subtitle} icon={<Activity size={16} />} />
      <div className="home-project-list">
        {homeProjects.map((project) => (
          <button key={project.name} type="button" className={`home-project-row ${project.status === "异常" ? "danger" : ""}`} onClick={() => setActiveModule("projects")}>
            <div>
              <strong>{project.name}</strong>
              <span>{project.scene} · {project.status}</span>
            </div>
            <em>待复核 {project.review}</em>
            <b>{project.score}</b>
            <i style={{ "--score": `${project.score}%` } as CSSProperties} />
          </button>
        ))}
      </div>
      <div className="home-alert-strip">
        {homeAlerts.map((alert) => (
          <button key={alert.title} type="button" className={`home-alert-pill ${alert.tone}`} onClick={() => openHomeTarget(targetForAlert(alert.title, alert.tone), "首页异常提醒", alert.title)}>
            <strong>{alert.title}</strong>
            <span>{alert.meta}</span>
          </button>
        ))}
      </div>
    </section>
  );

  const renderSignalPanel = () => (
    <section className="module-panel home-signal-panel">
      <PanelHeader title="洞察信号" subtitle="首页只展示可行动信号，完整趋势进入业务洞察" icon={<BarChart3 size={16} />} />
      <div className="home-signal-grid">
        {[
          ["价格异议", "18.8%", "+2.6", "amber", "listening"],
          ["成交意向", "31.2%", "+5.4", "green", "insights"],
          ["边界 F1", "88.6", "-2.1", "red", "evaluation"],
          ["资产回填", "5", "影响 12", "violet", "assets"]
        ].map(([label, value, delta, tone, route]) => (
          <button key={label} type="button" className={`home-signal-card ${tone}`} onClick={() => setActiveModule(route as ModuleKey)}>
            <span>{label}</span>
            <strong>{value}</strong>
            <em>{delta}</em>
            <i />
          </button>
        ))}
      </div>
      <button type="button" className="home-primary-link" onClick={() => setActiveModule("insights")}>
        进入业务洞察
        <ChevronDown size={14} />
      </button>
    </section>
  );

  const renderEntityPanel = () => (
    <section className="module-panel home-entity-panel home-focus-panel">
      <PanelHeader title="实体关联与跨项目跳转" subtitle="同一实体可来自多个项目，跳转保留租户、项目和主体语义" icon={<Link2 size={16} />} />
      <div className="home-entity-grid">
        {homeCatalogData.entityRelations.map((relation) => (
          <article key={relation.entityId} className="home-entity-card">
            <div className="home-entity-head">
              <span>{relation.kind}</span>
              <strong>{relation.entity}</strong>
              <em>{relation.entityId}</em>
            </div>
            <p>{relation.context}</p>
            <div className="home-entity-projects" aria-label={`${relation.entity} 来源项目`}>
              <span>来源项目</span>
              {relation.sourceProjects.map((project) => (
                <b key={project}>{project}</b>
              ))}
            </div>
            <div className="home-entity-chain" aria-label={`${relation.entity} 关联链路`}>
              {relation.chain.map((step, index) => (
                <span key={step}>
                  {index > 0 && <i>→</i>}
                  <b>{step}</b>
                </span>
              ))}
            </div>
            <div className="home-entity-actions">
              {relation.links.map((link) => (
                <button key={`${relation.entityId}-${link.label}`} type="button" onClick={() => openHomeTarget(targetForEntityLink(relation.entityId, link.label, link.route), "首页实体关联", relation.entity)}>
                  <strong>{link.label}</strong>
                  <span>{link.scope}</span>
                </button>
              ))}
            </div>
          </article>
        ))}
      </div>
    </section>
  );

  if (activeTab === "tasks") {
    return (
      <div className="module-grid home-dashboard-grid home-tab-tasks">
        {renderAgentPanel("待处理任务", "需要人工复核、重跑、回填或进入证据审查的队列")}
        {renderStatusPanel("待处理项目", "按待复核量和质量分排序，点击进入项目配置或证据处理")}
        {renderFlowPanel("待处理分布", "人工网关、失败节点和资产回填会下钻到对应模块")}
        {renderModulePanel()}
      </div>
    );
  }

  if (activeTab === "alerts") {
    return (
      <div className="module-grid home-dashboard-grid home-tab-alerts">
        {renderStatusPanel("异常提醒", "金额冲突、模型下降、证据缺失和资产回填按风险聚合")}
        {renderSignalPanel()}
        {renderAgentPanel("异常根因建议", "Agent 根据证据、模型指标和资产状态给出下一步")}
        {renderFlowPanel("异常所在链路", "定位异常发生在接入、处理、人工网关、资产或洞察回流")}
      </div>
    );
  }

  if (activeTab === "assets") {
    return (
      <div className="module-grid home-dashboard-grid home-tab-assets">
        {renderEvidencePanel()}
        {renderSignalPanel()}
        {renderEntityPanel()}
        {renderStatusPanel("最近资产状态", "只展示最近生成、失败、待回填和影响下游的资产")}
        {renderAgentPanel("资产处理建议", "按证据链完整性、下游影响和可回填程度排序")}
      </div>
    );
  };

  return (
    <div className="module-grid home-dashboard-grid home-default-grid">
      <HomeRunDashboard
        activeRunTrendKey={activeRunTrendKey}
        activeRunTrendPoint={activeRunTrendPoint}
        setActiveRunTrendKey={setActiveRunTrendKey}
        setActiveRunTrendPoint={setActiveRunTrendPoint}
        setActiveModule={setActiveModule}
      />
      {renderAgentPanel()}
      {renderEvidencePanel()}
      {renderStatusPanel()}
      {renderSignalPanel()}
    </div>
  );
}
