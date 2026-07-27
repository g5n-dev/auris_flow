import { Bell, Check, Languages, LogOut, Settings, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import type { ModuleKey } from "../shared/contracts/navigation";
import type { AuthUser } from "../shared/contracts/auth";
import type { BackendStatus } from "../shared/contracts/operations";
import type { TopbarContextState } from "../shared/contracts/workspace";
import { ApiStatusPill } from "../shared/ui/ApiStatusPill";
import { Lang, Theme, TopbarContextOption, TopbarCoreContextKey, TopbarPanelKey, TopbarVisibleContextKey } from "../shared/contracts/application";
import { ContextSelect } from "./ContextSelect";

export function TopBar({
  theme,
  setTheme,
  lang,
  setLang,
  activeModule,
  currentUser,
  context,
  contextOptions,
  contextState,
  contextError,
  onContextValueChange,
  backendStatus,
  onOpenAccountSettings,
  onLogout,
  logoutPending
}: {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  lang: Lang;
  setLang: (lang: Lang) => void;
  activeModule: ModuleKey;
  currentUser: AuthUser;
  context: TopbarContextState;
  contextOptions: Record<TopbarCoreContextKey, TopbarContextOption[]>;
  contextState: "idle" | "loading" | "ready" | "error" | "switching";
  contextError: string;
  onContextValueChange: (
    key: TopbarCoreContextKey,
    option: TopbarContextOption
  ) => Promise<void>;
  backendStatus: BackendStatus;
  onOpenAccountSettings: () => void;
  onLogout: () => void;
  logoutPending: boolean;
}) {
  const contextLabels: Record<TopbarCoreContextKey, string> = {
    tenant: "租户",
    project: "项目",
    store: "门店",
    date: "日期",
    model: "模型版本",
    label: "标签版本"
  };
  const [openPanel, setOpenPanel] = useState<TopbarPanelKey | null>(null);
  const [topbarFeedback, setTopbarFeedback] = useState("租户已锁定，项目、门店和版本跟随当前租户");
  const contextKeys: TopbarVisibleContextKey[] = ["project", "store", "date", "model", "label"];
  const moduleContextHints: Record<ModuleKey, string> = {
    home: "运营首页按锁定租户聚合项目、待处理和异常资产",
    tenants: "租户管理用于浏览和治理边界，不在顶部直接切换工作租户",
    projects: "项目管理展示当前租户下的项目、成员、标签和质量目标",
    canvas: "任务管理运行时写入 tenant_id、project_id、store_id 和版本标签",
    data: "数据管理按当前租户聚合空间、时间、人物和事件层级",
    knowledge: "知识库按当前租户项目构建索引、质量门禁和证据回跳",
    listening: "调听工作台沿用当前租户、项目、门店和版本证据上下文",
    labels: "标签治理只影响当前租户/项目的标签版本和冲突样本",
    insights: "洞察展示只汇总当前租户下可回到证据片段的数据",
    evaluation: "模型评测按当前租户项目和模型/标签版本对齐样本",
    assets: "资产目录按 tenant_id + project_id 隔离，避免跨租户引用",
    settings: "系统设置只调整当前工作租户可用的策略与审计规则"
  };
  const setContextValue = async (key: TopbarCoreContextKey, option: TopbarContextOption) => {
    setTopbarFeedback(
      key === "project"
        ? `正在安全切换项目：${option.value}`
        : `正在应用${contextLabels[key]}：${option.value}`
    );
    try {
      await onContextValueChange(key, option);
      setTopbarFeedback(
        `${contextLabels[key]}已切换：${option.value} · ${option.meta}，租户仍锁定为 ${context.tenant}`
      );
      setOpenPanel(null);
    } catch (error) {
      setTopbarFeedback(
        error instanceof Error
          ? `${contextLabels[key]}切换失败：${error.message}`
          : `${contextLabels[key]}切换失败，请重试`
      );
    }
  };
  const focusTenantBoundary = () => {
    setOpenPanel(null);
    setTopbarFeedback(`租户为身份隔离边界，当前会话锁定为 ${context.tenant}，不能在工作台内切换。`);
  };
  const lockArcoLightTheme = () => {
    if (theme !== "light") setTheme("light");
    setTopbarFeedback("界面已固定为 Arco 浅色控制台皮肤，不再切换高对比或深色模式。");
  };
  const toggleLanguage = () => {
    const nextLang = lang === "zh" ? "en" : "zh";
    setLang(nextLang);
    setTopbarFeedback(`语言已切换：${nextLang === "zh" ? "中文" : "English"}`);
  };
  useEffect(() => {
    if (!openPanel) return undefined;

    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      const activeOwner = target?.closest<HTMLElement>("[data-topbar-panel-owner]");
      if (activeOwner?.dataset.topbarPanelOwner === openPanel) return;
      setOpenPanel(null);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpenPanel(null);
    };

    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [openPanel]);
  const renderContextPanel = (key: TopbarCoreContextKey) => (
    <div className="topbar-popover context" role="menu">
      <div className="topbar-popover-head">
        <span>{contextLabels[key]}</span>
        <strong>{contextError || topbarFeedback}</strong>
      </div>
      {contextOptions[key].length === 0 && (
        <div className="topbar-qa-row" role="status">
          <span>{contextState === "loading" ? "正在读取 BFF" : "当前范围无可用选项"}</span>
          <strong>未使用本地 fixture</strong>
        </div>
      )}
      {contextOptions[key].map((option) => (
        <button
          key={option.id}
          type="button"
          className={
            (key === "project" ? context[key] === option.value : context[key] === option.id)
              ? "selected"
              : ""
          }
          disabled={contextState === "switching"}
          onClick={() => void setContextValue(key, option)}
        >
          <strong>{option.value}</strong>
          <span>{option.meta}</span>
          {(key === "project" ? context[key] === option.value : context[key] === option.id)
            && <Check size={14} />}
        </button>
      ))}
    </div>
  );
  const renderNotifications = () => {
    const notifications = [
      ["P0", "报价金额与单据冲突", "北京 SKP 店 / 15 个样本"],
      ["P0", "发布门禁阻断", "标签 v1.9.0-rc2 / 2 个风险项"],
      ["P1", "同门店串音待排除", "极光中心店 / 24 个候选"],
      ["P1", "边界切片需复核", "W2 来源片段 / 3 个 overlap"],
      ["P1", "低置信金额实体", "销售C 工牌 / 18 条待审"],
      ["P2", "标签资产待回填", "v1.8.4 影响 12 下游"],
      ["P2", "知识切片语义断裂", "kb-audio-drive-129-c02"],
      ["P2", "评测集补样完成", "报价风险回归集 / +42"],
      ["P3", "CRM FAQ 待同步", "优惠政策 / 24h 增量"],
      ["P3", "音频证据包待入库", "37 个证据包 / 等待索引"],
      ["P3", "日报报告已生成", "运营日报 / 09:30"],
      ["P3", "资产血缘检查通过", "auris/label/event_tags"]
    ];
    return (
      <div className="topbar-popover compact notification-popover" role="dialog" aria-label="通知">
        <div className="topbar-popover-head">
          <span>通知中心</span>
          <strong>{notifications.length} 条待处理</strong>
        </div>
        <div className="topbar-notification-list" role="list">
          {notifications.map(([level, title, meta]) => (
            <button key={`${level}-${title}`} type="button" role="listitem" onClick={() => setTopbarFeedback(`${level} 通知已聚焦：${title}`)}>
              <b>{level}</b>
              <strong>{title}</strong>
              <span>{meta}</span>
            </button>
          ))}
        </div>
      </div>
    );
  };
  const renderAccountPanel = () => (
    <div className="topbar-popover compact account" role="dialog" aria-label="当前登录用户">
      <div className="topbar-popover-head">
        <span>当前登录用户</span>
        <strong>{currentUser.name}</strong>
      </div>
      {[
        ["邮箱", currentUser.email],
        ["身份", currentUser.role],
        ["租户/项目", `${context.tenant} / ${context.project}`],
        ["状态", topbarFeedback]
      ].map(([label, value]) => (
        <div key={label} className="topbar-qa-row">
          <span>{label}</span>
          <strong>{value}</strong>
        </div>
      ))}
      <div className="topbar-account-actions">
        <button
          type="button"
          onClick={() => {
            setOpenPanel(null);
            onOpenAccountSettings();
          }}
        >
          <Settings size={14} />
          账号设置
        </button>
        <button type="button" onClick={onLogout} disabled={logoutPending}>
          <LogOut size={14} />
          {logoutPending ? "正在撤销" : "退出登录"}
        </button>
      </div>
    </div>
  );

  return (
    <header className="topbar">
      <div className="topbar-control">
        <ContextSelect label="租户锁定" value={context.tenant} locked onClick={focusTenantBoundary} />
      </div>
      {contextKeys.map((key) => (
        <div key={key} className="topbar-control" data-topbar-panel-owner={key}>
          <ContextSelect
            label={contextLabels[key]}
            value={
              contextOptions[key].find((option) =>
                key === "project"
                  ? option.value === context[key]
                  : option.id === context[key]
              )?.value || context[key] || "未选择"
            }
            active={openPanel === key}
            disabled={contextState === "loading" || contextState === "switching"}
            onClick={() => setOpenPanel((current) => (current === key ? null : key))}
          />
          {openPanel === key && renderContextPanel(key)}
        </div>
      ))}
      <div className="topbar-context-note" title={moduleContextHints[activeModule]}>
        <ShieldCheck size={14} />
        <span>{moduleContextHints[activeModule]}</span>
      </div>
      <div className="topbar-spacer" />
      <ApiStatusPill status={backendStatus} />
      <button className="icon-pill arco-theme-pill" onClick={lockArcoLightTheme}>
        <ShieldCheck size={16} />
        Arco 浅色
      </button>
      <button className="icon-pill" onClick={toggleLanguage}>
        <Languages size={16} />
        {lang === "zh" ? "中" : "EN"}
      </button>
      <div className="topbar-control" data-topbar-panel-owner="notifications">
        <button className={openPanel === "notifications" ? "bell active" : "bell"} onClick={() => setOpenPanel((current) => (current === "notifications" ? null : "notifications"))}>
          <Bell size={17} />
          <span>12</span>
        </button>
        {openPanel === "notifications" && renderNotifications()}
      </div>
      <div className="topbar-control" data-topbar-panel-owner="account">
        <button className={openPanel === "account" ? "avatar active" : "avatar"} onClick={() => setOpenPanel((current) => (current === "account" ? null : "account"))} title={currentUser.name}>
          {currentUser.initials}
        </button>
        {openPanel === "account" && renderAccountPanel()}
      </div>
    </header>
  );
}
