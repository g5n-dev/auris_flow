import { useEffect, useState } from "react";
import type { ModuleDeepLink, ModuleKey } from "../shared/contracts/navigation";
import type { ModuleConfig } from "../shared/contracts/modules";

type ModuleWorkspaceNavigationInput = {
  moduleKey: Exclude<ModuleKey, "listening">;
  initialTab: string;
  tabs: ModuleConfig["tabs"];
  deepLink: ModuleDeepLink | null;
};

export function useModuleWorkspaceNavigation({
  moduleKey,
  initialTab,
  tabs,
  deepLink
}: ModuleWorkspaceNavigationInput) {
  const [activeTab, setActiveTab] = useState(initialTab);
  const [scopeMenuOpen, setScopeMenuOpen] = useState(false);
  const resetWorkspaceScroll = () => {
    const reset = () => {
      document.querySelector<HTMLElement>(".workspace")?.scrollTo({ top: 0, left: 0 });
      window.scrollTo(0, 0);
    };
    window.requestAnimationFrame(reset);
    window.setTimeout(reset, 80);
  };

  useEffect(() => {
    if (!tabs.some((tab) => tab.id === activeTab)) setActiveTab(tabs[0].id);
  }, [activeTab, tabs]);
  useEffect(() => {
    if (deepLink?.tab && tabs.some((tab) => tab.id === deepLink.tab)) setActiveTab(deepLink.tab);
  }, [tabs, deepLink?.tab]);
  useEffect(() => {
    setScopeMenuOpen(false);
  }, [moduleKey, activeTab]);
  useEffect(() => {
    resetWorkspaceScroll();
  }, [moduleKey, activeTab]);

  return {
    activeTab,
    resetWorkspaceScroll,
    scopeMenuOpen,
    setActiveTab,
    setScopeMenuOpen
  };
}

export type ModuleWorkspaceNavigation = ReturnType<typeof useModuleWorkspaceNavigation>;
