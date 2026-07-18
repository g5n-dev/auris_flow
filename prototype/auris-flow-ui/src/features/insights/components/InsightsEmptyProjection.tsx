import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { Activity, Building2, Database, FileText, Gauge, LayoutDashboard, LockKeyhole, ShieldCheck, Tags, UserCheck } from "lucide-react";
import type { ComponentType } from "react";
import { cloneFixtureDescriptor, emptyProjectionViews } from "../fixtures/viewDescriptors";

export type InsightProjectionTabKey = keyof typeof emptyProjectionViews;

type ProjectionIcon = ComponentType<{ size?: number }>;
type StaticProjectionView = (typeof emptyProjectionViews)[InsightProjectionTabKey];
type ProjectionView = Omit<StaticProjectionView, "icon"> & { icon: ProjectionIcon };

const projectionIcons = {
  LayoutDashboard,
  Building2,
  UserCheck,
  Tags,
  Gauge,
  FileText
} satisfies Record<string, ProjectionIcon>;

export function InsightsEmptyProjection({ activeTab }: { activeTab: string }) {
  const views = Object.fromEntries(
    (Object.entries(emptyProjectionViews) as Array<[InsightProjectionTabKey, StaticProjectionView]>).map(([key, descriptor]) => {
      const view = cloneFixtureDescriptor(descriptor);
      return [key, {
        ...view,
        icon: projectionIcons[view.icon as keyof typeof projectionIcons]
      }];
    })
  ) as unknown as Record<InsightProjectionTabKey, ProjectionView>;
  const currentTab = (Object.prototype.hasOwnProperty.call(views, activeTab) ? activeTab : "business") as InsightProjectionTabKey;
  const view = views[currentTab];
  const EmptyIcon = view.icon;

  return (
    <div
      className={`insight-command-shell insight-empty-projection insight-empty-${currentTab}`}
      data-audit-tab-content={view.label}
      data-audit-tab-key={currentTab}
      data-testid="insight-empty-projection"
      aria-label={`${view.label}主内容`}
    >
      <section className="module-panel insight-scope-panel">
        <PanelHeader title={view.title} subtitle={view.subtitle} icon={<EmptyIcon size={16} />} sticky />
        <div className="insight-empty-projection-status" role="status">
          <Database size={17} />
          <div>
            <strong>BFF 已返回空集合</strong>
            <span>当前 Tab 展示领域化空态，不注入本地业务数据。</span>
          </div>
        </div>
        <div className="insight-empty-metric-list" aria-label={`${view.label}待同步指标`}>
          {view.metrics.map((metric) => (
            <article key={metric.label} className={`insight-empty-metric ${metric.tone}`}>
              <span>{metric.label}</span>
              <strong>--</strong>
              <b>{metric.state}</b>
              <em>{metric.detail}</em>
            </article>
          ))}
        </div>
        <div className="insight-empty-lineage" aria-label={`${view.label}数据链路`}>
          {view.lineage.map((item, index) => (
            <span key={item}>
              <b>{index + 1}</b>
              {item}
            </span>
          ))}
        </div>
      </section>

      <div className="insight-content-stack">
        <section className="module-panel insight-dashboard-panel">
          <div className="insight-dashboard-head">
            <PanelHeader title={view.canvasTitle} subtitle="当前数据范围 · 真实空态" icon={<EmptyIcon size={16} />} sticky />
          </div>
          <div className="insight-empty-state insight-empty-domain-state">
            <EmptyIcon size={24} />
            <strong>{view.canvasTitle}</strong>
            <span>{view.canvasDetail}</span>
            <div className="insight-empty-checks">
              {view.checks.map((check, index) => (
                <div key={check}>
                  <b>{index + 1}</b>
                  <span>{check}</span>
                  <em>待满足</em>
                </div>
              ))}
            </div>
          </div>
        </section>

        <aside className="module-panel insight-side-panel">
          <PanelHeader title={view.sideTitle} subtitle="生成条件与范围约束" icon={<ShieldCheck size={16} />} sticky />
          <div className="insight-empty-next-step">
            <span>建议动作</span>
            <strong>{view.nextTitle}</strong>
            <p>{view.nextDetail}</p>
          </div>
          <div className="insight-empty-scope-guard">
            <LockKeyhole size={17} />
            <div>
              <strong>范围隔离已生效</strong>
              <span>仅接受当前租户、项目、门店、日期、模型和标签版本的数据。</span>
            </div>
          </div>
          <button type="button" className="insight-empty-disabled-action" disabled title="当前 BFF 范围没有可执行数据">
            <Activity size={14} />
            等待数据同步
          </button>
        </aside>
      </div>
    </div>
  );
}
