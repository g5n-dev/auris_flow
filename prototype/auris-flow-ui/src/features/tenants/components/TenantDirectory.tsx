import { Check, Plus, Search, ShieldCheck } from "lucide-react";

import { PanelHeader } from "../../../shared/ui/PanelHeader";
import type { TenantWorkspace } from "../useTenantWorkspace";

export function TenantDirectory({ workspace }: { workspace: TenantWorkspace }) {
  const {
    activeTab,
    canManageTenants,
    filteredTenants,
    projectionSource,
    riskFilter,
    selectedTenant,
    setActiveModule,
    setRiskFilter,
    setSelectedTenantName,
    setTenantCreateOpen,
    setTenantNotice,
    setTenantQuery,
    tenantAction,
    tenantAdminUnavailableReason,
    tenantBackendIds,
    tenantQuery,
    updateTenantStatus
  } = workspace;

  return (
    <section className="module-panel wide">
      <div className="tenant-panel-head">
        <PanelHeader title="租户列表" subtitle={`平台隔离边界 / ${activeTab}`} icon={<ShieldCheck size={16} />} />
        <div className="tenant-head-actions">
          <div className="tenant-search-box">
            <Search size={13} />
            <input value={tenantQuery} onChange={(event) => setTenantQuery(event.target.value)} placeholder="搜索租户 / 状态 / 风险" />
          </div>
          <div className="tenant-filters" aria-label="租户过滤">
            {[
              ["all", "全部"],
              ["risk", "有风险"],
              ["active", "活跃"],
              ["trial", "试运行"],
              ["paused", "暂停"]
            ].map(([key, label]) => (
              <button
                key={key}
                className={riskFilter === key ? "active" : ""}
                onClick={() => {
                  setRiskFilter(key as typeof riskFilter);
                  setTenantNotice({
                    status: "success",
                    title: "租户筛选已应用",
                    detail: `当前筛选：${label}，命中结果会保留右侧详情上下文。`
                  });
                }}
              >
                {label}
              </button>
            ))}
          </div>
          <button
            className="entity-primary-action"
            type="button"
            disabled={!canManageTenants || tenantAction === "create"}
            title={!canManageTenants ? tenantAdminUnavailableReason : "由 BFF 创建租户并读回资源状态"}
            onClick={() => setTenantCreateOpen(true)}
          >
            <Plus size={14} />
            {tenantAction === "create" ? "创建中" : "新建租户"}
          </button>
        </div>
      </div>
      <div className="tenant-directory">
        <div className="tenant-table">
          <div className="tenant-table-head">
            <span>租户</span>
            <span>状态</span>
            <span>项目</span>
            <span>成员</span>
            <span>存储</span>
            <span>风险</span>
          </div>
          {filteredTenants.map((tenant) => (
            <button
              key={tenant.name}
              data-testid={projectionSource === "bff" ? "tenant-projection-row" : undefined}
              className={selectedTenant.name === tenant.name ? "tenant-row active" : "tenant-row"}
              onClick={() => setSelectedTenantName(tenant.name)}
            >
              <strong>{tenant.name}</strong>
              <span className={`tenant-status ${tenant.status === "活跃" ? "ok" : tenant.status === "试运行" ? "trial" : tenant.status === "配置中" ? "setup" : "paused"}`}>
                {tenant.status}
              </span>
              <span>{tenant.projects}</span>
              <span>{tenant.members}</span>
              <span>{tenant.storage}</span>
              <span className={tenant.risk === "正常" ? "tenant-risk ok" : "tenant-risk"}>{tenant.risk}</span>
            </button>
          ))}
          {filteredTenants.length === 0 && (
            <div className="tenant-empty-state">
              <Search size={22} />
              <strong>没有匹配的租户</strong>
              <span>当前搜索或筛选没有命中。可以清空关键词，或直接新建租户后再配置项目与数据接入。</span>
              <button
                type="button"
                onClick={() => {
                  setTenantQuery("");
                  setRiskFilter("all");
                  setTenantNotice({
                    status: "success",
                    title: "租户筛选已清空",
                    detail: "已恢复全部租户列表。"
                  });
                }}
              >
                清空筛选
              </button>
            </div>
          )}
        </div>
        <aside className="tenant-detail-card">
          <div>
            <span>当前租户</span>
            <strong>{selectedTenant.name}</strong>
            <em>{selectedTenant.status} / {selectedTenant.projects} 个项目 / {selectedTenant.members} 名成员</em>
          </div>
          <div className="tenant-detail-metrics">
            <span>
              <b>{selectedTenant.storage}</b>
              存储使用
            </span>
            <span>
              <b>{selectedTenant.risk}</b>
              风险状态
            </span>
          </div>
          <div className="tenant-isolation-list">
            {[
              ["权限隔离", "租户管理员只能管理本租户项目和成员"],
              ["数据隔离", "项目资产按 tenant_id + project_id 分区"],
              ["审计隔离", "导出、成员、配额和模型发布写入全局审计"]
            ].map(([label, desc]) => (
              <div key={label}>
                <Check size={13} />
                <span>{label}</span>
                <strong>{desc}</strong>
              </div>
            ))}
          </div>
          <div className="tenant-detail-actions">
            <button onClick={() => setActiveModule("projects")}>查看项目</button>
            <button
              onClick={() => {
                setRiskFilter("risk");
                setTenantNotice({
                  status: "success",
                  title: "已切换到风险租户",
                  detail: "列表只展示风险不为正常的租户。"
                });
              }}
            >
              风险过滤
            </button>
            <button
              className={selectedTenant.status === "暂停" ? "recover" : "danger"}
              disabled={!canManageTenants || tenantAction === "tenant-status" || !tenantBackendIds[selectedTenant.name]}
              title={!canManageTenants ? tenantAdminUnavailableReason : !tenantBackendIds[selectedTenant.name] ? "该原型记录没有后端 tenant_id，禁止本地假更新。" : "通过 BFF 修改并读回租户状态"}
              onClick={updateTenantStatus}
            >
              {tenantAction === "tenant-status" ? "写入中" : selectedTenant.status === "暂停" ? "恢复租户" : "暂停租户"}
            </button>
          </div>
          {!canManageTenants && <small data-testid="tenant-admin-disabled-reason">{tenantAdminUnavailableReason}</small>}
        </aside>
      </div>
    </section>
  );
}
