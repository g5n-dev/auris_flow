import moduleCatalog from "../../modules/moduleCatalog";
import staticCatalog from "../../modules/staticCatalog";
import type {
  AsrServiceProfile,
  AudioServiceParamGroup,
  SettingConfigRow,
  SettingPolicyBundle
} from "./types";
import {
  audioServiceObservabilityRowsSource,
  audioServiceOptimizationRowsSource,
  audioServiceParamGroupsSource
} from "./catalogSources";

const moduleCatalogData = moduleCatalog as {
  moduleConfigs: {
    settings: {
      tabs: Array<{ id: string; label: string }>;
    };
  };
};

const staticCatalogData = staticCatalog as {
  settingsRows: Record<string, SettingConfigRow[]>;
  settingPolicyBundles: SettingPolicyBundle[];
  canvasCatalog: {
    asrServiceProfile: AsrServiceProfile;
    audioServiceParamGroups: typeof audioServiceParamGroupsSource;
    audioServiceObservabilityRows: typeof audioServiceObservabilityRowsSource;
    audioServiceOptimizationRows: typeof audioServiceOptimizationRowsSource;
  };
};

export const settingsTabs = moduleCatalogData.moduleConfigs.settings.tabs;
export const settingsRows = staticCatalogData.settingsRows;
export const settingPolicyBundles = staticCatalogData.settingPolicyBundles;
export const asrServiceProfile = staticCatalogData.canvasCatalog.asrServiceProfile;
export const audioServiceParamGroups = staticCatalogData.canvasCatalog.audioServiceParamGroups;
export const audioServiceObservabilityRows = staticCatalogData.canvasCatalog.audioServiceObservabilityRows;
export const audioServiceOptimizationRows = staticCatalogData.canvasCatalog.audioServiceOptimizationRows;
