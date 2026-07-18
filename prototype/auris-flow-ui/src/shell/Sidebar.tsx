import { BarChart3, BookOpen, BrainCircuit, Database, Gauge, GitBranch, Headphones, Layers, LayoutDashboard, LogOut, Settings, ShieldCheck, Tags } from "lucide-react";
import { type ComponentType } from "react";
import type { ModuleKey } from "../shared/contracts/navigation";
import type { AuthUser } from "../shared/contracts/auth";

export function Sidebar({
  activeModule,
  setActiveModule,
  currentUser,
  onOpenAccountSettings,
  onLogout,
  logoutPending
}: {
  activeModule: ModuleKey;
  setActiveModule: (module: ModuleKey) => void;
  currentUser: AuthUser;
  onOpenAccountSettings: () => void;
  onLogout: () => void;
  logoutPending: boolean;
}) {
  const items: Array<[ComponentType<{ size?: number }>, string, ModuleKey]> = [
    [LayoutDashboard, "首页", "home"],
    [ShieldCheck, "租户", "tenants"],
    [Layers, "项目", "projects"],
    [GitBranch, "任务", "canvas"],
    [Database, "数据", "data"],
    [BrainCircuit, "知识库", "knowledge"],
    [Headphones, "调听", "listening"],
    [Tags, "标签", "labels"],
    [BarChart3, "洞察", "insights"],
    [Gauge, "评测", "evaluation"],
    [BookOpen, "资产", "assets"],
    [Settings, "设置", "settings"]
  ];
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">A</div>
        <div>
          <strong>Auris Flow</strong>
        </div>
      </div>
      <nav>
        {items.map(([Icon, label, key]) => (
          <button key={label} aria-label={`导航：${label}`} title={`进入${label}`} className={activeModule === key ? "active" : ""} onClick={() => setActiveModule(key)}>
            <Icon size={18} />
            <span>{label}</span>
          </button>
        ))}
      </nav>
      <div className="sidebar-foot sidebar-user">
        <span>当前登录用户</span>
        <button type="button" className="sidebar-user-main" onClick={onOpenAccountSettings} title="打开账号设置">
          <b>{currentUser.initials}</b>
          <strong>{currentUser.name}</strong>
          <em>{currentUser.role}</em>
        </button>
        <button type="button" className="sidebar-user-logout" onClick={onLogout} disabled={logoutPending}>
          <LogOut size={13} />
          {logoutPending ? "正在撤销" : "退出登录"}
        </button>
      </div>
    </aside>
  );
}
