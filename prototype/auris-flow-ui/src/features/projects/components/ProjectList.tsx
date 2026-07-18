import { Layers, Plus, Search } from "lucide-react";

import { PanelHeader } from "../../../shared/ui/PanelHeader";
import type { ProjectWorkspace } from "../useProjectWorkspace";

export function ProjectList({ workspace }: { workspace: ProjectWorkspace }) {
  const {
    apiContext,
    currentUser,
    filteredProjects,
    projectQuery,
    projectStatusFilter,
    projectionSource,
    selectedProject,
    setCreateProjectOpen,
    setProjectNotice,
    setProjectQuery,
    setProjectStatusFilter,
    setSelectedProjectName
  } = workspace;
  return (
    <section className="module-panel wide">
      <div className="compact-panel-head">
        <PanelHeader title="项目列表" subtitle={`租户：${currentUser?.tenant ?? String(apiContext.tenantId ?? "当前租户")} / 当前 ${selectedProject.name}`} icon={<Layers size={16} />} />
        <div className="project-head-actions">
          <div className="tenant-search-box">
            <Search size={13} />
            <input value={projectQuery} onChange={(event) => setProjectQuery(event.target.value)} placeholder="搜索项目 / 负责人 / 状态" />
          </div>
          <div className="tenant-filters" aria-label="项目过滤">
            {[
              ["all", "全部"],
              ["running", "运行中"],
              ["attention", "需处理"]
            ].map(([key, label]) => (
              <button
                key={key}
                className={projectStatusFilter === key ? "active" : ""}
                onClick={() => {
                  setProjectStatusFilter(key as typeof projectStatusFilter);
                  setProjectNotice({
                    status: "success",
                    title: "项目筛选已应用",
                    detail: `当前筛选：${label}，右侧概览继续跟随当前选中项目。`
                  });
                }}
              >
                {label}
              </button>
            ))}
          </div>
          <button className="entity-primary-action" type="button" onClick={() => setCreateProjectOpen(true)}>
            <Plus size={14} />
            新建项目
          </button>
        </div>
      </div>
      <div className="project-card-grid">
        {filteredProjects.map((project) => (
          <button
            key={project.name}
            data-testid={projectionSource === "bff" ? "project-projection-row" : undefined}
            type="button"
            className={selectedProject.name === project.name ? "project-work-card active" : project.asset === "健康" ? "project-work-card" : "project-work-card danger"}
            onClick={() => setSelectedProjectName(project.name)}
          >
            <span>{project.status}</span>
            <strong>{project.name}</strong>
            <em>{project.owner} / 今日新增 {project.added}</em>
            {project.projectId && (
              <code className="project-resource-id" title={project.projectId}>
                {project.projectId}
              </code>
            )}
            <small>
              <b>待处理 {project.pending}</b>
              <b>{project.pass}</b>
              <b>{project.asset}</b>
            </small>
          </button>
        ))}
        {filteredProjects.length === 0 && (
          <div className="tenant-empty-state">
            <Search size={22} />
            <strong>没有匹配的项目</strong>
            <span>当前搜索或状态筛选没有命中。可以清空筛选，或新建项目后通过任务配置接入数据。</span>
            <button
              type="button"
              onClick={() => {
                setProjectQuery("");
                setProjectStatusFilter("all");
                setProjectNotice({
                  status: "success",
                  title: "项目筛选已清空",
                  detail: "已恢复全部项目列表。"
                });
              }}
            >
              清空筛选
            </button>
          </div>
        )}
      </div>
    </section>
  );
}
