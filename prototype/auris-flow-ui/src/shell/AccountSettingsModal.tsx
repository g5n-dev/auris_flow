import { Bell, Check, Eye, EyeOff, Layers, LockKeyhole, LogOut, ShieldCheck, Sparkles, UserCheck, X } from "lucide-react";
import { useEffect, useState, type ComponentType } from "react";
import type { AuthUser } from "../shared/contracts/auth";
import { AccountSettingsTab } from "../shared/contracts/application";
import { initialsForName } from "./accountIdentity";

export function AccountSettingsModal({
  user,
  onSave,
  onClose,
  onLogout,
  logoutPending
}: {
  user: AuthUser;
  onSave: (user: AuthUser) => void;
  onClose: () => void;
  onLogout: () => void;
  logoutPending: boolean;
}) {
  type NotificationPrefKey = "review" | "agent" | "security" | "email";
  const [activeTab, setActiveTab] = useState<AccountSettingsTab>("profile");
  const [draft, setDraft] = useState({
    name: user.name,
    email: user.email,
    role: user.role,
    tenant: user.tenant,
    project: user.project,
    defaultEntry: "调听工作台",
    timezone: "Asia/Shanghai"
  });
  const [notificationPrefs, setNotificationPrefs] = useState<Record<NotificationPrefKey, boolean>>({
    review: true,
    agent: true,
    security: true,
    email: false
  });
  const [tokenVisible, setTokenVisible] = useState(false);
  const [securityNotice, setSecurityNotice] = useState("当前设备已受信任，最近登录来自 127.0.0.1。");
  const [saveState, setSaveState] = useState<"idle" | "dirty" | "saved">("idle");

  useEffect(() => {
    setDraft((current) => ({
      ...current,
      name: user.name,
      email: user.email,
      role: user.role,
      tenant: user.tenant,
      project: user.project
    }));
  }, [user.email, user.name, user.project, user.role, user.tenant]);

  const tabs: Array<{ id: AccountSettingsTab; label: string; meta: string; Icon: ComponentType<{ size?: number }> }> = [
    { id: "profile", label: "个人资料", meta: "名称、邮箱、默认入口", Icon: UserCheck },
    { id: "workspace", label: "工作区", meta: "租户、项目、权限边界", Icon: Layers },
    { id: "notifications", label: "通知", meta: "队列、Agent、安全提醒", Icon: Bell },
    { id: "security", label: "安全", meta: "会话、Token、登录审计", Icon: LockKeyhole }
  ];
  const workspaceOptions = [
    { tenant: "极光汽车", project: "销售话术质检", role: "平台管理员", meta: "生产项目 / 标签 v1.8.4" },
    { tenant: "北区经销集团", project: "试驾流程分析", role: "项目管理员", meta: "试运行 / 影子评测" },
    { tenant: "华东体验中心", project: "门店接待洞察", role: "质检负责人", meta: "只读分析 / 数据回放" }
  ];
  const notificationItems: Array<{ key: NotificationPrefKey; title: string; meta: string }> = [
    { key: "review", title: "复核队列提醒", meta: "金额冲突、串音候选、低置信样本进入队列时提醒" },
    { key: "agent", title: "Agent 建议", meta: "Agent 生成映射、标签或证据建议后推送到账号中心" },
    { key: "security", title: "安全与权限", meta: "新设备登录、跨租户访问、Token 变更会强提醒" },
    { key: "email", title: "邮件摘要", meta: "每天 09:00 汇总昨日运行、异常和人工复核结果" }
  ];
  const updateDraft = (key: keyof typeof draft, value: string) => {
    setDraft((current) => ({ ...current, [key]: value }));
    setSaveState("dirty");
  };
  const toggleNotification = (key: NotificationPrefKey) => {
    setNotificationPrefs((current) => ({ ...current, [key]: !current[key] }));
    setSaveState("dirty");
  };
  const selectWorkspace = (tenant: string, project: string, role: string) => {
    setDraft((current) => ({ ...current, tenant, project, role }));
    setSaveState("dirty");
  };
  const handleSave = () => {
    const name = draft.name.trim() || user.name;
    onSave({
      ...user,
      name,
      email: draft.email.trim() || user.email,
      role: draft.role,
      tenant: draft.tenant,
      project: draft.project,
      initials: initialsForName(name)
    });
    setSaveState("saved");
  };

  const renderProfile = () => (
    <>
      <section className="account-settings-card">
        <div className="account-settings-card-head">
          <strong>基础资料</strong>
          <span>保存后同步顶部头像、侧栏身份和审计操作者。</span>
        </div>
        <div className="account-settings-form-grid">
          <label>
            <span>显示名称</span>
            <input value={draft.name} onChange={(event) => updateDraft("name", event.target.value)} />
          </label>
          <label>
            <span>邮箱</span>
            <input value={draft.email} onChange={(event) => updateDraft("email", event.target.value)} />
          </label>
          <label>
            <span>默认入口</span>
            <select value={draft.defaultEntry} onChange={(event) => updateDraft("defaultEntry", event.target.value)}>
              <option>调听工作台</option>
              <option>任务配置</option>
              <option>数据资产</option>
              <option>标签管理</option>
            </select>
          </label>
          <label>
            <span>时区</span>
            <select value={draft.timezone} onChange={(event) => updateDraft("timezone", event.target.value)}>
              <option>Asia/Shanghai</option>
              <option>UTC</option>
              <option>America/Los_Angeles</option>
            </select>
          </label>
        </div>
      </section>
      <section className="account-settings-insight">
        <Sparkles size={17} />
        <div>
          <strong>个人化工作台</strong>
          <span>下次进入会优先恢复最近的租户、项目、门店、模型版本和标签版本。</span>
        </div>
      </section>
    </>
  );

  const renderWorkspace = () => (
    <>
      <section className="account-settings-card">
        <div className="account-settings-card-head">
          <strong>当前工作边界</strong>
          <span>所有任务、数据资产和调听证据都会写入这个租户/项目上下文。</span>
        </div>
        <div className="account-workspace-list">
          {workspaceOptions.map((item) => {
            const active = item.tenant === draft.tenant && item.project === draft.project;
            return (
              <button
                key={`${item.tenant}-${item.project}`}
                type="button"
                className={active ? "active" : ""}
                onClick={() => selectWorkspace(item.tenant, item.project, item.role)}
              >
                <span>{item.tenant}</span>
                <strong>{item.project}</strong>
                <em>{item.meta}</em>
                {active && <Check size={15} />}
              </button>
            );
          })}
        </div>
      </section>
      <section className="account-settings-card-grid">
        {[
          ["权限角色", draft.role, "控制项目配置、任务发布和复核审批入口"],
          ["数据边界", "tenant_id + project_id", "运行记录、资产 Key 和审计日志必须携带"],
          ["默认版本", "ASR v2.3.1 / 标签 v1.8.4", "进入调听和任务配置时自动带入"],
          ["审计状态", "已启用", "账号操作会写入 auris/audit/access_logs"]
        ].map(([label, value, meta]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
            <em>{meta}</em>
          </div>
        ))}
      </section>
    </>
  );

  const renderNotifications = () => (
    <section className="account-settings-card">
      <div className="account-settings-card-head">
        <strong>通知偏好</strong>
        <span>把高优先级事件推到账号中心；低优先级事件只进入模块内队列。</span>
      </div>
      <div className="account-notification-list">
        {notificationItems.map((item) => (
          <button key={item.key} type="button" className="account-notification-row" onClick={() => toggleNotification(item.key)}>
            <div>
              <strong>{item.title}</strong>
              <span>{item.meta}</span>
            </div>
            <span className={notificationPrefs[item.key] ? "account-toggle is-on" : "account-toggle"} aria-pressed={notificationPrefs[item.key]}>
              <i />
              <b>{notificationPrefs[item.key] ? "开启" : "关闭"}</b>
            </span>
          </button>
        ))}
      </div>
    </section>
  );

  const renderSecurity = () => (
    <>
      <section className="account-settings-card">
        <div className="account-settings-card-head">
          <strong>登录与会话</strong>
          <span>{securityNotice}</span>
        </div>
        <div className="account-session-list">
          {[
            ["当前设备", "Chrome / macOS · 127.0.0.1", "活跃中"],
            ["最近登录", "2026-07-02 12:31 · 北京", "已审计"],
            ["API Token", tokenVisible ? "demo_project_token_••••_39d2" : "demo_project_••••••••••••", "项目级"]
          ].map(([label, value, status]) => (
            <div key={label} className="account-session-row">
              <span>{label}</span>
              <strong>{value}</strong>
              <em>{status}</em>
            </div>
          ))}
        </div>
      </section>
      <div className="account-security-actions">
        <button type="button" onClick={() => setTokenVisible((current) => !current)}>
          {tokenVisible ? <EyeOff size={15} /> : <Eye size={15} />}
          {tokenVisible ? "隐藏 Token" : "显示 Token"}
        </button>
        <button type="button" onClick={() => setSecurityNotice("已撤销其他设备会话，当前设备继续保持登录。")}>
          <ShieldCheck size={15} />
          撤销其他会话
        </button>
        <button type="button" onClick={onLogout} disabled={logoutPending}>
          <LogOut size={15} />
          {logoutPending ? "正在撤销会话" : "退出当前账号"}
        </button>
      </div>
    </>
  );

  return (
    <div className="account-settings-scrim" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="account-settings-modal" role="dialog" aria-modal="true" aria-label="账号设置">
        <header className="account-settings-head">
          <div className="account-settings-user">
            <b>{initialsForName(draft.name)}</b>
            <div>
              <span>账号中心</span>
              <strong>{draft.name || user.name}</strong>
              <em>{draft.email}</em>
            </div>
          </div>
          <button type="button" className="account-settings-close" onClick={onClose} aria-label="关闭账号设置">
            <X size={18} />
          </button>
        </header>

        <div className="account-settings-body">
          <nav className="account-settings-tabs" aria-label="账号设置导航">
            {tabs.map(({ id, label, meta, Icon }) => (
              <button key={id} type="button" className={activeTab === id ? "active" : ""} onClick={() => setActiveTab(id)}>
                <Icon size={16} />
                <strong>{label}</strong>
                <span>{meta}</span>
              </button>
            ))}
          </nav>
          <div className="account-settings-content">
            {activeTab === "profile" && renderProfile()}
            {activeTab === "workspace" && renderWorkspace()}
            {activeTab === "notifications" && renderNotifications()}
            {activeTab === "security" && renderSecurity()}
          </div>
        </div>

        <footer className="account-settings-footer">
          <span>
            {saveState === "saved" ? "设置已保存，并同步到当前工作台。" : saveState === "dirty" ? "有未保存更改。" : "当前账号配置已加载。"}
          </span>
          <div>
            <button type="button" onClick={onClose}>
              取消
            </button>
            <button type="button" className="primary" onClick={handleSave}>
              <Check size={15} />
              保存设置
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}
