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
  setActiveModule,
  currentUser,
  context,
  setContext,
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
  setActiveModule: (module: ModuleKey) => void;
  currentUser: AuthUser;
  context: TopbarContextState;
  setContext: (updater: TopbarContextState | ((current: TopbarContextState) => TopbarContextState)) => void;
  backendStatus: BackendStatus;
  onOpenAccountSettings: () => void;
  onLogout: () => void;
  logoutPending: boolean;
}) {
  const contextOptions: Record<TopbarCoreContextKey, { label: string; options: TopbarContextOption[] }> = {
    tenant: {
      label: "租户",
      options: [
        { value: "极光汽车", meta: "12 项目 / 运行中 9" },
        { value: "北区经销集团", meta: "7 项目 / 2 异常" },
        { value: "华东体验中心", meta: "试运行 / 4 项目" }
      ]
    },
    project: {
      label: "项目",
      options: [
        { value: "销售话术质检", meta: "汽车门店 / 当前主项目" },
        { value: "试驾流程分析", meta: "试驾单据 / 评测中" },
        { value: "门店接待洞察", meta: "同门店多设备分析" }
      ]
    },
    store: {
      label: "门店",
      options: [
        { value: "极光中心店", meta: "北京 / 今日 2,846 音频" },
        { value: "北京 SKP 店", meta: "报价冲突样本 15" },
        { value: "静安体验店", meta: "上海 / 低置信 8" }
      ]
    },
    date: {
      label: "日期",
      options: [
        { value: "2025-05-26", meta: "当前证据日 / 128 会话" },
        { value: "2025-05-25", meta: "已完成 / 可回放" },
        { value: "2025-05-24", meta: "可回填 / 标签 v1.8.3" }
      ]
    },
    model: {
      label: "模型版本",
      options: [
        { value: "v2.3.1", meta: "生产主线 / ASR + Diar" },
        { value: "v2.4-fast", meta: "候选 / 影子评测" },
        { value: "v2.2-stable", meta: "回滚基线" }
      ]
    },
    label: {
      label: "标签版本",
      options: [
        { value: "v1.8.4", meta: "生产标签体系" },
        { value: "v1.9.0-rc2", meta: "候选 / AB实验" },
        { value: "v1.8.3", meta: "历史可回滚" }
      ]
    }
  };
  const [openPanel, setOpenPanel] = useState<TopbarPanelKey | null>(null);
  const [topbarFeedback, setTopbarFeedback] = useState("租户已锁定，项目、门店和版本跟随当前租户");
  const contextKeys: TopbarVisibleContextKey[] = ["project", "store", "model", "label"];
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
  const setContextValue = (key: TopbarCoreContextKey, value: string, meta: string) => {
    setContext((current) => ({ ...current, [key]: value }));
    setTopbarFeedback(`${contextOptions[key].label}已切换：${value} · ${meta}，租户仍锁定为 ${context.tenant}`);
    setOpenPanel(null);
  };
  const focusTenantBoundary = () => {
    setOpenPanel(null);
    setTopbarFeedback(`租户锁定为 ${context.tenant}。如需切换工作边界，请在租户管理中完成授权切换。`);
    setActiveModule("tenants");
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
        <span>{contextOptions[key].label}</span>
        <strong>{topbarFeedback}</strong>
      </div>
      {contextOptions[key].options.map((option) => (
        <button
          key={option.value}
          type="button"
          className={context[key] === option.value ? "selected" : ""}
          onClick={() => setContextValue(key, option.value, option.meta)}
        >
          <strong>{option.value}</strong>
          <span>{option.meta}</span>
          {context[key] === option.value && <Check size={14} />}
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
            label={contextOptions[key].label}
            value={context[key]}
            active={openPanel === key}
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
